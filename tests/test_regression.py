"""Автоматическая проверка сценариев из ручного regression-чеклиста (см.
обсуждение аудита) — той части, которую можно проверить без реального
Telegram: FSM lifecycle заявки, маршрутизация admin-роутера, referential
integrity в content_store. Клиентский Mini App (JS) и полноценный round-trip
через Telegram polling сюда не входят — см. README/аудит для ручных шагов.

content_store пишет в реальные data/*.json — тесты, которые дёргают
мутирующие функции (ReferentialIntegrityTests), подменяют content_store.DATA_DIR
на временную копию данных на время теста и возвращают как было в tearDown.
Настоящие файлы проекта не трогаются.
"""

import io
import json
import shutil
import tempfile
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot.admin_keyboards as kb
import bot.content_store as content_store
import bot.handlers.admin as admin
import bot.handlers.faq as faq
import bot.handlers.webapp as webapp
import bot.handlers.start as start
import bot.flow as flow
import bot.telegram_auth as telegram_auth
import bot.webserver as webserver
from bot.states import AdminStates, BriefStates


def make_state(chat_id: int = 555) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=chat_id, user_id=chat_id))


def make_message() -> SimpleNamespace:
    """Достаточно для _handle_brief_submission: message.from_user, message.bot
    (async send_message), message.answer (async) — сам объект не обязан быть
    настоящим aiogram Message, потому что типовые аннотации в рантайме не
    проверяются. first_name/last_name присутствуют, т.к. реальный aiogram
    User их всегда отдаёт (first_name обязателен по Telegram Bot API,
    last_name — опционален, но атрибут есть всегда, просто может быть None)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="client", first_name="Клиент", last_name=None),
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
        forward=AsyncMock(),
    )


class BriefLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """P0 из аудита: старое awaiting_tz_file не должно переживать новую
    заявку, отправленную без "пришлю файл" — иначе все следующие сообщения
    клиента перехватываются как файл ТЗ от предыдущей заявки."""

    def setUp(self):
        # _handle_brief_submission теперь пишет заявку через content_store.add_lead
        # (см. Part 6-7 ТЗ) — без подмены DATA_DIR тесты писали бы тестовые
        # заявки в настоящий data/leads.json.
        self.tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_second_submission_without_tz_clears_stale_awaiting_state(self):
        state = make_state()
        message = make_message()

        # Заявка №1: "пришлю файл" -> бот должен перейти в ожидание файла.
        await webapp._handle_brief_submission(message, state, {"service_name": "Лендинг", "attach_tz": True})
        self.assertEqual(await state.get_state(), BriefStates.awaiting_tz_file.state)

        # Заявка №2 (например, клиент передумал и не прислал файл): состояние
        # должно быть полностью очищено, а не остаться от заявки №1.
        await webapp._handle_brief_submission(message, state, {"service_name": "Лендинг", "attach_tz": False})
        self.assertIsNone(await state.get_state())

    async def test_repeat_submission_with_tz_again_still_ends_clean_without_file(self):
        state = make_state()
        message = make_message()

        await webapp._handle_brief_submission(message, state, {"service_name": "Лендинг", "attach_tz": True})
        await webapp._handle_brief_submission(message, state, {"service_name": "Лендинг", "attach_tz": True})
        self.assertEqual(await state.get_state(), BriefStates.awaiting_tz_file.state)

        # Присланный файл закрывает ожидание (тот же путь, что и в проде).
        await webapp.handle_tz_file(message, state)
        self.assertIsNone(await state.get_state())


class LeadNotificationContentTests(unittest.IsolatedAsyncioTestCase):
    """Часть Telegram round-trip, которую МОЖНО проверить офлайн: ровно тот
    payload, который шлёт webapp/js/app.js -> submitBrief(), прогоняется
    через handle_webapp_data (реальный JSON.parse) -> _handle_brief_submission
    -> lead.format_lead_message, без единого обращения к сети. Собственно
    доставку через живой Telegram polling это не проверяет — см. отчёт."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_case_sourced_calculated_brief_produces_expected_notification(self):
        state = make_state()
        message = make_message()

        payload = {
            "action": "submit_brief",
            "service_id": "SITE",
            "service_name": "Сайт",
            "task_description": "Нужен сайт для стоматологии",
            "have": ["references"],
            "deadline": "asap",
            "budget": "40-70",
            "contact": "Тест Тестов — @e2e_test",
            "attach_tz": False,
            "tz_details": None,
            "calc": {"service_id": "SITE", "options": [{"id": "SITE_1", "qty": 1}], "urgent": False, "complex": False},
            "source": "case",
            "source_case_title": "Сайт для частной стоматологии",
        }
        message.web_app_data = SimpleNamespace(data=json.dumps(payload))

        await webapp.handle_webapp_data(message, state)

        message.bot.send_message.assert_awaited_once()
        text = message.bot.send_message.await_args.kwargs["text"]

        self.assertIn("Сайт", text)
        self.assertIn("кейс «Сайт для частной стоматологии»", text)
        self.assertIn("40 000", text)  # цена из расчёта попала в уведомление
        self.assertIn("Бюджет:</b> 40 000–70 000 ₽", text)
        self.assertNotIn("ТЗ:</b> клиент пришлёт файл", text)  # attach_tz=False
        message.answer.assert_awaited_once()
        self.assertIsNone(await state.get_state())  # attach_tz=False -> ничего не ждём

    async def test_direct_brief_has_no_source_noise_in_notification(self):
        state = make_state()
        message = make_message()

        payload = {
            "action": "submit_brief",
            "service_id": None,
            "service_name": "Не определился с услугой",
            "task_description": "Просто вопрос про услуги",
            "have": ["none"],
            "deadline": "unknown",
            "budget": "undecided",
            "contact": "Аноним — @anon",
            "attach_tz": True,
            "tz_details": None,
            "calc": None,
            "source": "direct",
            "source_case_title": None,
        }
        message.web_app_data = SimpleNamespace(data=json.dumps(payload))

        await webapp.handle_webapp_data(message, state)

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("Источник", text)  # "direct" осознанно не показываем — см. lead.py
        self.assertEqual(await state.get_state(), BriefStates.awaiting_tz_file.state)  # attach_tz=True


class AdminCancelContextTests(unittest.TestCase):
    """_resolve_cancel — сердце Механизма 2: "Отмена" должна возвращать к
    разделу, с которого начался мастер, а не всегда в корень."""

    def test_options_target_preserves_service_id_and_returns_to_options_menu(self):
        text, markup, next_state, next_data = admin._resolve_cancel(
            {"cancel_to": "options", "service_id": "LEND", "opt_name": "черновик, должен быть отброшен"}
        )
        self.assertEqual(next_state, AdminStates.edit_service_field_pick)
        self.assertEqual(next_data, {"service_id": "LEND"})
        self.assertIn("Опции", text)

    def test_options_target_without_service_id_falls_back_to_root(self):
        # Защита от испорченных данных состояния — cancel_to="options" без
        # service_id не должен приводить к сломанному экрану.
        text, markup, next_state, next_data = admin._resolve_cancel({"cancel_to": "options"})
        self.assertIsNone(next_state)
        self.assertIn("Админ-меню", text)

    def test_section_targets_clear_state_and_data(self):
        for target in ("cases", "faq", "pricing", "categories"):
            with self.subTest(target=target):
                text, markup, next_state, next_data = admin._resolve_cancel({"cancel_to": target, "junk": 1})
                self.assertIsNone(next_state)
                self.assertEqual(next_data, {})

    def test_unknown_or_missing_target_defaults_to_root(self):
        default_text, *_ = admin._resolve_cancel({})
        unknown_text, *_ = admin._resolve_cancel({"cancel_to": "does-not-exist"})
        self.assertEqual(default_text, unknown_text)
        self.assertIn("Админ-меню", default_text)


