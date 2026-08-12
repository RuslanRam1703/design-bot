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

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot.content_store as content_store
import bot.handlers.admin as admin
import bot.handlers.faq as faq
import bot.handlers.webapp as webapp
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


if __name__ == "__main__":
    unittest.main()
