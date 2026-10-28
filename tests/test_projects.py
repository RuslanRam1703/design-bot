"""Project ledger integration tests: no real bank operations or Telegram calls."""
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bot import config, content_store, project_keyboards, project_store
from bot.handlers import admin_projects
from bot.states import ProjectStates
from test_regression import make_callback, make_flow_message, make_state


class ProjectLedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_data = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.temp.name)
        self.original_admin = config.DESIGNER_CHAT_ID
        config.DESIGNER_CHAT_ID = "777"

    def tearDown(self):
        content_store.DATA_DIR = self.original_data
        config.DESIGNER_CHAT_ID = self.original_admin
        self.temp.cleanup()

    async def new_lead(self):
        return await content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Собрать лендинг"},
            {"user_id": 31, "first_name": "Клиент"},
        )

    async def new_project(self):
        lead = await self.new_lead()
        return await project_store.create_from_lead(777, lead["id"])

    async def test_projects_only_created_from_leads(self):
        self.assertEqual(await project_store.list_projects(777), [])
        with self.assertRaisesRegex(ValueError, "не найдена"):
            await project_store.create_from_lead(777, 9999)
        project = await self.new_project()
        self.assertIsNotNone(project["lead_id"])
        self.assertNotIn("is_demo", project)
        menu = project_keyboards.project_list([project])
        actions = [b.callback_data for row in menu.inline_keyboard for b in row]
        self.assertNotIn("pj:newdemo", actions)
        self.assertEqual(len(await project_store.list_projects(777)), 1)

    async def test_real_project_converts_existing_lead_exactly_once(self):
        lead = await self.new_lead()
        project = await project_store.create_from_lead(777, lead["id"])
        repeat = await project_store.create_from_lead(777, lead["id"])
        self.assertEqual(project["id"], repeat["id"])
        self.assertEqual(project["lead_id"], lead["id"])
        self.assertEqual(len(await project_store.list_projects(777)), 1)
        self.assertFalse(await content_store.delete_lead(777, lead["id"]))

    async def test_legacy_lead_writes_never_erase_projects(self):
        project = await self.new_project()
        lead = await self.new_lead()
        self.assertTrue(await content_store.update_lead_status(777, lead["id"], "VIEWED"))
        projects = await project_store.list_projects(777)
        self.assertEqual([p["id"] for p in projects], [project["id"]])
        self.assertEqual((await content_store.get_lead(lead["id"]))["status"], "VIEWED")

    async def test_upstash_lead_mutation_preserves_project_ledger(self):
        from test_regression import FakeUpstash
        fake = FakeUpstash()
        fake.store[content_store.MARKER_KEY] = "1"
        fake.store["leads.json"] = json.dumps({"leads": []})
        with patch.object(config, "UPSTASH_REDIS_REST_URL", "https://fake/"), \
             patch.object(config, "UPSTASH_REDIS_REST_TOKEN", "fake"), \
             patch("bot.content_store.urllib.request.urlopen", side_effect=fake.urlopen):
            lead = await self.new_lead()
            project = await project_store.create_from_lead(777, lead["id"])
            lead = await self.new_lead()
            self.assertTrue(await content_store.update_lead_status(777, lead["id"], "IN_PROGRESS"))
            preserved = await project_store.list_projects(777)
            self.assertEqual([p["id"] for p in preserved], [project["id"]])
            self.assertEqual(json.loads(fake.store["leads.json"])["projects"][0]["id"], project["id"])

    async def test_preexisting_demo_data_remains_readable_but_has_no_special_controls(self):
        project = await self.new_project()
        doc = await content_store._read("leads.json")
        old = dict(project, id="D1", lead_id=None, is_demo=True, title="Ранее созданный проект")
        doc["projects"].append(old)
        await content_store._write("leads.json", doc)
        before = await project_store.list_projects(777)
        self.assertEqual(len(before), 2)
        stored = await project_store.get_project(777, "D1")
        self.assertEqual(stored["title"], "Ранее созданный проект")
        self.assertIn("📂 Проект", admin_projects._project_text(stored))
        controls = [b.callback_data for row in project_keyboards.project_detail(stored).inline_keyboard for b in row]
        self.assertNotIn("pj:delete", controls)
        await project_store.mutate(777, project["id"], "status", "IN_PROGRESS")
        self.assertIsNotNone(await project_store.get_project(777, "D1"))

    async def test_private_ledger_is_not_in_client_lead_record(self):
        lead = await self.new_lead()
        project = await project_store.create_from_lead(777, lead["id"])
        await project_store.mutate(777, project["id"], "price", 60000)
        stored_lead = (await content_store.list_leads_by_user(31))[0]
        self.assertNotIn("projects", stored_lead)
        self.assertNotIn("price_rub", stored_lead)
        self.assertNotIn("payments", stored_lead)
        self.assertIn("projects", await content_store._read("leads.json"))

    async def test_financial_entries_are_append_only_and_voidable(self):
        project = await self.new_project()
        with self.assertRaisesRegex(ValueError, "Сначала укажите"):
            await project_store.mutate(777, project["id"], "pay", 1000)
        await project_store.mutate(777, project["id"], "price", 60000)
        project = await project_store.mutate(777, project["id"], "pay", 30000)
        self.assertEqual(project_store.total_paid(project), 30000)
        self.assertEqual(project["payments"][0]["id"], 1)
        with self.assertRaisesRegex(ValueError, "превышает"):
            await project_store.mutate(777, project["id"], "pay", 31000)
        with self.assertRaisesRegex(ValueError, "меньше"):
            await project_store.mutate(777, project["id"], "price", 29000)
        voided = await project_store.mutate(777, project["id"], "void", 1)
        self.assertEqual(project_store.total_paid(voided), 0)
        self.assertIsNotNone(voided["payments"][0]["voided_at"])
        with self.assertRaises(ValueError):
            await project_store.mutate(777, project["id"], "void", 1)
        self.assertTrue(any(x["kind"] == "void" for x in voided["history"]))
        self.assertTrue(any(x["kind"] == "pay" for x in voided["history"]))

    async def test_amount_validation_rejects_boolean_float_negative_and_overflow(self):
        project = await self.new_project()
        for value in (0, -3, True, 0.5, "100", 100000001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await project_store.mutate(777, project["id"], "price", value)
        self.assertIsNone(admin_projects._parse_rub("-10"))
        self.assertIsNone(admin_projects._parse_rub("1,2"))
        self.assertIsNone(admin_projects._parse_rub("100000001"))
        self.assertEqual(admin_projects._parse_rub("60 000"), 60000)

    async def test_deadlines_stages_status_and_history(self):
        project = await self.new_project()
        await project_store.mutate(777, project["id"], "deadline", "2026-11-15")
        await project_store.mutate(777, project["id"], "stage", 2)
        project = await project_store.mutate(777, project["id"], "status", "IN_PROGRESS")
        self.assertEqual(project["stage_index"], 2)
        self.assertEqual(project["deadline"], "2026-11-15")
        self.assertEqual(project["status"], "IN_PROGRESS")
        self.assertEqual(len(project["history"]), 4)
        with self.assertRaises(ValueError):
            await project_store.mutate(777, project["id"], "deadline", "15.11.2026")
        with self.assertRaises(ValueError):
            await project_store.mutate(777, project["id"], "stage", 88)
        with self.assertRaises(ValueError):
            await project_store.mutate(777, project["id"], "status", "INVENTED")
        await project_store.mutate(777, project["id"], "deadline", None)
        self.assertIsNone((await project_store.get_project(777, project["id"]))["deadline"])

    async def test_non_owner_cannot_read_or_change_or_delete(self):
        project = await self.new_project()
        for coro in (
            project_store.list_projects(31),
            project_store.get_project(31, project["id"]),
            project_store.create_from_lead(31, project["lead_id"]),
            project_store.mutate(31, project["id"], "price", 5000),
        ):
            with self.assertRaises(content_store.NotDesignerError):
                await coro
        callback = make_callback("adminmenu:projects", chat_id=31, message_id=14)
        self.assertFalse(admin_projects._owner_callback(callback))

    async def test_native_keyboard_actions_and_escape_output(self):
        project = await self.new_project()
        project["title"] = "Лендинг <b>&"
        card = admin_projects._project_text(demo)
        self.assertIn("&lt;b&gt;&amp;", card)
        self.assertNotIn("Лендинг <b>&", card)
        keys = [b.callback_data for row in project_keyboards.project_detail(demo).inline_keyboard for b in row]
        self.assertIn("pj:price", keys)
        self.assertIn("pj:deadline", keys)
        self.assertIn("pj:stage", keys)
        self.assertIn("pj:pay", keys)
        self.assertNotIn("pj:delete", keys)
        self.assertLessEqual(max(len(x) for x in keys), 64)

    async def test_payment_confirmation_updates_once(self):
        project = await self.new_project()
        await project_store.mutate(777, project["id"], "price", 60000)
        state = make_state(777)
        await state.update_data(project_id=project["id"], pending_amount=30000)
        await state.set_state(ProjectStates.payment_confirm)
        callback = make_callback("pj:pay:yes", chat_id=777, message_id=25)
        await admin_projects.payment_confirm(callback, state)
        self.assertEqual((await project_store.get_project(777, project["id"]))["payments"][0]["amount"], 30000)
        self.assertEqual(await state.get_state(), ProjectStates.detail.state)
        self.assertEqual(len((await project_store.get_project(777, project["id"]))["payments"]), 1)

    async def test_old_backup_cannot_silently_erase_ledger(self):
        project = await self.new_project()
        legacy = io.BytesIO()
        with zipfile.ZipFile(legacy, "w") as zf:
            zf.writestr("data/leads.json", json.dumps({"leads": []}))
        with self.assertRaises(content_store.BackupValidationError):
            await content_store.import_backup_bytes(777, legacy.getvalue())
        self.assertEqual((await project_store.list_projects(777))[0]["id"], project["id"])

    async def test_backup_contains_ledger_without_new_files(self):
        project = await self.new_project()
        # The existing archive only knows six files; projects share leads.json.
        defaults = {
            "portfolio.json": {"cases": [], "types": []},
            "pricing.json": {"services": [], "options": [], "coefficients": {}, "rounding": {}},
            "faq.json": {"faq": []},
            "about.json": {},
            "ui_config.json": {"menu": {}},
        }
        for name, data in defaults.items():
            (content_store.DATA_DIR / name).write_text(json.dumps(data), encoding="utf-8")
        raw = await content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            self.assertEqual(len([n for n in zf.namelist() if n.startswith("data/")]), 6)
            stored = json.loads(zf.read("data/leads.json"))
        self.assertEqual(stored["projects"][0]["id"], project["id"])


if __name__ == "__main__":
    unittest.main()