class AdminRouterOrderingTests(unittest.TestCase):
    """Механизм 3 предупреждение из аудита: generic fallback обязан стоять
    строго после специфичных хендлеров состояний, а Command("cancel") —
    строго перед ними (иначе текст "/cancel" сохранится как введённые
    данные вместо того, чтобы отменить шаг)."""

    def test_cancel_command_registered_before_specific_text_handlers(self):
        names = [h.callback.__name__ for h in admin.router.message.handlers]
        self.assertIn("admin_cancel_command", names)
        cancel_index = names.index("admin_cancel_command")
        for specific in ("cases_add_title", "faq_add_question", "price_add_name", "option_add_name", "cat_add_label"):
            with self.subTest(handler=specific):
                self.assertLess(cancel_index, names.index(specific))

    def test_unexpected_input_catchall_is_the_last_handler(self):
        names = [h.callback.__name__ for h in admin.router.message.handlers]
        self.assertEqual(names[-1], "admin_unexpected_input")


def make_callback(data: str, chat_id: int = 777) -> SimpleNamespace:
    """Достаточно для admin.py callback-хендлеров: callback.data,
    callback.message.chat.id, callback.message.edit_text (async),
    callback.answer (async)."""
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def make_flow_message(chat_id: int = 888, text: str | None = "/start", delete_raises: bool = False) -> SimpleNamespace:
    """Для bot/flow.py — message.delete (может кидать TelegramAPIError best-
    effort), message.answer возвращает объект с message_id/chat, message.bot
    с delete_message/edit_message_text (тоже async, тоже могут кидать)."""
    async def _delete():
        if delete_raises:
            raise TelegramAPIError(method=None, message="can't delete")

    sent = SimpleNamespace(message_id=555, chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        text=text,
        delete=_delete,
        answer=AsyncMock(return_value=sent),
        bot=SimpleNamespace(delete_message=AsyncMock(), edit_message_text=AsyncMock()),
    )


class FlowUtilTests(unittest.IsolatedAsyncioTestCase):
    """bot/flow.py — принцип перенесён из Personal Assistant
    (src/bot/utils/flow.py), не придуман заново. Все три правила: триггер
    удаляется, старый корневой экран удаляется при открытии нового, шаги
    внутри сценария редактируют одно сообщение. Удаления — best-effort."""

    async def test_open_flow_deletes_previous_anchor_and_trigger_then_tracks_new_message(self):
        state = make_state()
        await state.update_data(_flow_msg_id=111, _flow_chat_id=888)
        msg = make_flow_message()

        await flow.open_flow(msg, state, "Новый экран")

        msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=111)  # RULE 2
        msg.answer.assert_awaited_once_with("Новый экран", reply_markup=None)
        data = await state.get_data()
        self.assertEqual(data["_flow_msg_id"], 555)  # новое сообщение стало текущим экраном

    async def test_open_flow_survives_delete_failures_best_effort(self):
        state = make_state()
        await state.update_data(_flow_msg_id=111, _flow_chat_id=888)
        msg = make_flow_message(delete_raises=True)
        msg.bot.delete_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="too old"))

        await flow.open_flow(msg, state, "Новый экран")  # не должно упасть

        msg.answer.assert_awaited_once()

    async def test_step_from_callback_edits_in_place_not_sending_new_message(self):
        state = make_state()
        cb = make_callback("x", chat_id=888)
        cb.message.message_id = 999
        await flow.step_from_callback(cb, state, "Шаг 2")
        cb.message.edit_text.assert_awaited_once_with("Шаг 2", reply_markup=None)

    async def test_step_from_text_deletes_user_message_and_edits_anchor(self):
        state = make_state()
        await state.update_data(_flow_msg_id=555, _flow_chat_id=888)
        msg = make_flow_message(text="мой ответ")

        await flow.step_from_text(msg, state, "Шаг 3")

        msg.bot.edit_message_text.assert_awaited_once_with("Шаг 3", chat_id=888, message_id=555, reply_markup=None)
        msg.answer.assert_not_awaited()  # редактируем существующее, не шлём новое

    async def test_step_from_text_falls_back_to_new_message_when_edit_fails(self):
        state = make_state()
        await state.update_data(_flow_msg_id=555, _flow_chat_id=888)
        msg = make_flow_message(text="мой ответ")
        msg.bot.edit_message_text = AsyncMock(side_effect=TelegramAPIError(method=None, message="message not found"))

        await flow.step_from_text(msg, state, "Шаг 3")

        msg.answer.assert_awaited_once_with("Шаг 3", reply_markup=None)

    async def test_open_root_clears_fsm_state(self):
        state = make_state()
        await state.set_state(AdminStates.edit_case_field_pick)
        msg = make_flow_message()

        await flow.open_root(msg, state, "Главное меню")

        self.assertIsNone(await state.get_state())


class StartHandlerCleanupTests(unittest.IsolatedAsyncioTestCase):
    """Реальные хендлеры bot/handlers/start.py, переведённые на flow.py —
    /start реально пытается удалить триггер и предыдущий корневой экран,
    а не просто существует в коде."""

    async def test_cmd_start_deletes_trigger_and_tracks_new_root_screen(self):
        state = make_state()
        msg = make_flow_message(text="/start")
        await start.cmd_start(msg, state)
        msg.answer.assert_awaited_once()
        data = await state.get_data()
        self.assertEqual(data["_flow_msg_id"], 555)

    async def test_second_root_command_deletes_first_root_screen(self):
        state = make_state()
        msg1 = make_flow_message(text="/start")
        await start.cmd_start(msg1, state)

        msg2 = make_flow_message(text="/portfolio")
        await start.cmd_portfolio(msg2, state)

        msg2.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=555)

    async def test_fallback_text_deletes_stray_message_and_shows_menu(self):
        state = make_state()
        msg = make_flow_message(text="случайный текст")
        await start.fallback_text(msg, state)
        msg.answer.assert_awaited_once()

    def test_calculator_command_has_no_registered_handler(self):
        # Раньше был отдельный /calculator с заглушкой "теперь это часть
        # заявки" — теперь команда полностью удалена из router, а не
        # оставлена как stub. Проверяем на уровне router.observers, а не
        # только "функции cmd_calculator больше нет в модуле" — важно, что
        # ни один обработчик в router не матчится на /calculator.
        self.assertFalse(hasattr(start, "cmd_calculator"))
        message_observer = start.router.observers["message"]
        for handler_obj in message_observer.handlers:
            for filter_obj in handler_obj.filters:
                callback = getattr(filter_obj, "callback", None)
                commands = getattr(callback, "commands", None) if callback else None
                if commands:
                    self.assertNotIn("calculator", commands)

    async def test_calculator_text_falls_through_to_generic_fallback(self):
        # Реальное поведение при вводе /calculator сейчас — не Command-фильтр
        # (его больше нет), а F.text catch-all: тот же ответ, что на любой
        # нераспознанный текст, без слов про калькулятор/расчёт/перенос.
        state = make_state()
        msg = make_flow_message(text="/calculator")
        await start.fallback_text(msg, state)
        msg.answer.assert_awaited_once()
        sent_text = msg.answer.await_args.args[0]
        for banned in ("калькулятор", "расчёт стоимости", "теперь это", "переехал"):
            self.assertNotIn(banned, sent_text.lower())


class AdminCleanupFlowTests(unittest.IsolatedAsyncioTestCase):
    """/admin (flow.open_root) и мастер добавления FAQ (flow.step_from_text)
    — переведены на bot/flow.py как точечный пример RULE 1-3 в админке;
    реальные хендлеры, не мок логики."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "888"
        self.actor = 888

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_admin_command_deletes_trigger_and_previous_root(self):
        state = make_state(self.actor)
        msg1 = make_flow_message(chat_id=self.actor, text="/admin")
        await admin.cmd_admin(msg1, state)
        msg1.answer.assert_awaited_once()

        msg2 = make_flow_message(chat_id=self.actor, text="/admin")
        await admin.cmd_admin(msg2, state)
        msg2.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=555)

    async def test_faq_add_wizard_edits_one_message_across_steps(self):
        state = make_state(self.actor)
        cb = make_callback("adminfaqaction:add", chat_id=self.actor)
        cb.message.message_id = 555
        await admin.faq_add_start(cb, state)
        cb.message.edit_text.assert_awaited_once_with("Текст вопроса:", reply_markup=kb.cancel_keyboard())

        q_msg = make_flow_message(chat_id=self.actor, text="Сколько стоит лендинг?")
        await admin.faq_add_question(q_msg, state)
        # редактирует существующий anchor (555), а не шлёт новое сообщение
        q_msg.bot.edit_message_text.assert_awaited_once_with("Текст ответа:", chat_id=self.actor, message_id=555, reply_markup=kb.cancel_keyboard())
        q_msg.answer.assert_not_awaited()

        a_msg = make_flow_message(chat_id=self.actor, text="От 25 000 рублей")
        await admin.faq_add_answer(a_msg, state)
        a_msg.bot.edit_message_text.assert_awaited_once()
        a_msg.answer.assert_not_awaited()

        faq_items = content_store.list_faq()
        self.assertTrue(any(i["question"] == "Сколько стоит лендинг?" and i["answer"] == "От 25 000 рублей" for i in faq_items))


class AdminLeadsFullSequenceTests(unittest.IsolatedAsyncioTestCase):
    """TEST D из ТЗ: /admin -> Заявки -> открыть -> изменить статус ->
    вернуться, одной непрерывной последовательностью реальных хендлеров
    (не по отдельности), с проверкой persistence на каждом шаге."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "888"
        self.actor = 888
        self.lead = content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Тест"},
            {"user_id": 55555, "username": "client", "first_name": "Клиент"},
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_list_open_change_status_back_persists_across_steps(self):
        state = make_state(self.actor)

        await admin.menu_leads(make_callback("adminmenu:leads", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)

        await admin.lead_open_detail(make_callback(f"adminleadpick:{self.lead['id']}", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)
        self.assertEqual(content_store.get_lead(self.lead["id"])["status"], "NEW")

        await admin.lead_change_status(make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor), state)
        self.assertEqual(content_store.get_lead(self.lead["id"])["status"], "IN_PROGRESS")  # реально сохранилось

        await admin.lead_back_to_list(make_callback("adminleadaction:back", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)

        # Статус пережил весь проход, не только момент смены
        self.assertEqual(content_store.get_lead(self.lead["id"])["status"], "IN_PROGRESS")


class AdminCancelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Реальный проход через хендлеры (не изолированный вызов
    _resolve_cancel) для сценария, который явно попросили перепроверить:
    Услуги -> редактирование услуги -> Опции -> Добавить опцию -> Отмена
    должна вернуть в меню опций ИМЕННО этой услуги, с сохранённым
    service_id, а не в корень /admin."""

    async def test_cancel_from_nested_option_add_returns_to_options_with_service_id(self):
        state = make_state()

        await admin.price_edit_start(make_callback("adminpriceaction:edit"), state)
        await admin.price_edit_picked(make_callback("admineditservice:LEND"), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick)
        self.assertEqual((await state.get_data())["service_id"], "LEND")

        await admin.option_action(make_callback("adminoptaction:add"), state)
        self.assertEqual(await state.get_state(), AdminStates.option_add_name)
        data_mid_wizard = await state.get_data()
        self.assertEqual(data_mid_wizard["cancel_to"], "options")
        self.assertEqual(data_mid_wizard["service_id"], "LEND")

        cancel_cb = make_callback("admincancel")
        await admin.admin_cancel(cancel_cb, state)

        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick)
        self.assertEqual(await state.get_data(), {"service_id": "LEND"})
        cancel_cb.message.edit_text.assert_awaited_once()
        self.assertIn("Опции", cancel_cb.message.edit_text.await_args.args[0])


def make_photo_message(chat_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        photo=[SimpleNamespace(file_id="fake_file_id")],
        document=None,
        text=None,
        bot=SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_path="photos/fake.jpg")),
            download_file=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


def make_text_message(chat_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), photo=None, document=None, text=text,
        bot=SimpleNamespace(), answer=AsyncMock(),
    )


class AdminCaseConstructorTests(unittest.IsolatedAsyncioTestCase):
    """Реальный проход через bot/handlers/admin.py для конструктора кейса
    (не изолированные вызовы content_store, а настоящие FSM-хендлеры) —
    нашёл здесь живой баг: cases_edit_field для field in ("images", "sections")
    строил `next((c for c in ... if c["id"] == (await state.get_data())[...]), None)` —
    await внутри генератора неявно делает его async-генератором, и next()
    падает с TypeError на КАЖДОМ открытии этих пунктов меню в реальном боте."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = 999
        content_store.add_case(
            str(self.actor), case_id="case_ctor_test", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _case(self):
        return next(c for c in content_store.list_cases() if c["id"] == "case_ctor_test")

    async def _state(self):
        state = make_state(self.actor)
        await state.update_data(case_id="case_ctor_test")
        await state.set_state(AdminStates.edit_case_field_pick)
        return state

    async def test_opening_images_menu_does_not_crash_and_add_remove_works(self):
        state = await self._state()
        cb = make_callback("admineditfield:images", chat_id=self.actor)
        await admin.cases_edit_field(cb, state)  # раньше падало здесь с TypeError
        self.assertEqual(await state.get_state(), AdminStates.case_images_menu.state)

        await admin.case_image_add_start(make_callback("admincaseimgaction:add", chat_id=self.actor), state)
        await admin.case_image_add_receive(make_photo_message(self.actor), state)
        self.assertEqual(len(self._case()["images"]), 2)

        await admin.case_image_picked(make_callback("admincaseimgpick:1", chat_id=self.actor), state)
        await admin.case_image_action(make_callback("admincaseimgact:cover", chat_id=self.actor), state)
        self.assertEqual(self._case()["cover"], self._case()["images"][1])

        await admin.case_image_picked(make_callback("admincaseimgpick:1", chat_id=self.actor), state)
        await admin.case_image_action(make_callback("admincaseimgact:delete", chat_id=self.actor), state)
        self.assertEqual(len(self._case()["images"]), 1)

    async def test_opening_sections_menu_does_not_crash_and_add_edit_works(self):
        state = await self._state()
        cb = make_callback("admineditfield:sections", chat_id=self.actor)
        await admin.cases_edit_field(cb, state)  # раньше падало здесь с TypeError
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)

        await admin.case_section_add_start(make_callback("admincasesecaction:add", chat_id=self.actor), state)
        await admin.case_section_add_type(make_callback("admincasesectype:text", chat_id=self.actor), state)
        await admin.case_section_add_title(make_text_message(self.actor, "Задача"), state)
        await admin.case_section_add_content(make_text_message(self.actor, "Описание задачи"), state)
        self.assertEqual(self._case()["sections"][0], {"type": "text", "title": "Задача", "content": "Описание задачи"})

        await admin.case_section_picked(make_callback("admincasesecpick:0", chat_id=self.actor), state)
        await admin.case_section_action(make_callback("admincasesecact:title", chat_id=self.actor), state)
        await admin.case_section_edit_value(make_text_message(self.actor, "Задача проекта"), state)
        self.assertEqual(self._case()["sections"][0]["title"], "Задача проекта")

    async def test_category_and_external_url_edit_via_real_handlers(self):
        state = await self._state()
        await admin.cases_edit_field(make_callback("admineditfield:category", chat_id=self.actor), state)
        await admin.cases_edit_category(make_callback("admincasenewcat:site", chat_id=self.actor), state)
        self.assertEqual(self._case()["type"], "site")

        await admin.cases_edit_field(make_callback("admineditfield:external_url", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_case_value.state)
        await admin.cases_edit_value(make_text_message(self.actor, "https://behance.net/gallery/x"), state)
        self.assertEqual(self._case()["external_url"], "https://behance.net/gallery/x")


class AdminBackupHandlersTests(unittest.IsolatedAsyncioTestCase):
    """Реальный проход через bot/handlers/admin.py для /admin -> Бэкап:
    экспорт шлёт .zip документом, импорт принимает загруженный .zip и
    реально восстанавливает изменённые данные — не мок, настоящий
    content_store.import_backup_bytes через настоящий хендлер."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = 999

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_export_callback(self):
        return SimpleNamespace(
            data="adminbackupaction:export",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=self.actor),
                edit_text=AsyncMock(),
                answer_document=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    def _make_zip_document_message(self, zip_bytes: bytes):
        return SimpleNamespace(
            chat=SimpleNamespace(id=self.actor),
            document=SimpleNamespace(file_id="fake_zip_id"),
            bot=SimpleNamespace(
                get_file=AsyncMock(return_value=SimpleNamespace(file_path="documents/backup.zip")),
                download_file=AsyncMock(return_value=io.BytesIO(zip_bytes)),
            ),
            answer=AsyncMock(),
        )

    async def test_export_sends_zip_document_with_current_data(self):
        state = make_state(self.actor)
        cb = self._make_export_callback()
        await admin.backup_export(cb, state)
        cb.message.answer_document.assert_awaited_once()
        sent_file = cb.message.answer_document.await_args.args[0]
        with zipfile.ZipFile(io.BytesIO(sent_file.data)) as zf:
            self.assertIn("data/portfolio.json", zf.namelist())

    async def test_import_via_real_handler_restores_changed_data(self):
        zip_bytes = content_store.export_backup_bytes()
        content_store.update_portfolio_type_related_service(str(self.actor), "landing", "SITE")
        self.assertEqual(content_store.default_related_service_for_type("landing"), "SITE")

        state = make_state(self.actor)
        await admin.backup_import_start(make_callback("adminbackupaction:import", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_wait_file.state)

        await admin.backup_import_receive(self._make_zip_document_message(zip_bytes), state)

        self.assertEqual(content_store.default_related_service_for_type("landing"), "LEND")
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_import_bad_zip_via_real_handler_does_not_crash(self):
        state = make_state(self.actor)
        msg = self._make_zip_document_message(b"not a zip")
        await admin.backup_import_receive(msg, state)
        msg.answer.assert_awaited_once()
        self.assertIn("повреждён", msg.answer.await_args.args[0])


class AdminAboutResumeFieldsTests(unittest.IsolatedAsyncioTestCase):
    """Part 2 (уменьшённый объём): location — обычное текстовое поле,
    skills — список через запятую, отдельный от tools. Оба уже существуют
    как ключи в data/about.json (update_about_field требует, чтобы поле
    уже было в data), поэтому реальные хендлеры должны их принимать через
    общий ABOUT_TEXT_FIELDS/ABOUT_LIST_FIELDS механизм без доп. кода."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = 999

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.edit_about_field_pick)
        return state

    async def test_location_edit_via_real_handler_and_clears_needs_review(self):
        self.assertIn("location", content_store.get_about()["needs_review_fields"])
        state = await self._state()
        await admin.about_edit_field(make_callback("admineditabout:location", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_about_value.state)
        await admin.about_edit_value(make_text_message(self.actor, "Москва, удалённо"), state)
        about = content_store.get_about()
        self.assertEqual(about["location"], "Москва, удалённо")
        self.assertNotIn("location", about["needs_review_fields"])

    async def test_skills_edit_is_comma_split_list_separate_from_tools(self):
        state = await self._state()
        await admin.about_edit_field(make_callback("admineditabout:skills", chat_id=self.actor), state)
        await admin.about_edit_value(make_text_message(self.actor, "UX-исследования, Прототипирование"), state)
        about = content_store.get_about()
        self.assertEqual(about["skills"], ["UX-исследования", "Прототипирование"])
        self.assertNotEqual(about["skills"], about["tools"])


class FakeUpstash:
    """Имитирует Upstash Redis REST API в памяти (без сети) — команды
    GET/SET кодируются как JSON-массив в теле POST-запроса, ответ —
    {"result": ...}, ровно как настоящий Upstash REST."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.calls: list[tuple] = []

    def urlopen(self, req, timeout=10):
        args = json.loads(req.data.decode("utf-8"))
        self.calls.append(tuple(args))
        cmd = args[0]
        if cmd == "GET":
            result = self.store.get(args[1])
        elif cmd == "SET":
            self.store[args[1]] = args[2]
            result = "OK"
        else:
            raise AssertionError(f"unexpected command in test double: {args}")
        body = json.dumps({"result": result}).encode("utf-8")
        return _FakeUpstashResponse(body)


class _FakeUpstashResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UpstashPersistenceTests(unittest.TestCase):
    """content_store._read/_write должны переключаться на Upstash Redis
    (REST) вместо локальных файлов, когда заданы креды — конкретно чтобы
    заявки и правки /admin переживали redeploy на бесплатном Render, где
    файловая система эфемерна. Первое чтение (ключа ещё нет в Redis)
    должно засеваться из локального файла-репозитория и сразу сохраняться."""

    def setUp(self):
        self.fake = FakeUpstash()
        self._orig_url = content_store.config.UPSTASH_REDIS_REST_URL
        self._orig_token = content_store.config.UPSTASH_REDIS_REST_TOKEN
        content_store.config.UPSTASH_REDIS_REST_URL = "https://fake-upstash.example/"
        content_store.config.UPSTASH_REDIS_REST_TOKEN = "fake-token"
        self._patch = patch("bot.content_store.urllib.request.urlopen", side_effect=self.fake.urlopen)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        content_store.config.UPSTASH_REDIS_REST_URL = self._orig_url
        content_store.config.UPSTASH_REDIS_REST_TOKEN = self._orig_token

    def test_first_read_seeds_from_local_file_and_persists_to_redis(self):
        data = content_store._read("ui_config.json")
        self.assertIn("menu", data)
        self.assertIn("ui_config.json", self.fake.store)  # засеяно в Redis сразу

    def test_write_then_read_round_trips_through_redis_not_local_disk(self):
        content_store._write("ui_config.json", {"menu": {"portfolio": False}})
        self.assertEqual(json.loads(self.fake.store["ui_config.json"]), {"menu": {"portfolio": False}})

        # локальный файл на диске НЕ тронут — вся мутация ушла только в Redis
        with open(Path(__file__).resolve().parent.parent / "data" / "ui_config.json", encoding="utf-8") as f:
            real_local = json.load(f)
        self.assertNotEqual(real_local, {"menu": {"portfolio": False}})

        # повторное чтение отдаёт то, что записали, а не переседевает заново
        self.assertEqual(content_store._read("ui_config.json"), {"menu": {"portfolio": False}})
        self.assertEqual(self.fake.calls.count(("GET", "ui_config.json")), 1)  # ровно одно чтение из Redis


class BackupExportImportTests(unittest.TestCase):
    """export_backup_bytes/import_backup_bytes — единственный бесплатный
    (без стороннего сервиса) способ пережить redeploy на Render: дизайнер
    выгружает .zip себе в Telegram и загружает обратно после деплоя.
    Покрывает и data/*.json, и загруженные фото."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json", "leads.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"

        # изолируем и папки с фото — реальные webapp/img/* трогать нельзя
        self.img_tmpdir = tempfile.mkdtemp()
        self._orig_img_portfolio = content_store.IMG_PORTFOLIO_DIR
        self._orig_img_about = content_store.IMG_ABOUT_DIR
        content_store.IMG_PORTFOLIO_DIR = Path(self.img_tmpdir) / "portfolio"
        content_store.IMG_ABOUT_DIR = Path(self.img_tmpdir) / "about"
        content_store.IMG_PORTFOLIO_DIR.mkdir(parents=True)
        content_store.IMG_ABOUT_DIR.mkdir(parents=True)
        (content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").write_bytes(b"fake-jpeg-bytes")
        (content_store.IMG_ABOUT_DIR / "avatar.jpg").write_bytes(b"fake-avatar-bytes")

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        content_store.IMG_PORTFOLIO_DIR = self._orig_img_portfolio
        content_store.IMG_ABOUT_DIR = self._orig_img_about
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.img_tmpdir, ignore_errors=True)

    def test_export_zip_contains_data_files_and_images(self):
        zip_bytes = content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        self.assertIn("data/portfolio.json", names)
        self.assertIn("data/leads.json", names)
        self.assertIn("img/portfolio/case_1.jpg", names)
        self.assertIn("img/about/avatar.jpg", names)

    def test_import_restores_json_field_that_changed_after_export(self):
        zip_bytes = content_store.export_backup_bytes()
        content_store.update_portfolio_type_related_service(self.actor, "landing", "SITE")  # мутируем после бэкапа
        self.assertEqual(content_store.default_related_service_for_type("landing"), "SITE")

        content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(content_store.default_related_service_for_type("landing"), "LEND")  # вернулось из бэкапа

    def test_import_restores_deleted_image_file(self):
        zip_bytes = content_store.export_backup_bytes()
        (content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").unlink()
        self.assertFalse((content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").exists())

        content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertTrue((content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").exists())
        self.assertEqual((content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").read_bytes(), b"fake-jpeg-bytes")

    def test_import_requires_designer(self):
        zip_bytes = content_store.export_backup_bytes()
        with self.assertRaises(content_store.NotDesignerError):
            content_store.import_backup_bytes("not-the-designer", zip_bytes)

    def test_import_rejects_non_zip_bytes(self):
        with self.assertRaises(zipfile.BadZipFile):
            content_store.import_backup_bytes(self.actor, b"not a zip file at all")


class ReferentialIntegrityTests(unittest.TestCase):
    """Category -> Service: и находка 09 (кастомные категории должны уметь
    получить related_service), и её следствие, о котором предупредили в
    разборе — удаление услуги не должно оставлять related_service,
    указывающий на несуществующую услугу, ни у категории, ни у кейса."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_custom_category_can_get_a_related_service(self):
        new_type = content_store.add_portfolio_type(self.actor, type_id="cat_custom", label="Тестовая категория")
        self.assertIsNone(new_type["related_service"])  # раньше здесь навсегда осталось бы "нет связи"

        content_store.update_portfolio_type_related_service(self.actor, "cat_custom", "SMM")
        self.assertEqual(content_store.default_related_service_for_type("cat_custom"), "SMM")

    def test_deleting_service_clears_it_from_categories_and_cases(self):
        content_store.update_portfolio_type_related_service(self.actor, "landing", "LEND")
        self.assertEqual(content_store.default_related_service_for_type("landing"), "LEND")
        cases_before = [c["id"] for c in content_store.list_cases() if c.get("related_service") == "LEND"]
        self.assertTrue(cases_before, "фикстура должна содержать хотя бы один кейс с related_service=LEND")

        content_store.delete_service(self.actor, "LEND")

        self.assertIsNone(content_store.default_related_service_for_type("landing"))
        dangling = [c["id"] for c in content_store.list_cases() if c.get("related_service") == "LEND"]
        self.assertEqual(dangling, [], "не должно остаться related_service, указывающих на удалённую услугу")

    def test_category_without_related_service_is_a_valid_steady_state(self):
        # "graphics" в фикстуре осознанно без related_service — объединяет
        # 3 услуги, однозначно выбрать нельзя (см. data/pricing.json -> groups).
        self.assertIsNone(content_store.default_related_service_for_type("graphics"))
        case = content_store.add_case(
            self.actor, case_id="case_test_graphics", title="Тест", type_id="graphics",
            cover="img/portfolio/x.svg", task="t",
            related_service=content_store.default_related_service_for_type("graphics"),
        )
        self.assertIsNone(case["related_service"])

    def test_category_without_cases_can_be_deleted_category_with_cases_cannot(self):
        empty_type = content_store.add_portfolio_type(self.actor, type_id="cat_empty", label="Пустая категория")
        self.assertEqual(content_store.count_cases_with_type(empty_type["id"]), 0)
        self.assertTrue(content_store.delete_portfolio_type(self.actor, empty_type["id"]))

        self.assertGreater(content_store.count_cases_with_type("landing"), 0)
        self.assertFalse(content_store.delete_portfolio_type(self.actor, "landing"))

    def test_deleting_a_service_with_no_cases_referencing_it_is_a_clean_noop_on_portfolio(self):
        service_id = content_store.next_service_id()
        content_store.add_service(
            self.actor, service_id=service_id, name="Тестовая услуга",
            base_price=1000, term_min=1, term_max=2, includes="—",
        )
        portfolio_before = content_store._read("portfolio.json")

        self.assertTrue(content_store.delete_service(self.actor, service_id))

        portfolio_after = content_store._read("portfolio.json")
        self.assertEqual(portfolio_before, portfolio_after, "услуга без единой ссылки не должна менять portfolio.json вовсе")


class ClientFacingFaqFilterTests(unittest.IsolatedAsyncioTestCase):
    """UX-аудит, находка F04: needs_review-пункты FAQ ("этот пункт ещё
    дорабатывается") не должны попадать клиенту — ни в список, ни по
    прямому клику на устаревшую клавиатуру старого сообщения.

    Проверяется на синтетических данных, а не на живом data/faq.json —
    сама логика фильтра не должна зависеть от того, сколько needs_review-
    пунктов реально осталось незаполненными на данный момент (сейчас,
    после заполнения demo-контента, их 0 — это не повод переставать
    проверять сам фильтр)."""

    FAKE_FAQ = {
        "faq": [
            {"id": 101, "question": "Готовый вопрос", "type": "static", "answer": "Готовый ответ", "needs_review": False},
            {"id": 102, "question": "Черновой вопрос", "type": "static", "answer": "черновик", "needs_review": True},
        ]
    }

    def test_client_faq_items_excludes_needs_review(self):
        with patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            items = faq._client_faq_items()
        self.assertEqual([i["id"] for i in items], [101])

    async def test_faq_answer_treats_needs_review_item_as_not_found(self):
        callback = make_callback("faq:102")
        with patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            await faq.faq_answer(callback)
        callback.answer.assert_awaited_once_with("Вопрос не найден", show_alert=True)
        callback.message.edit_text.assert_not_awaited()


class ContentReadinessSummaryTests(unittest.TestCase):
    """UX-аудит, находки F01-F03: сводка в /admin должна отражать реальное
    число незавершённых пунктов контента, а не быть статичным нулём."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_summary_reflects_current_fixture_state(self):
        # demo-контент заполнен (см. content fill pass): реальные обложки,
        # все FAQ отвечены; About.education/links намеренно оставлены
        # пустыми — банер всё ещё должен предупреждать именно про них.
        summary = content_store.content_readiness_summary()
        self.assertEqual(summary["placeholder_cases"], 0)
        self.assertEqual(summary["faq_pending"], 0)
        self.assertGreater(summary["about_pending_fields"], 0)
        text = admin._admin_root_text()
        self.assertIn("⚠️", text)
        self.assertIn("Обо мне", text)

    def test_summary_drops_to_zero_once_content_is_filled_in(self):
        for c in content_store.list_cases():
            content_store.update_case(self.actor, c["id"], cover="img/portfolio/real_photo.jpg")
        about = content_store.get_about()
        for field in list(about.get("needs_review_fields", [])):
            content_store.update_about_field(self.actor, field, "заполнено")
        for item in content_store.list_faq():
            if item.get("needs_review"):
                content_store.update_faq(self.actor, item["id"], answer="Готовый ответ")

        summary = content_store.content_readiness_summary()
        self.assertEqual(summary, {"placeholder_cases": 0, "about_pending_fields": 0, "faq_pending": 0})
        self.assertNotIn("⚠️", admin._admin_root_text())


class CaseCategoryChangeTests(unittest.TestCase):
    """Part 1 ТЗ: категория кейса стала editable (раньше — только при
    создании). related_service должен предлагать новый дефолт только если
    раньше НЕ был осознанно выбран вручную — см. update_case_category."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_related_service_follows_new_category_default_when_not_customized(self):
        case = content_store.add_case(
            self.actor, case_id="case_cat_test1", title="Т", type_id="landing",
            cover="img/portfolio/x.svg", task="t", related_service="LEND",
        )
        self.assertEqual(case["related_service"], "LEND")  # совпадает с дефолтом старой категории

        content_store.update_case_category(self.actor, "case_cat_test1", "site")

        updated = next(c for c in content_store.list_cases() if c["id"] == "case_cat_test1")
        self.assertEqual(updated["type"], "site")
        self.assertEqual(updated["related_service"], "SITE", "не тронут вручную -> подставляем новый дефолт")

    def test_manually_customized_related_service_survives_category_change(self):
        case = content_store.add_case(
            self.actor, case_id="case_cat_test2", title="Т", type_id="landing",
            cover="img/portfolio/x.svg", task="t", related_service="UXUI",
        )
        self.assertEqual(case["related_service"], "UXUI")  # отличается от дефолта "landing" (LEND) -> выбран вручную

        content_store.update_case_category(self.actor, "case_cat_test2", "site")

        updated = next(c for c in content_store.list_cases() if c["id"] == "case_cat_test2")
        self.assertEqual(updated["type"], "site")
        self.assertEqual(updated["related_service"], "UXUI", "осознанный выбор не должен стираться сменой категории")

    def test_update_case_category_returns_false_for_unknown_case(self):
        self.assertFalse(content_store.update_case_category(self.actor, "case_does_not_exist", "site"))


class CaseImageManagementTests(unittest.TestCase):
    """Part 1 ТЗ: независимое управление галереей — добавить/удалить/
    переставить/назначить обложку, без пустых состояний и без потери
    обложки при удалении текущей."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"
        content_store.add_case(
            self.actor, case_id="case_img_test", title="Т", type_id="landing",
            cover="img/portfolio/a.svg", task="t", related_service=None,
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _case(self):
        return next(c for c in content_store.list_cases() if c["id"] == "case_img_test")

    def test_add_image_does_not_override_existing_cover_unless_forced(self):
        content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        case = self._case()
        self.assertEqual(case["images"], ["img/portfolio/a.svg", "img/portfolio/b.svg"])
        self.assertEqual(case["cover"], "img/portfolio/a.svg")

        content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/c.svg", set_as_cover=True)
        self.assertEqual(self._case()["cover"], "img/portfolio/c.svg")

    def test_remove_cover_image_reassigns_to_first_remaining(self):
        content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        content_store.remove_case_image(self.actor, "case_img_test", "img/portfolio/a.svg")
        case = self._case()
        self.assertEqual(case["images"], ["img/portfolio/b.svg"])
        self.assertEqual(case["cover"], "img/portfolio/b.svg")

    def test_remove_last_image_leaves_cover_none_not_broken(self):
        content_store.remove_case_image(self.actor, "case_img_test", "img/portfolio/a.svg")
        case = self._case()
        self.assertEqual(case["images"], [])
        self.assertIsNone(case["cover"])

    def test_reorder_image_swaps_and_rejects_out_of_bounds(self):
        content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        self.assertTrue(content_store.reorder_case_image(self.actor, "case_img_test", "img/portfolio/b.svg", "up"))
        self.assertEqual(self._case()["images"], ["img/portfolio/b.svg", "img/portfolio/a.svg"])
        self.assertFalse(content_store.reorder_case_image(self.actor, "case_img_test", "img/portfolio/b.svg", "up"))

    def test_set_cover_requires_image_to_already_be_in_gallery(self):
        self.assertFalse(content_store.set_case_cover(self.actor, "case_img_test", "img/portfolio/not-there.svg"))
        self.assertEqual(self._case()["cover"], "img/portfolio/a.svg")
        content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        self.assertTrue(content_store.set_case_cover(self.actor, "case_img_test", "img/portfolio/b.svg"))
        self.assertEqual(self._case()["cover"], "img/portfolio/b.svg")


class CaseSectionManagementTests(unittest.TestCase):
    """Part 1 ТЗ: гибкие sections вместо жёстких task/solution/result."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"
        content_store.add_case(
            self.actor, case_id="case_sec_test", title="Т", type_id="landing",
            cover="img/portfolio/a.svg", task="t", related_service=None,
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _case(self):
        return next(c for c in content_store.list_cases() if c["id"] == "case_sec_test")

    def test_add_gallery_section_stores_images_not_content(self):
        content_store.add_case_section(
            self.actor, "case_sec_test", section_type="gallery", title="Скриншоты",
            images=["img/portfolio/x.svg"],
        )
        section = self._case()["sections"][0]
        self.assertEqual(section["images"], ["img/portfolio/x.svg"])
        self.assertNotIn("content", section)

    def test_add_text_section_stores_content(self):
        content_store.add_case_section(self.actor, "case_sec_test", section_type="text", title="Задача", content="Описание")
        section = self._case()["sections"][0]
        self.assertEqual(section["content"], "Описание")

    def test_update_delete_reorder_sections(self):
        content_store.add_case_section(self.actor, "case_sec_test", section_type="text", title="Первая", content="A")
        content_store.add_case_section(self.actor, "case_sec_test", section_type="text", title="Вторая", content="B")

        content_store.update_case_section(self.actor, "case_sec_test", 0, title="Первая (изменено)")
        self.assertEqual(self._case()["sections"][0]["title"], "Первая (изменено)")

        self.assertTrue(content_store.reorder_case_section(self.actor, "case_sec_test", 0, "down"))
        self.assertEqual([s["title"] for s in self._case()["sections"]], ["Вторая", "Первая (изменено)"])
        self.assertFalse(content_store.reorder_case_section(self.actor, "case_sec_test", 0, "up"))

        self.assertTrue(content_store.delete_case_section(self.actor, "case_sec_test", 0))
        self.assertEqual(len(self._case()["sections"]), 1)
        self.assertFalse(content_store.delete_case_section(self.actor, "case_sec_test", 5))


class LeadStoreTests(unittest.TestCase):
    """Part 6-7 ТЗ: заявки — upsert по draft_id (без дублей при "Дополнить
    информацию"), фильтрация по статусу, требование прав на смену статуса."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"
        self.telegram = {"user_id": 42, "username": None, "first_name": "Аня", "last_name": None}

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_lead_without_draft_id_always_creates_a_new_lead(self):
        lead1 = content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        lead2 = content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        self.assertNotEqual(lead1["id"], lead2["id"])
        self.assertEqual(len(content_store.list_leads()), 2)

    def test_add_lead_with_matching_draft_id_updates_instead_of_duplicating(self):
        first = content_store.add_lead({"service_name": "Лендинг"}, self.telegram, draft_id="draft-abc")
        self.assertIsNone(first["updated_at"])

        second = content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "доп. инфо"}, self.telegram, draft_id="draft-abc",
        )

        self.assertEqual(second["id"], first["id"], "\"Дополнить информацию\" не должно создавать вторую заявку")
        self.assertIsNotNone(second["updated_at"])
        self.assertEqual(len(content_store.list_leads()), 1)
        self.assertEqual(second["payload"]["task_description"], "доп. инфо")

    def test_add_lead_with_different_draft_id_creates_separate_lead(self):
        content_store.add_lead({"service_name": "Лендинг"}, self.telegram, draft_id="draft-1")
        content_store.add_lead({"service_name": "Сайт"}, self.telegram, draft_id="draft-2")
        self.assertEqual(len(content_store.list_leads()), 2)

    def test_list_leads_filters_by_status_and_sorts_newest_first(self):
        lead1 = content_store.add_lead({"service_name": "A"}, self.telegram)
        lead2 = content_store.add_lead({"service_name": "B"}, self.telegram)
        content_store.update_lead_status(self.actor, lead2["id"], "IN_PROGRESS")

        all_leads = content_store.list_leads()
        self.assertEqual([l["id"] for l in all_leads], [lead2["id"], lead1["id"]])

        new_only = content_store.list_leads("NEW")
        self.assertEqual([l["id"] for l in new_only], [lead1["id"]])

    def test_update_lead_status_rejects_unknown_status_and_requires_designer(self):
        lead = content_store.add_lead({"service_name": "A"}, self.telegram)
        self.assertFalse(content_store.update_lead_status(self.actor, lead["id"], "BOGUS"))
        with self.assertRaises(content_store.NotDesignerError):
            content_store.update_lead_status("not-the-designer", lead["id"], "DONE")

    def test_get_lead_returns_none_for_unknown_id(self):
        self.assertIsNone(content_store.get_lead(999999))


class ParseNumberBoundsTests(unittest.TestCase):
    """UX-аудит, находка F12: опечатка в /admin не должна попадать в
    клиент-facing цену как отрицательное число; round_to=0 сломал бы
    формулу вилки цены (используется как делитель) для всех клиентов сразу."""

    def test_negative_rejected_by_default(self):
        self.assertIsNone(admin._parse_number("-500"))

    def test_zero_allowed_by_default(self):
        self.assertEqual(admin._parse_number("0"), 0)

    def test_positive_still_parses(self):
        self.assertEqual(admin._parse_number("25000"), 25000)

    def test_round_to_rejects_zero_via_stricter_min_value(self):
        self.assertIsNone(admin._parse_number("0", min_value=0.01))
        self.assertEqual(admin._parse_number("500", min_value=0.01), 500)


def _sign_init_data(fields: dict, bot_token: str) -> str:
    """Независимая от bot/telegram_auth.py конструкция валидной initData —
    по тому же документированному алгоритму Telegram, но написана отдельно
    здесь, чтобы тест реально проверял правильность реализации, а не просто
    проверял, что функция согласна сама с собой."""
    import hashlib as _hashlib
    import hmac as _hmac

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = _hmac.new(b"WebAppData", bot_token.encode("utf-8"), _hashlib.sha256).digest()
    h = _hmac.new(secret_key, check_string.encode("utf-8"), _hashlib.sha256).hexdigest()
    signed = dict(fields)
    signed["hash"] = h
    return urllib.parse.urlencode(signed)


class TelegramInitDataValidationTests(unittest.TestCase):
    """"Мои заявки" не должны доверять user_id, переданному напрямую —
    только проверенному через initData (см. Part 8 ТЗ). Это криптографии
    касается напрямую, поэтому проверяем и "правильная подпись проходит",
    и — важнее — все пути отказа."""

    BOT_TOKEN = "123456:test-token-not-real"

    def _valid_fields(self, user_id=777, auth_date=None):
        if auth_date is None:
            auth_date = int(time.time())
        return {
            "auth_date": str(auth_date),
            "query_id": "AAEtest",
            "user": json.dumps({"id": user_id, "first_name": "Клиент", "username": "client1"}),
        }

    def test_correctly_signed_init_data_is_accepted(self):
        init_data = _sign_init_data(self._valid_fields(user_id=42), self.BOT_TOKEN)
        user = telegram_auth.validate_init_data(init_data, self.BOT_TOKEN)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], 42)

    def test_tampered_user_id_is_rejected(self):
        init_data = _sign_init_data(self._valid_fields(user_id=42), self.BOT_TOKEN)
        # Подменяем user на чужой ПОСЛЕ подписи (не пересчитывая hash) — как
        # если бы кто-то пытался запросить чужие заявки, просто изменив
        # параметр в уже сформированном запросе. Парсим/меняем/пересобираем
        # через тот же urlencode, а не строковый replace — иначе экранирование
        # может не совпасть и подмена молча не сработает.
        parsed = dict(urllib.parse.parse_qsl(init_data))
        parsed["user"] = json.dumps({"id": 999, "first_name": "Клиент", "username": "client1"})
        tampered = urllib.parse.urlencode(parsed)
        self.assertIsNone(telegram_auth.validate_init_data(tampered, self.BOT_TOKEN))

    def test_wrong_bot_token_is_rejected(self):
        init_data = _sign_init_data(self._valid_fields(), self.BOT_TOKEN)
        self.assertIsNone(telegram_auth.validate_init_data(init_data, "999999:another-token"))

    def test_missing_hash_is_rejected(self):
        fields = self._valid_fields()
        self.assertIsNone(telegram_auth.validate_init_data(urllib.parse.urlencode(fields), self.BOT_TOKEN))

    def test_expired_auth_date_is_rejected(self):
        old = int(time.time()) - 3 * 86400  # 3 дня назад
        init_data = _sign_init_data(self._valid_fields(auth_date=old), self.BOT_TOKEN)
        self.assertIsNone(telegram_auth.validate_init_data(init_data, self.BOT_TOKEN, max_age_seconds=86400))

    def test_empty_or_garbage_input_is_rejected_not_crashing(self):
        self.assertIsNone(telegram_auth.validate_init_data("", self.BOT_TOKEN))
        self.assertIsNone(telegram_auth.validate_init_data("not a valid query string ===", self.BOT_TOKEN))
        self.assertIsNone(telegram_auth.validate_init_data("hash=abc&auth_date=123", self.BOT_TOKEN))

    def test_validly_signed_data_with_no_user_field_is_rejected(self):
        # Подпись верна, но самого пользователя в пакете нет (например,
        # initData от игры/inline-режима, а не от Mini App с юзером) —
        # без user нечего проверять на "чьи это заявки".
        fields = {"auth_date": str(int(time.time())), "query_id": "AAEtest"}
        init_data = _sign_init_data(fields, self.BOT_TOKEN)
        self.assertIsNone(telegram_auth.validate_init_data(init_data, self.BOT_TOKEN))

    def test_corrupted_user_json_is_rejected_not_crashing(self):
        fields = {"auth_date": str(int(time.time())), "user": "{not valid json"}
        init_data = _sign_init_data(fields, self.BOT_TOKEN)
        self.assertIsNone(telegram_auth.validate_init_data(init_data, self.BOT_TOKEN))


class MyLeadsFilteringTests(unittest.TestCase):
    """list_leads_by_user — User A никогда не должен получить заявки User B."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_user_a_does_not_see_user_b_leads(self):
        lead_a = content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 111, "username": "a"})
        lead_b = content_store.add_lead({"service_name": "Сайт"}, {"user_id": 222, "username": "b"})

        leads_for_a = content_store.list_leads_by_user(111)
        leads_for_b = content_store.list_leads_by_user(222)

        self.assertEqual([l["id"] for l in leads_for_a], [lead_a["id"]])
        self.assertEqual([l["id"] for l in leads_for_b], [lead_b["id"]])
        self.assertNotIn(lead_b["id"], [l["id"] for l in leads_for_a])

    def test_unknown_user_gets_empty_list_not_error(self):
        content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 111, "username": "a"})
        self.assertEqual(content_store.list_leads_by_user(999999), [])

    def test_leads_sorted_newest_first(self):
        first = content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        second = content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        leads = content_store.list_leads_by_user(111)
        self.assertEqual([l["id"] for l in leads], [second["id"], first["id"]])


class MyLeadsHttpEndpointTests(unittest.IsolatedAsyncioTestCase):
    """/api/my-leads через реальный aiohttp-хендлер (не мок логики) — с
    правильной initData отдаёт только свои заявки, без нее — 401, не 500."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_token = webserver.config.BOT_TOKEN
        content_store.DATA_DIR = Path(self.tmpdir)
        webserver.config.BOT_TOKEN = "123456:test-token-not-real"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        webserver.config.BOT_TOKEN = self._orig_token
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_endpoint_returns_only_authenticated_users_leads(self):
        from aiohttp.test_utils import TestClient, TestServer

        content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 555, "username": "me"})
        content_store.add_lead({"service_name": "Сайт"}, {"user_id": 666, "username": "other"})

        init_data = _sign_init_data(
            {"auth_date": str(int(time.time())), "user": json.dumps({"id": 555, "first_name": "Я"})},
            webserver.config.BOT_TOKEN,
        )

        app = webserver.create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": init_data})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(len(body), 1)
            self.assertEqual(body[0]["payload"]["service_name"], "Лендинг")

            resp_no_auth = await client.get("/api/my-leads")
            self.assertEqual(resp_no_auth.status, 401)

    async def test_empty_init_data_header_is_401_not_500(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": ""})
            self.assertEqual(resp.status, 401)

    async def test_corrupted_init_data_header_is_401_not_500(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": "garbage=%%%not-valid"})
            self.assertEqual(resp.status, 401)

    async def test_debug_headers_do_not_affect_auth_decision(self):
        """Диагностические заголовки (платформа/версия/наличие hash) — это
        просто для логов, они НЕ должны влиять на решение сервера впустить
        или отклонить запрос, даже если специально подделаны."""
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/my-leads",
                headers={
                    "X-Telegram-Init-Data": "",
                    "X-Debug-Platform": "ios",
                    "X-Debug-Version": "99.0",
                    "X-Debug-Has-Hash": "true",
                },
            )
            self.assertEqual(resp.status, 401)  # диагностика не открыла доступ


if __name__ == "__main__":
    unittest.main()
