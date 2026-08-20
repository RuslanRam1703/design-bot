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
from aiogram.utils.web_app import check_webapp_signature as aiogram_check_webapp_signature

import bot.admin_keyboards as kb
import bot.content_store as content_store
import bot.handlers.admin as admin
import bot.handlers.faq as faq
import bot.handlers.webapp as webapp
import bot.handlers.start as start
import bot.flow as flow
import bot.keyboards as keyboards
import bot.telegram_auth as telegram_auth
import bot.texts as texts
import bot.webserver as webserver
from bot import lead as lead_format
from bot.states import AdminStates


def make_state(chat_id: int = 555) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=chat_id, user_id=chat_id))


def make_message(document=None, photo=None) -> SimpleNamespace:
    """Достаточно для _handle_brief_submission/handle_tz_file: message.from_user,
    message.bot (async send_message), message.answer (async) — сам объект не
    обязан быть настоящим aiogram Message, потому что типовые аннотации в
    рантайме не проверяются. first_name/last_name присутствуют, т.к. реальный
    aiogram User их всегда отдаёт (first_name обязателен по Telegram Bot API,
    last_name — опционален, но атрибут есть всегда, просто может быть None).
    document/photo по умолчанию None — как у настоящего aiogram Message,
    когда в сообщении нет файла/фото соответственно."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="client", first_name="Клиент", last_name=None),
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
        forward=AsyncMock(),
        document=document,
        photo=photo,
    )


def make_fake_document(file_id: str = "fake-file-id", file_unique_id: str = "fake-file-unique-id") -> SimpleNamespace:
    return SimpleNamespace(file_id=file_id, file_unique_id=file_unique_id)


class BriefLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """P0 из аудита: awaiting_tz_file — теперь persistent-поле самой заявки
    (content_store), не FSM — переживает restart/redeploy (тот же Upstash-
    слой, что и вся заявка), и не должно переживать новую заявку без
    "пришлю файл", и не должно путать заявки разных пользователей."""

    def setUp(self):
        # _handle_brief_submission пишет заявку через content_store.add_lead
        # (см. Part 6-7 ТЗ) — без подмены DATA_DIR тесты писали бы тестовые
        # заявки в настоящий data/leads.json.
        self.tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_second_submission_without_tz_clears_stale_awaiting_state(self):
        message = make_message()

        # Заявка (draft_id="d1"): "пришлю файл" -> заявка должна ждать файл.
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d1"})
        self.assertIsNotNone(content_store.find_lead_awaiting_file(message.from_user.id))

        # Тот же draft_id (клиент передумал через "Дополнить информацию" и
        # убрал "пришлю файл") -> upsert той же заявки, флаг должен сняться,
        # а не остаться висеть от предыдущей отправки.
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": False, "draft_id": "d1"})
        self.assertIsNone(content_store.find_lead_awaiting_file(message.from_user.id))

    async def test_repeat_submission_with_tz_again_still_ends_clean_without_file(self):
        message = make_message()

        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d2"})
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d2"})
        self.assertIsNotNone(content_store.find_lead_awaiting_file(message.from_user.id))

        # Присланный файл закрывает ожидание (тот же путь, что и в проде).
        message.document = make_fake_document()
        await webapp.handle_tz_file(message)
        self.assertIsNone(content_store.find_lead_awaiting_file(message.from_user.id))

    async def test_file_from_different_user_does_not_close_someone_elses_wait(self):
        # Security: файл от чужого user_id не должен закрывать чужое
        # ожидание и не должен пересылаться дизайнеру как relevant TЗ.
        owner_message = make_message()  # from_user.id == 1
        await webapp._handle_brief_submission(
            owner_message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d3"}
        )

        stranger_message = make_message(document=make_fake_document())
        stranger_message.from_user = SimpleNamespace(id=999, username="stranger", first_name="Чужой", last_name=None)
        await webapp.handle_tz_file(stranger_message)

        self.assertIsNotNone(content_store.find_lead_awaiting_file(1))  # заявка владельца всё ещё ждёт файл
        stranger_message.forward.assert_not_awaited()
        stranger_message.answer.assert_not_awaited()  # чужому отправителю тоже ничего не отвечаем

    async def test_awaiting_state_survives_simulated_restart(self):
        # "Persistence через restart/redeploy" — здесь эмулируется тем, что
        # find_lead_awaiting_file каждый раз читает данные заново с диска
        # (_read_leads), а не держит что-то в памяти процесса, как раньше
        # делал FSM MemoryStorage (терялось при рестарте бота).
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d4"})

        # "Рестарт" — работаем со свежим вызовом, ничего не переиспользуем
        # из предыдущего процесса/переменных, кроме самого файла на диске.
        lead = content_store.find_lead_awaiting_file(message.from_user.id)
        self.assertIsNotNone(lead)
        self.assertTrue(lead["awaiting_tz_file"])


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

        await webapp.handle_webapp_data(message)

        message.bot.send_message.assert_awaited_once()
        text = message.bot.send_message.await_args.kwargs["text"]

        self.assertIn("Сайт", text)
        self.assertIn("кейс «Сайт для частной стоматологии»", text)
        self.assertIn("40 000", text)  # цена из расчёта попала в уведомление
        self.assertIn("Бюджет:</b> 40 000–70 000 ₽", text)
        self.assertNotIn("ТЗ:</b> клиент пришлёт файл", text)  # attach_tz=False
        message.answer.assert_awaited_once()
        self.assertIsNone(content_store.find_lead_awaiting_file(message.from_user.id))  # attach_tz=False -> ничего не ждём

    async def test_direct_brief_has_no_source_noise_in_notification(self):
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

        await webapp.handle_webapp_data(message)

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertNotIn("Источник", text)  # "direct" осознанно не показываем — см. lead.py
        self.assertIsNotNone(content_store.find_lead_awaiting_file(message.from_user.id))  # attach_tz=True


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


def make_callback(data: str, chat_id: int = 777, bot: AsyncMock | None = None) -> SimpleNamespace:
    """Достаточно для admin.py callback-хендлеров: callback.data,
    callback.message.chat.id, callback.message.edit_text (async),
    callback.answer (async), callback.bot (async send_message — нужен
    lead_change_status для уведомления клиента о смене статуса)."""
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), edit_text=AsyncMock()),
        answer=AsyncMock(),
        bot=bot if bot is not None else AsyncMock(),
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
        # В приватном чате с ботом chat_id == from_user.id всегда — тот же
        # принцип использует cmd_cancel (content_store.find_lead_awaiting_file).
        from_user=SimpleNamespace(id=chat_id, username=None, first_name="Тест", last_name=None),
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

    async def test_refresh_reply_keyboard_sends_invisible_text_and_deletes_it(self):
        from aiogram.types import ReplyKeyboardMarkup

        markup = ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True)
        msg = make_flow_message()

        await flow.refresh_reply_keyboard(msg, markup)

        msg.answer.assert_awaited_once()
        call = msg.answer.await_args
        sent_text = call.args[0] if call.args else call.kwargs.get("text")
        self.assertEqual(sent_text, "​")
        self.assertIs(call.kwargs.get("reply_markup"), markup)
        # answer() в make_flow_message всегда возвращает message_id=555 —
        # именно это сообщение и должно быть удалено.
        msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=555)

    async def test_refresh_reply_keyboard_survives_send_failure(self):
        from aiogram.types import ReplyKeyboardMarkup

        markup = ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True)
        msg = make_flow_message()
        msg.answer = AsyncMock(side_effect=TelegramAPIError(method=None, message="blocked by user"))

        await flow.refresh_reply_keyboard(msg, markup)  # не должно упасть

        msg.bot.delete_message.assert_not_awaited()  # нечего удалять — отправка не удалась

    async def test_refresh_reply_keyboard_survives_delete_failure(self):
        from aiogram.types import ReplyKeyboardMarkup

        markup = ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True)
        msg = make_flow_message()
        msg.bot.delete_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="message not found"))

        await flow.refresh_reply_keyboard(msg, markup)  # не должно упасть — best-effort

        msg.answer.assert_awaited_once()


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

        # cmd_portfolio теперь дополнительно вызывает refresh_reply_keyboard
        # (см. bot/flow.py), которая сама шлёт и best-effort удаляет ещё одно
        # сообщение — delete_message вызывается больше одного раза, но
        # RULE 2 (удаление старого корневого экрана) всё равно должна была
        # сработать среди этих вызовов.
        msg2.bot.delete_message.assert_any_await(chat_id=888, message_id=555)

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


class EntryPointArchitectureTests(unittest.IsolatedAsyncioTestCase):
    """Итоговая архитектура: постоянная reply-клавиатура (обычные текстовые
    кнопки — RULE: НИ ОДНА не web_app, реальный Telegram не передаёт
    initData для KeyboardButton.web_app, подтверждено production-тестами),
    "🚀 Открыть приложение" — триггер, который в ответ шлёт inline
    web_app-кнопку (единственный подтверждённо рабочий launch-механизм)."""

    def setUp(self):
        self._orig_designer = start.config.DESIGNER_CHAT_ID
        start.config.DESIGNER_CHAT_ID = "888"
        # cmd_cancel теперь читает content_store.find_lead_awaiting_file, а
        # admin_button -> cmd_admin читает portfolio/faq/about для сводки —
        # изолируем DATA_DIR (с сидированием реальных файлов), чтобы не
        # трогать настоящий data/*.json.
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        start.config.DESIGNER_CHAT_ID = self._orig_designer
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_main_menu_keyboard_no_longer_exists(self):
        # Старое имя удалённой inline-двух-кнопочной клавиатуры — не должно
        # существовать ни под старым, ни под предыдущим промежуточным именем.
        self.assertFalse(hasattr(keyboards, "main_menu_keyboard"))
        self.assertFalse(hasattr(keyboards, "main_entry_keyboard"))

    def test_main_reply_keyboard_has_no_web_app_buttons(self):
        from aiogram.types import ReplyKeyboardMarkup

        for is_owner in (False, True):
            markup = keyboards.main_reply_keyboard(is_owner=is_owner)
            self.assertIsInstance(markup, ReplyKeyboardMarkup)
            for row in markup.keyboard:
                for btn in row:
                    self.assertIsNone(btn.web_app, f"{btn.text!r} не должна быть web_app-кнопкой")

    def test_main_reply_keyboard_client_vs_owner_buttons(self):
        client_texts = {btn.text for row in keyboards.main_reply_keyboard(is_owner=False).keyboard for btn in row}
        owner_texts = {btn.text for row in keyboards.main_reply_keyboard(is_owner=True).keyboard for btn in row}
        self.assertEqual(client_texts, {texts.OPEN_APP_BUTTON, texts.MENU_FAQ})
        self.assertEqual(owner_texts, {texts.OPEN_APP_BUTTON, texts.MENU_FAQ, texts.ADMIN_BUTTON})

    def test_reply_keyboard_for_chat_picks_correct_variant(self):
        # Общий helper (bot/keyboards.py) — используется и в start.py, и в
        # faq.py, и в bot/flow.py::refresh_reply_keyboard. Сравнение с
        # DESIGNER_CHAT_ID — та же проверка, что и в
        # bot/handlers/admin.py::_is_designer_message, но здесь она только
        # выбирает, что показать, не влияет на авторизацию.
        owner_markup = keyboards.reply_keyboard_for_chat(888)
        client_markup = keyboards.reply_keyboard_for_chat(999)
        owner_texts_seen = {btn.text for row in owner_markup.keyboard for btn in row}
        client_texts_seen = {btn.text for row in client_markup.keyboard for btn in row}
        self.assertIn(texts.ADMIN_BUTTON, owner_texts_seen)
        self.assertNotIn(texts.ADMIN_BUTTON, client_texts_seen)

    async def test_cmd_start_sends_reply_keyboard_without_web_app(self):
        from aiogram.types import ReplyKeyboardMarkup

        state = make_state()
        msg = make_flow_message(text="/start")
        await start.cmd_start(msg, state)
        sent_markup = msg.answer.await_args.kwargs.get("reply_markup") or msg.answer.await_args.args[1]
        self.assertIsInstance(sent_markup, ReplyKeyboardMarkup)
        for row in sent_markup.keyboard:
            for btn in row:
                self.assertIsNone(btn.web_app)

    async def test_cmd_start_shows_admin_button_only_to_owner(self):
        owner_msg = make_flow_message(chat_id=888, text="/start")
        await start.cmd_start(owner_msg, make_state(888))
        owner_markup = owner_msg.answer.await_args.kwargs.get("reply_markup") or owner_msg.answer.await_args.args[1]
        owner_texts_seen = {btn.text for row in owner_markup.keyboard for btn in row}
        self.assertIn(texts.ADMIN_BUTTON, owner_texts_seen)

        client_msg = make_flow_message(chat_id=999, text="/start")
        await start.cmd_start(client_msg, make_state(999))
        client_markup = client_msg.answer.await_args.kwargs.get("reply_markup") or client_msg.answer.await_args.args[1]
        client_texts_seen = {btn.text for row in client_markup.keyboard for btn in row}
        self.assertNotIn(texts.ADMIN_BUTTON, client_texts_seen)

    async def test_open_app_button_replies_with_inline_webapp_button(self):
        from aiogram.types import InlineKeyboardMarkup, WebAppInfo

        state = make_state()
        msg = make_flow_message(text=texts.OPEN_APP_BUTTON)
        await start.open_app_button(msg, state)
        # Первый вызов answer() — содержательный ответ (inline web_app-кнопка);
        # второй — best-effort "освежение" reply-клавиатуры (см.
        # bot/flow.py::refresh_reply_keyboard), проверяется отдельным тестом.
        first_call = msg.answer.await_args_list[0]
        sent_markup = first_call.kwargs.get("reply_markup") or first_call.args[1]
        self.assertIsInstance(sent_markup, InlineKeyboardMarkup)
        btn = sent_markup.inline_keyboard[0][0]
        self.assertIsInstance(btn.web_app, WebAppInfo)
        self.assertTrue(btn.web_app.url.endswith("/portfolio"))

    async def test_admin_button_triggers_same_as_admin_command(self):
        state = make_state(888)
        msg = make_flow_message(chat_id=888, text=texts.ADMIN_BUTTON)
        await admin.admin_button(msg, state)
        msg.answer.assert_awaited_once()

    def test_client_commands_include_faq_and_no_calculator(self):
        import bot.main as bot_main

        command_names = [c.command for c in bot_main.CLIENT_COMMANDS]
        self.assertEqual(command_names, ["start", "faq", "portfolio", "about", "brief"])

    def test_owner_command_scope_is_client_commands_plus_admin(self):
        import bot.main as bot_main

        owner_names = [c.command for c in bot_main.CLIENT_COMMANDS + bot_main.ADMIN_EXTRA_COMMANDS]
        self.assertEqual(owner_names, ["start", "faq", "portfolio", "about", "brief", "admin"])

    async def test_setup_menu_button_is_commands_not_webapp(self):
        # Регресс commit ac09080: MenuButtonWebApp здесь подменял системное
        # Telegram Menu (список команд) на кнопку запуска Mini App. Menu
        # должно оставаться обычным списком команд — запуск Mini App теперь
        # только через reply-кнопку "🚀 Открыть приложение" + inline-кнопки.
        from aiogram.types import MenuButtonCommands, MenuButtonWebApp

        import bot.main as bot_main

        fake_bot = AsyncMock()
        await bot_main._setup_menu_button(fake_bot)
        fake_bot.set_chat_menu_button.assert_awaited_once()
        _, call_kwargs = fake_bot.set_chat_menu_button.call_args
        menu_button = call_kwargs["menu_button"]
        self.assertIsInstance(menu_button, MenuButtonCommands)
        self.assertNotIsInstance(menu_button, MenuButtonWebApp)

    def test_client_reply_keyboard_has_no_admin_button(self):
        client_texts = {btn.text for row in keyboards.main_reply_keyboard(is_owner=False).keyboard for btn in row}
        self.assertEqual(client_texts, {texts.OPEN_APP_BUTTON, texts.MENU_FAQ})
        self.assertNotIn(texts.ADMIN_BUTTON, client_texts)

    def test_startup_does_not_drop_pending_updates(self):
        # См. production-hardening аудит: drop_pending_updates=True на каждом
        # старте (Render rolling-деплой) безусловно отбрасывал бы реальные
        # клиентские апдейты (сообщения/файлы/callback-и), попавшие в узкое
        # окно, когда новый инстанс перекрывается со старым. main() нельзя
        # безопасно вызвать напрямую в тесте — он реально стартует aiohttp-
        # сервер и блокируется в бесконечном polling-цикле, а рефакторить его
        # ради тестируемости здесь явно не просили, поэтому проверяем сам
        # исходный код функции — этого достаточно, чтобы не дать значению
        # тихо откатиться обратно на True при будущей правке.
        import inspect

        import bot.main as bot_main

        source = inspect.getsource(bot_main.main)
        self.assertIn("delete_webhook(drop_pending_updates=False)", source)
        self.assertNotIn("delete_webhook(drop_pending_updates=True)", source)

    def test_owner_reply_keyboard_has_admin_button(self):
        owner_texts = {btn.text for row in keyboards.main_reply_keyboard(is_owner=True).keyboard for btn in row}
        self.assertIn(texts.ADMIN_BUTTON, owner_texts)
        self.assertIn(texts.OPEN_APP_BUTTON, owner_texts)
        self.assertIn(texts.MENU_FAQ, owner_texts)

    def test_no_keyboard_button_uses_web_app_anywhere_in_bot_package(self):
        # Статическая проверка исходников всего bot/ — не только
        # main_reply_keyboard(), но вообще нигде в проекте не должно
        # остаться KeyboardButton(web_app=...) (см. регресс, из-за которого
        # эта задача вообще начиналась несколько итераций назад).
        import re

        bot_dir = Path(__file__).resolve().parent.parent / "bot"
        # Ищем реальный вызов конструктора (web_app=WebAppInfo(...)), а не
        # упоминание в докстринге/комментарии (там пишем "web_app=..." как
        # прозу, объясняющую, чего быть не должно — это не код).
        pattern = re.compile(r"(?<!Inline)KeyboardButton\([^)]*web_app\s*=\s*WebAppInfo")
        offenders = []
        for path in bot_dir.rglob("*.py"):
            text_content = path.read_text(encoding="utf-8")
            if pattern.search(text_content):
                offenders.append(str(path))
        self.assertEqual(offenders, [])

    def test_reply_keyboard_is_persistent(self):
        # Регресс: без is_persistent=True Telegram-клиент вправе скрыть
        # reply-клавиатуру после любого сообщения с inline-разметкой
        # (/portfolio, /about, /brief, /faq, "🚀 Открыть приложение" —
        # все отвечают inline, одно сообщение не может нести оба типа).
        for is_owner in (False, True):
            markup = keyboards.main_reply_keyboard(is_owner=is_owner)
            self.assertTrue(markup.is_persistent)

    async def test_start_sends_persistent_keyboard_client_and_owner(self):
        client_msg = make_flow_message(chat_id=999, text="/start")
        await start.cmd_start(client_msg, make_state(999))
        client_markup = client_msg.answer.await_args.kwargs.get("reply_markup") or client_msg.answer.await_args.args[1]
        self.assertTrue(client_markup.is_persistent)

        owner_msg = make_flow_message(chat_id=888, text="/start")
        await start.cmd_start(owner_msg, make_state(888))
        owner_markup = owner_msg.answer.await_args.kwargs.get("reply_markup") or owner_msg.answer.await_args.args[1]
        self.assertTrue(owner_markup.is_persistent)

    async def test_cancel_sends_persistent_reply_keyboard(self):
        state = make_state()
        msg = make_flow_message(text="/cancel")
        await start.cmd_cancel(msg, state)
        sent_markup = msg.answer.await_args.kwargs.get("reply_markup") or msg.answer.await_args.args[1]
        self.assertTrue(sent_markup.is_persistent)

    async def test_portfolio_about_brief_still_use_inline_webapp_keyboard(self):
        # Не меняли и не должны были менять поведение inline WebApp-кнопок —
        # эти хендлеры по-прежнему ПЕРВЫМ сообщением отвечают
        # InlineKeyboardMarkup с web_app; ВТОРЫМ сообщением (best-effort,
        # см. bot/flow.py::refresh_reply_keyboard) — освежают
        # reply-клавиатуру, т.к. Telegram разрешает только один reply_markup
        # на сообщение и не может нести оба типа сразу.
        from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, WebAppInfo

        cases = [
            (start.cmd_portfolio, "portfolio"),
            (start.cmd_about, "about"),
            (start.cmd_brief, "brief"),
        ]
        for handler, path in cases:
            state = make_state()
            msg = make_flow_message(text=f"/{path}")
            await handler(msg, state)
            self.assertEqual(msg.answer.await_count, 2)
            first_call, second_call = msg.answer.await_args_list
            sent_markup = first_call.kwargs.get("reply_markup") or first_call.args[1]
            self.assertIsInstance(sent_markup, InlineKeyboardMarkup)
            btn = sent_markup.inline_keyboard[0][0]
            self.assertIsInstance(btn.web_app, WebAppInfo)
            refresh_markup = second_call.kwargs.get("reply_markup") or second_call.args[1]
            self.assertIsInstance(refresh_markup, ReplyKeyboardMarkup)
            self.assertTrue(btn.web_app.url.endswith(f"/{path}"))

    async def test_faq_command_still_uses_inline_faq_keyboard(self):
        # /faq тоже теперь шлёт второе, best-effort сообщение для
        # "освежения" reply-клавиатуры (см. bot/flow.py::refresh_reply_keyboard,
        # вызывается из faq.py::_send_faq_list) — answer() вызывается дважды.
        from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

        msg = make_flow_message(text="/faq")
        await faq.cmd_faq(msg)
        self.assertEqual(msg.answer.await_count, 2)
        first_call, second_call = msg.answer.await_args_list
        sent_markup = first_call.kwargs.get("reply_markup") or first_call.args[1]
        self.assertIsInstance(sent_markup, InlineKeyboardMarkup)
        refresh_markup = second_call.kwargs.get("reply_markup") or second_call.args[1]
        self.assertIsInstance(refresh_markup, ReplyKeyboardMarkup)

    async def test_open_app_button_stays_plain_text_not_web_app(self):
        # main_reply_keyboard() уже проверяется на отсутствие web_app в
        # других тестах — здесь отдельно, явно, именно для кнопки-триггера
        # "🚀 Открыть приложение", как запрошено отдельным пунктом.
        markup = keyboards.main_reply_keyboard(is_owner=False)
        trigger_btn = next(
            btn for row in markup.keyboard for btn in row if btn.text == texts.OPEN_APP_BUTTON
        )
        self.assertIsNone(trigger_btn.web_app)


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
    {"result": ...}, ровно как настоящий Upstash REST.

    fail_on — опциональный набор (cmd, key) пар, на которых urlopen должен
    вместо ответа поднять исключение (симуляция сетевого сбоя/ошибки
    Upstash) — используется тестами P0-1 storage-инициализации, чтобы
    проверить, что MARKER_KEY не выставляется при сбое GET/SET."""

    def __init__(self, fail_on: set[tuple[str, str]] | None = None):
        self.store: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.fail_on = fail_on or set()

    def urlopen(self, req, timeout=10):
        args = json.loads(req.data.decode("utf-8"))
        self.calls.append(tuple(args))
        cmd = args[0]
        if (cmd, args[1]) in self.fail_on:
            raise ConnectionError(f"simulated Upstash failure on {args[:2]}")
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


class StorageInitializationTests(unittest.TestCase):
    """ensure_storage_initialized (P0-1, production-hardening аудит) —
    eager batch-сид всех DATA_FILENAMES под единым persistent MARKER_KEY:
    новая Upstash-база сеется целиком и сразу; уже проинициализированная —
    никогда не реседится по одному ключу, если тот вдруг пропал (защита от
    тихой потери production-данных при сбое/ошибке конфигурации Upstash —
    см. UpstashKeyMissingError)."""

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

    def test_empty_new_database_seeds_all_and_sets_marker(self):
        content_store.ensure_storage_initialized()
        for filename in content_store.DATA_FILENAMES:
            self.assertIn(filename, self.fake.store)
        self.assertIn(content_store.MARKER_KEY, self.fake.store)

    def test_partially_initialized_database_seeds_only_missing_keys(self):
        self.fake.store["leads.json"] = json.dumps({"leads": [{"id": 999, "custom": True}]})
        content_store.ensure_storage_initialized()
        # уже существовавший ключ не тронут
        self.assertEqual(json.loads(self.fake.store["leads.json"]), {"leads": [{"id": 999, "custom": True}]})
        # остальные — досеяны
        for filename in content_store.DATA_FILENAMES:
            self.assertIn(filename, self.fake.store)
        self.assertIn(content_store.MARKER_KEY, self.fake.store)

    def test_existing_production_data_without_marker_is_untouched(self):
        # Симулируем уже давно работающий продакшн: реальные данные во всех
        # 6 ключах, но marker ещё не существовал (появился только в этом
        # фиксе) — критический тест безопасной миграции, см. аудит.
        original = {}
        for filename in content_store.DATA_FILENAMES:
            value = json.dumps({"marker_test_sentinel": filename})
            self.fake.store[filename] = value
            original[filename] = value

        content_store.ensure_storage_initialized()

        for filename in content_store.DATA_FILENAMES:
            self.assertEqual(self.fake.store[filename], original[filename])  # ни байта не изменилось
        self.assertIn(content_store.MARKER_KEY, self.fake.store)

    def test_marker_present_and_key_missing_raises_without_seeding(self):
        self.fake.store[content_store.MARKER_KEY] = "2026-01-01T00:00:00+00:00"
        with self.assertRaises(content_store.UpstashKeyMissingError):
            content_store._read("leads.json")
        self.assertNotIn("leads.json", self.fake.store)  # НЕ засеяно
        self.assertFalse(any(c[0] == "SET" for c in self.fake.calls))  # ни одного SET

    def test_marker_present_and_all_keys_present_reads_normally(self):
        self.fake.store[content_store.MARKER_KEY] = "2026-01-01T00:00:00+00:00"
        self.fake.store["ui_config.json"] = json.dumps({"menu": {"portfolio": True}})
        data = content_store._read("ui_config.json")
        self.assertEqual(data, {"menu": {"portfolio": True}})

    def test_invalid_json_in_existing_key_still_fails_loud(self):
        self.fake.store["ui_config.json"] = "{not valid json"
        with self.assertRaises(json.JSONDecodeError):
            content_store._read("ui_config.json")

    def test_network_failure_during_init_leaves_marker_unset(self):
        self.fake.fail_on = {("GET", "faq.json")}
        with self.assertRaises(ConnectionError):
            content_store.ensure_storage_initialized()
        self.assertNotIn(content_store.MARKER_KEY, self.fake.store)

    def test_set_failure_during_init_leaves_marker_unset(self):
        self.fake.fail_on = {("SET", "about.json")}
        with self.assertRaises(ConnectionError):
            content_store.ensure_storage_initialized()
        self.assertNotIn(content_store.MARKER_KEY, self.fake.store)

    def test_last_key_failure_still_prevents_marker(self):
        # DATA_FILENAMES = (portfolio, pricing, faq, about, ui_config, leads)
        # — валим именно последний (6-й) ключ цикла: даже если первые 5
        # успешно засеялись до него, marker всё равно не должен появиться.
        last_filename = content_store.DATA_FILENAMES[-1]
        self.fake.fail_on = {("SET", last_filename)}
        with self.assertRaises(ConnectionError):
            content_store.ensure_storage_initialized()
        self.assertNotIn(content_store.MARKER_KEY, self.fake.store)
        for filename in content_store.DATA_FILENAMES[:-1]:
            self.assertIn(filename, self.fake.store)  # первые 5 уже успели засеяться — это ожидаемо

    def test_upstash_disabled_is_noop(self):
        content_store.config.UPSTASH_REDIS_REST_URL = ""
        content_store.config.UPSTASH_REDIS_REST_TOKEN = ""
        content_store.ensure_storage_initialized()
        self.assertEqual(self.fake.calls, [])  # ни одного сетевого вызова — локальный dev не затронут

    def test_marker_key_not_in_backup_filenames(self):
        self.assertNotIn(content_store.MARKER_KEY, content_store.DATA_FILENAMES)


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


class InitDataDiagnosticsTests(unittest.TestCase):
    """diagnose_init_data — только для логов при отказе; должна точно
    показывать, какой из шагов проверки не прошёл, при этом сама НЕ
    участвует в решении validate_init_data (см. отдельные HTTP-тесты
    в MyLeadsHttpEndpointTests, что она не может открыть доступ)."""

    BOT_TOKEN = "123456:test-token-not-real"

    def test_valid_init_data_all_flags_true(self):
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": 42, "first_name": "Клиент"}),
        }
        init_data = _sign_init_data(fields, self.BOT_TOKEN)
        diag = telegram_auth.diagnose_init_data(init_data, self.BOT_TOKEN)
        self.assertTrue(diag.parse_ok)
        self.assertTrue(diag.hash_present)
        self.assertTrue(diag.hmac_valid)
        self.assertTrue(diag.auth_date_present)
        self.assertTrue(diag.auth_date_valid)
        self.assertTrue(diag.user_present)
        self.assertTrue(diag.user_json_ok)
        # validate_init_data и diagnose_init_data должны соглашаться друг с
        # другом на valid-случае — иначе диагностика вводит в заблуждение.
        self.assertIsNotNone(telegram_auth.validate_init_data(init_data, self.BOT_TOKEN))

    def test_bad_hmac_flagged_but_other_fields_still_computed(self):
        # Это ключевой сценарий этой задачи: initData присутствует и
        # непустая (как в реальном отчёте — initData_len=591), но подпись
        # не сходится. diagnose_init_data должна показать hmac_valid=False
        # И при этом всё равно посчитать auth_date/user — в отличие от
        # validate_init_data, которая на этом же месте останавливается.
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": 42, "first_name": "Клиент"}),
        }
        init_data = _sign_init_data(fields, self.BOT_TOKEN)
        tampered = init_data.replace(init_data.split("hash=")[1], "0" * 64)
        diag = telegram_auth.diagnose_init_data(tampered, self.BOT_TOKEN)
        self.assertTrue(diag.parse_ok)
        self.assertTrue(diag.hash_present)
        self.assertFalse(diag.hmac_valid)
        self.assertTrue(diag.auth_date_present)
        self.assertTrue(diag.auth_date_valid)
        self.assertTrue(diag.user_present)
        self.assertTrue(diag.user_json_ok)
        self.assertIsNone(telegram_auth.validate_init_data(tampered, self.BOT_TOKEN))

    def test_expired_auth_date_flagged_independently_of_user(self):
        old = int(time.time()) - 3 * 86400
        fields = {"auth_date": str(old), "user": json.dumps({"id": 42})}
        init_data = _sign_init_data(fields, self.BOT_TOKEN)
        diag = telegram_auth.diagnose_init_data(init_data, self.BOT_TOKEN, max_age_seconds=86400)
        self.assertTrue(diag.hmac_valid)
        self.assertTrue(diag.auth_date_present)
        self.assertFalse(diag.auth_date_valid)
        self.assertTrue(diag.user_present)
        self.assertTrue(diag.user_json_ok)

    def test_missing_user_flagged(self):
        fields = {"auth_date": str(int(time.time()))}
        init_data = _sign_init_data(fields, self.BOT_TOKEN)
        diag = telegram_auth.diagnose_init_data(init_data, self.BOT_TOKEN)
        self.assertTrue(diag.hmac_valid)
        self.assertFalse(diag.user_present)
        self.assertFalse(diag.user_json_ok)

    def test_corrupted_user_json_flagged(self):
        fields = {"auth_date": str(int(time.time())), "user": "{not valid json"}
        init_data = _sign_init_data(fields, self.BOT_TOKEN)
        diag = telegram_auth.diagnose_init_data(init_data, self.BOT_TOKEN)
        self.assertTrue(diag.user_present)
        self.assertFalse(diag.user_json_ok)

    def test_missing_hash_flagged_hmac_not_applicable(self):
        fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 1})}
        diag = telegram_auth.diagnose_init_data(urllib.parse.urlencode(fields), self.BOT_TOKEN)
        self.assertTrue(diag.parse_ok)
        self.assertFalse(diag.hash_present)
        self.assertIsNone(diag.hmac_valid)  # не "false" — проверка не применялась

    def test_empty_or_garbage_input_does_not_crash(self):
        self.assertEqual(
            telegram_auth.diagnose_init_data("", self.BOT_TOKEN),
            telegram_auth.InitDataDiagnostics(False, False, None, False, None, False, False, 0, 0, None, 0, None),
        )
        # Строка без "=" в одном из полей — то, что parse_qsl(strict_parsing=True)
        # реально отклоняет с ValueError (не любая "странная" строка ошибочна:
        # "a===b" — валидный query string с value "==b", это не наш случай).
        diag = telegram_auth.diagnose_init_data("a=1&bad_field_without_equals&c=2", self.BOT_TOKEN)
        self.assertFalse(diag.parse_ok)
        self.assertFalse(diag.hash_present)
        self.assertIsNone(diag.hmac_valid)


class RealisticInitDataCrossCheckTests(unittest.TestCase):
    """Сравнение нашей validate_init_data() с эталонной реализацией
    aiogram.utils.web_app.check_webapp_signature() на initData, максимально
    близкой к реальному формату Telegram Mini App (Bot API 7.x+): с полем
    signature (Ed25519, добавляется Telegram отдельно от hash) и обычным
    набором полей chat_instance/chat_type/query_id/user с URL-encoded JSON.
    Хэш в обоих случаях строится ПРАВИЛЬНО — по всем полям, кроме hash (как
    в aiogram, эталонной реализации того же документированного алгоритма);
    если наш validate_init_data при этом расходится с aiogram — это
    подтверждённое расхождение в нашей реализации, а не в тестовых данных."""

    BOT_TOKEN = "123456:test-token-not-real"

    @staticmethod
    def _sign_reference(fields: dict, bot_token: str) -> str:
        # Намеренно НЕ переиспользует bot/telegram_auth.py — иначе тест
        # проверял бы согласие функции самой с собой, а не с эталоном.
        import hashlib as _hashlib
        import hmac as _hmac

        check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret_key = _hmac.new(b"WebAppData", bot_token.encode("utf-8"), _hashlib.sha256).digest()
        h = _hmac.new(secret_key, check_string.encode("utf-8"), _hashlib.sha256).hexdigest()
        signed = dict(fields)
        signed["hash"] = h
        return urllib.parse.urlencode(signed)

    def _realistic_fields(self) -> dict:
        return {
            "auth_date": str(int(time.time())),
            "query_id": "AAEtest_query_id",
            "chat_instance": "-1234567890123456789",
            "chat_type": "sender",
            "user": json.dumps(
                {
                    "id": 42,
                    "first_name": "Клиент",
                    "last_name": "Тестов",
                    "username": "client1",
                    "language_code": "ru",
                    "allows_write_to_pm": True,
                },
                ensure_ascii=False,
            ),
        }

    def test_agrees_with_aiogram_without_signature_field(self):
        init_data = self._sign_reference(self._realistic_fields(), self.BOT_TOKEN)
        our_result = telegram_auth.validate_init_data(init_data, self.BOT_TOKEN)
        aiogram_result = aiogram_check_webapp_signature(self.BOT_TOKEN, init_data)
        self.assertTrue(aiogram_result)
        self.assertEqual(our_result is not None, aiogram_result)

    def test_agrees_with_aiogram_when_signature_field_present(self):
        # Регрессионный тест на реальный production-баг: реальный Telegram
        # (Bot API 7.x+) добавляет в Mini App initData поле "signature"
        # (Ed25519, для отдельного способа проверки без bot-token). Для
        # HMAC-метода (наш, bot-token) "signature" НЕ исключается из
        # data_check_string — исключается только "hash" (и hash, и
        # signature исключаются вместе только для ДРУГОГО, Ed25519-метода).
        # Раньше _parse_and_split() ошибочно удалял ещё и "signature" перед
        # вычислением хэша, из-за чего реальная initData с этим полем
        # (как в production, tdesktop 9.6) отклонялась как невалидная,
        # хотя эталонная реализация (aiogram) её принимает. Этот тест
        # фиксирует исправление — при регрессе снова начнёт падать.
        fields = self._realistic_fields()
        fields["signature"] = "MEUCIQC-fake_ed25519_signature_base64url_encoded_value-AAA"
        init_data = self._sign_reference(fields, self.BOT_TOKEN)

        our_result = telegram_auth.validate_init_data(init_data, self.BOT_TOKEN)
        aiogram_result = aiogram_check_webapp_signature(self.BOT_TOKEN, init_data)

        self.assertTrue(aiogram_result)  # эталонная реализация: initData валидна
        self.assertEqual(
            our_result is not None,
            aiogram_result,
            "validate_init_data() расходится с эталонной aiogram.check_webapp_signature() "
            "на initData с полем signature",
        )
        self.assertIsNotNone(our_result)
        self.assertEqual(our_result["id"], 42)


class MyLeadsFilteringTests(unittest.TestCase):
    """list_leads_by_user — User A никогда не должен получить заявки User B."""

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
        # Обе заявки ни разу не обновлялись (updated_at=None у обеих) —
        # сортировка падает на created_at, порядок по факту создания.
        first = content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        second = content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        leads = content_store.list_leads_by_user(111)
        self.assertEqual([l["id"] for l in leads], [second["id"], first["id"]])

    def test_updated_old_lead_rises_above_newer_untouched_lead(self):
        # См. UX-аудит "Мои заявки" — недавняя активность (статус/supplement/
        # owner_message/материал) должна поднимать заявку наверх, даже если
        # она создана раньше другой, нетронутой заявки.
        old_lead = content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        new_lead = content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        content_store.update_lead_status(self.actor, old_lead["id"], "IN_PROGRESS")

        # update_lead_status() уже реально отработал (сам факт простановки
        # updated_at проверяется отдельно, в других тестах) — но два
        # datetime.now(timezone.utc) вызова подряд в одном тесте иногда
        # совпадают до используемого разрешения системных часов, из-за чего
        # updated_at старой заявки может оказаться РАВЕН created_at новой —
        # тогда тай-брейк по id (осознанно реализованный) отдаёт победу
        # новой заявке, и тест стал бы flaky. Форсируем заведомо более
        # позднюю метку, чтобы порядок проверялся детерминированно.
        leads = content_store._read_leads()
        for l in leads:
            if l["id"] == old_lead["id"]:
                l["updated_at"] = "2030-01-01T00:00:00+00:00"
        content_store._write_leads(leads)

        leads = content_store.list_leads_by_user(111)
        self.assertEqual([l["id"] for l in leads], [old_lead["id"], new_lead["id"]])

    def test_same_updated_at_tiebreaks_on_higher_id(self):
        first = content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        second = content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        # Форсируем одинаковый updated_at у обеих — на быстром хранилище
        # (Upstash) реальное совпадение до секунды вполне возможно, это не
        # искусственный случай.
        leads = content_store._read_leads()
        same_ts = "2026-01-01T00:00:00+00:00"
        for l in leads:
            if l["id"] in (first["id"], second["id"]):
                l["updated_at"] = same_ts
        content_store._write_leads(leads)

        result = content_store.list_leads_by_user(111)
        self.assertEqual([l["id"] for l in result], [second["id"], first["id"]])

    def test_sorting_does_not_change_lead_fields(self):
        lead = content_store.add_lead({"service_name": "Лендинг", "task_description": "Тест"}, {"user_id": 111})
        content_store.update_lead_status(self.actor, lead["id"], "DONE")

        result = content_store.list_leads_by_user(111)
        self.assertEqual(result[0]["payload"]["service_name"], "Лендинг")
        self.assertEqual(result[0]["payload"]["task_description"], "Тест")
        self.assertEqual(result[0]["status"], "DONE")

    def test_admin_list_leads_unaffected_by_new_client_sort(self):
        # list_leads() (для /admin) остаётся отсортирован строго по id —
        # новая сортировка касается только list_leads_by_user().
        old_lead = content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        new_lead = content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        content_store.update_lead_status(self.actor, old_lead["id"], "IN_PROGRESS")

        admin_leads = content_store.list_leads()
        self.assertEqual([l["id"] for l in admin_leads], [new_lead["id"], old_lead["id"]])


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

        app = webserver.create_app(AsyncMock())
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

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": ""})
            self.assertEqual(resp.status, 401)

    async def test_corrupted_init_data_header_is_401_not_500(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": "garbage=%%%not-valid"})
            self.assertEqual(resp.status, 401)

    async def test_debug_headers_do_not_affect_auth_decision(self):
        """Диагностические заголовки (платформа/версия/наличие hash, наличие
        tgWebAppData в hash, ASCII-only ли initData на клиенте) — это просто
        для логов, они НЕ должны влиять на решение сервера впустить или
        отклонить запрос, даже если специально подделаны на максимально
        "убедительные" значения."""
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/my-leads",
                headers={
                    "X-Telegram-Init-Data": "",
                    "X-Debug-Platform": "ios",
                    "X-Debug-Version": "99.0",
                    "X-Debug-Has-Hash": "true",
                    "X-Debug-Hash-Has-TgWebAppData": "true",
                    "X-Debug-InitData-Ascii-Only": "true",
                },
            )
            self.assertEqual(resp.status, 401)  # диагностика не открыла доступ

    async def test_hash_has_tgwebappdata_header_does_not_bypass_invalid_signature(self):
        """Тот же принцип отдельно для случая, когда initData ПРИСУТСТВУЕТ, но
        подпись невалидна — поддельный X-Debug-Hash-Has-TgWebAppData не должен
        конвертировать провал HMAC-проверки в успех."""
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/my-leads",
                headers={
                    "X-Telegram-Init-Data": "auth_date=1786661229&user=%7B%22id%22%3A1%7D&hash=deadbeef",
                    "X-Debug-Hash-Has-TgWebAppData": "true",
                },
            )
            self.assertEqual(resp.status, 401)

    async def test_ascii_only_header_does_not_cause_false_rejection(self):
        """Симметричная проверка: даже если X-Debug-InitData-Ascii-Only
        подделан на "false" (как будто initData не-ASCII), это не должно
        ЛОМАТЬ доступ для валидной, реально ASCII initData — диагностика
        только читается для логов, никогда не участвует в решении."""
        from aiohttp.test_utils import TestClient, TestServer

        content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 42, "username": "me"})
        init_data = _sign_init_data(
            {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42, "first_name": "Я"})},
            webserver.config.BOT_TOKEN,
        )
        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/my-leads",
                headers={
                    "X-Telegram-Init-Data": init_data,
                    "X-Debug-InitData-Ascii-Only": "false",
                },
            )
            self.assertEqual(resp.status, 200)

    async def test_valid_init_data_still_returns_200_after_diagnostics_added(self):
        """diagnose_init_data() теперь вызывается на каждый отказ — этот тест
        подтверждает, что успешный (валидный) путь вообще не задет:
        правильная initData по-прежнему даёт 200 и реальные заявки, без
        каких-либо побочных эффектов от добавленной диагностики."""
        from aiohttp.test_utils import TestClient, TestServer

        content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 42, "username": "me"})
        init_data = _sign_init_data(
            {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42, "first_name": "Я"})},
            webserver.config.BOT_TOKEN,
        )
        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": init_data})
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(len(body), 1)


class CreateLeadHttpEndpointTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/leads — заменяет Telegram.WebApp.sendData() для submitBrief()
    (см. webapp/js/app.js), т.к. sendData() официально работает только для
    Mini App, запущенного через KeyboardButton.web_app, а мы ушли от этой
    кнопки ради initData. Тот же принцип identity, что и в /api/my-leads —
    user_id ТОЛЬКО из validate_init_data, никогда из body."""

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

    def _init_data(self, user_id=42, **extra_fields):
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": user_id, "first_name": "Клиент", "username": "client1"}),
            **extra_fields,
        }
        return _sign_init_data(fields, webserver.config.BOT_TOKEN)

    async def test_valid_init_data_creates_lead_and_returns_200(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"service_name": "Лендинг", "attach_tz": False, "task_description": "тест"},
            )
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertIn("lead_id", body)

        leads = content_store.list_leads_by_user(42)
        self.assertEqual(len(leads), 1)

    async def test_missing_init_data_is_401_and_creates_no_lead(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"Content-Type": "application/json"},
                json={"service_name": "Лендинг"},
            )
            self.assertEqual(resp.status, 401)
        self.assertEqual(content_store.list_leads(), [])

    async def test_invalid_init_data_is_401_and_creates_no_lead(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": "garbage=%%%not-valid", "Content-Type": "application/json"},
                json={"service_name": "Лендинг"},
            )
            self.assertEqual(resp.status, 401)
        self.assertEqual(content_store.list_leads(), [])

    async def test_user_id_spoofed_in_body_is_ignored(self):
        # Ключевое требование безопасности: body может содержать что угодно
        # (в т.ч. попытку выдать себя за другого user_id) — сервер должен
        # использовать ИСКЛЮЧИТЕЛЬНО user_id из провалидированной initData.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=42), "Content-Type": "application/json"},
                json={"service_name": "Лендинг", "user_id": 999999, "telegram": {"user_id": 999999}},
            )
            self.assertEqual(resp.status, 200)

        self.assertEqual(content_store.list_leads_by_user(999999), [])
        leads_for_real_user = content_store.list_leads_by_user(42)
        self.assertEqual(len(leads_for_real_user), 1)

    async def test_draft_id_upsert_updates_existing_lead_not_duplicate(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            first = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"service_name": "Лендинг", "draft_id": "d-upsert-1"},
            )
            first_body = await first.json()
            second = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"service_name": "Лендинг, доп. правки", "draft_id": "d-upsert-1"},
            )
            second_body = await second.json()

        self.assertEqual(first_body["lead_id"], second_body["lead_id"])  # тот же lead, не новый
        leads = content_store.list_leads_by_user(42)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["payload"]["service_name"], "Лендинг, доп. правки")

    async def test_source_case_fields_and_calc_summary_are_saved(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={
                    "service_name": "Лендинг",
                    "source": "case",
                    "source_case_id": "case_3",
                    "source_case_title": "Лендинг кофейни",
                    "calc": {"service_id": "LEND", "options": [], "urgent": False, "complex": False},
                },
            )
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertIsNotNone(body["price_range"])  # calc_summary реально посчитан

        lead = content_store.list_leads_by_user(42)[0]
        self.assertEqual(lead["payload"]["source_case_id"], "case_3")
        self.assertEqual(lead["payload"]["source_case_title"], "Лендинг кофейни")
        self.assertIsNotNone(lead["calc_summary"])

    async def test_attach_tz_true_creates_awaiting_file_state(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"service_name": "Лендинг", "attach_tz": True},
            )
            body = await resp.json()
            self.assertTrue(body["attach_tz"])

        lead = content_store.find_lead_awaiting_file(42)
        self.assertIsNotNone(lead)

    async def test_designer_is_notified_via_bot_instance(self):
        # bot передан в create_app() именно для этого — уведомление
        # DESIGNER_CHAT_ID должно идти через РЕАЛЬНЫЙ переданный Bot, а не
        # какой-то отдельный, не связанный с полученным аргументом инстанс.
        from aiohttp.test_utils import TestClient, TestServer

        fake_bot = AsyncMock()
        self._orig_designer = webserver.config.DESIGNER_CHAT_ID
        webserver.config.DESIGNER_CHAT_ID = "777"
        try:
            app = webserver.create_app(fake_bot)
            async with TestClient(TestServer(app)) as client:
                await client.post(
                    "/api/leads",
                    headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                    json={"service_name": "Лендинг"},
                )
            fake_bot.send_message.assert_awaited_once()
            self.assertEqual(fake_bot.send_message.await_args.kwargs["chat_id"], "777")
        finally:
            webserver.config.DESIGNER_CHAT_ID = self._orig_designer

    async def test_invalid_json_body_is_400_not_500(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                data="not valid json {{{",
            )
            self.assertEqual(resp.status, 400)


class SubmitIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """См. аудит: до фикса кнопка "Отправить заявку" могла из-за render(),
    пересоздающего DOM и теряющего disabled, породить несколько POST
    /api/leads на один клик — draft_id-upsert не создавал дублей заявок, но
    владелец получал уведомление на КАЖДЫЙ такой запрос. add_lead() теперь
    возвращает created: True только при реальном создании, и
    handle_create_lead шлёт send_message только когда created is True."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_token = webserver.config.BOT_TOKEN
        self._orig_designer = webserver.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        webserver.config.BOT_TOKEN = "123456:test-token-not-real"
        webserver.config.DESIGNER_CHAT_ID = "777"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        webserver.config.BOT_TOKEN = self._orig_token
        webserver.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _init_data(self, user_id=42, **extra_fields):
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": user_id, "first_name": "Клиент", "username": "client1"}),
            **extra_fields,
        }
        return _sign_init_data(fields, webserver.config.BOT_TOKEN)

    async def test_first_post_returns_created_true_and_notifies_once(self):
        from aiohttp.test_utils import TestClient, TestServer

        fake_bot = AsyncMock()
        app = webserver.create_app(fake_bot)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"service_name": "Лендинг", "draft_id": "idem-1"},
            )
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["created"])

        fake_bot.send_message.assert_awaited_once()

    async def test_repeat_post_same_draft_id_returns_created_false_and_no_second_notification(self):
        # Симулирует ровно баг из аудита: несколько POST одного и того же
        # ещё не изменившегося черновика (дублирующийся клик) до фикса
        # отправки disable.
        from aiohttp.test_utils import TestClient, TestServer

        fake_bot = AsyncMock()
        app = webserver.create_app(fake_bot)
        async with TestClient(TestServer(app)) as client:
            for _ in range(5):
                resp = await client.post(
                    "/api/leads",
                    headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                    json={"service_name": "Лендинг", "draft_id": "idem-2"},
                )
                self.assertEqual(resp.status, 200)
            last_body = await resp.json()
            self.assertFalse(last_body["created"])

        # Один lead (upsert), но ОДНО уведомление — не пять.
        self.assertEqual(len(content_store.list_leads_by_user(42)), 1)
        fake_bot.send_message.assert_awaited_once()


class LeadSupplementTests(unittest.IsolatedAsyncioTestCase):
    """mode="supplement" — дополнение к уже существующей заявке, адресуется
    по lead_id (см. аудит: НЕ draft_id — тот принадлежит другому этапу
    жизни заявки и не подходит для этой цели)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_token = webserver.config.BOT_TOKEN
        self._orig_designer = webserver.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        webserver.config.BOT_TOKEN = "123456:test-token-not-real"
        webserver.config.DESIGNER_CHAT_ID = "777"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        webserver.config.BOT_TOKEN = self._orig_token
        webserver.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _init_data(self, user_id=42, **extra_fields):
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": user_id, "first_name": "Клиент", "username": "client1"}),
            **extra_fields,
        }
        return _sign_init_data(fields, webserver.config.BOT_TOKEN)

    async def _create_lead(self, client, user_id=42, **payload_extra):
        resp = await client.post(
            "/api/leads",
            headers={"X-Telegram-Init-Data": self._init_data(user_id=user_id), "Content-Type": "application/json"},
            json={"service_name": "Лендинг", "task_description": "исходное описание", **payload_extra},
        )
        body = await resp.json()
        return body["lead_id"]

    async def test_valid_supplement_to_own_lead_returns_200(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "добавьте, пожалуйста, синий фон"}},
            )
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(body["lead_id"], lead_id)
            self.assertEqual(body["supplement_id"], 1)

    async def test_supplement_to_nonexistent_lead_is_404(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": 999999, "fields": {"comment": "..."}},
            )
            self.assertEqual(resp.status, 404)

    async def test_supplement_to_someone_elses_lead_is_403(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client, user_id=42)
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=999), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "чужое дополнение"}},
            )
            self.assertEqual(resp.status, 403)

        lead = content_store.get_lead(lead_id)
        self.assertEqual(lead.get("supplements", []), [])  # чужая попытка ничего не добавила

    async def test_supplement_does_not_change_original_payload(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "дополнительная деталь"}},
            )

        lead = content_store.get_lead(lead_id)
        self.assertEqual(lead["payload"]["task_description"], "исходное описание")  # не перезаписан

    async def test_two_supplements_are_both_kept_append_only(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "первое дополнение"}},
            )
            await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "второе дополнение"}},
            )

        lead = content_store.get_lead(lead_id)
        self.assertEqual(len(lead["supplements"]), 2)
        self.assertEqual(lead["supplements"][0]["fields"]["comment"], "первое дополнение")
        self.assertEqual(lead["supplements"][1]["fields"]["comment"], "второе дополнение")
        self.assertEqual(lead["supplements"][0]["id"], 1)
        self.assertEqual(lead["supplements"][1]["id"], 2)

    async def test_supplement_notification_is_not_new_lead_format(self):
        from aiohttp.test_utils import TestClient, TestServer

        fake_bot = AsyncMock()
        app = webserver.create_app(fake_bot)
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            fake_bot.send_message.reset_mock()  # сбрасываем уведомление о создании заявки
            await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "важная деталь"}},
            )

        fake_bot.send_message.assert_awaited_once()
        text = fake_bot.send_message.await_args.kwargs["text"]
        self.assertIn(f"Дополнение к заявке #{lead_id}", text)
        self.assertNotIn("Новая заявка", text)
        self.assertIn("важная деталь", text)

    async def test_supplement_user_id_from_body_is_ignored(self):
        # Тот же принцип, что и для mode="new" — lead_id принадлежности
        # проверяется по validated initData, не по тому, что прислано в body.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client, user_id=42)
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=42), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "user_id": 999999, "fields": {"comment": "..."}},
            )
            self.assertEqual(resp.status, 200)  # user_id в body просто игнорируется, не читается вообще


class LeadMaterialTests(unittest.IsolatedAsyncioTestCase):
    """Файл ТЗ теперь не просто пересылается — file_id/file_unique_id
    сохраняются на самой заявке (см. аудит: раньше связь "файл ↔ заявка"
    существовала только в момент исполнения handle_tz_file и терялась)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_material_attaches_to_correct_lead_and_saves_file_id(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m1"})
        lead_id = content_store.find_lead_awaiting_file(message.from_user.id)["id"]

        message.document = make_fake_document(file_id="doc-1", file_unique_id="uniq-1")
        await webapp.handle_tz_file(message)

        lead = content_store.get_lead(lead_id)
        self.assertEqual(len(lead["materials"]), 1)
        self.assertEqual(lead["materials"][0]["file_id"], "doc-1")
        self.assertEqual(lead["materials"][0]["file_unique_id"], "uniq-1")
        self.assertEqual(lead["materials"][0]["kind"], "document")
        self.assertEqual(lead["materials"][0]["source"], "new")

    async def test_awaiting_state_cleared_after_material_received(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m2"})
        self.assertIsNotNone(content_store.find_lead_awaiting_file(message.from_user.id))

        message.document = make_fake_document()
        await webapp.handle_tz_file(message)
        self.assertIsNone(content_store.find_lead_awaiting_file(message.from_user.id))

    async def test_multiple_leads_same_user_do_not_cause_misattribution(self):
        # Два лида одного клиента, оба помечены attach_tz=True — второй
        # (более новый) должен снять ожидание с первого (см.
        # content_store._clear_other_awaiting/аудит), иначе find_lead_awaiting_file
        # была бы неоднозначной и файл мог уйти не в ту заявку.
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m3-a"})
        first_lead_id = content_store.find_lead_awaiting_file(message.from_user.id)["id"]

        await webapp._handle_brief_submission(message, {"service_name": "Логотип", "attach_tz": True, "draft_id": "m3-b"})
        second_lead_id = content_store.find_lead_awaiting_file(message.from_user.id)["id"]

        self.assertNotEqual(first_lead_id, second_lead_id)
        first_lead = content_store.get_lead(first_lead_id)
        self.assertFalse(first_lead["awaiting_tz_file"])  # снято вторым запросом

        message.document = make_fake_document()
        await webapp.handle_tz_file(message)

        # Файл должен уйти именно во вторую (единственно ожидающую) заявку.
        self.assertEqual(len(content_store.get_lead(second_lead_id)["materials"]), 1)
        self.assertEqual(content_store.get_lead(first_lead_id).get("materials", []), [])

    async def test_supplement_wants_file_ties_awaiting_state_to_that_lead(self):
        from aiohttp.test_utils import TestClient, TestServer

        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_token = webserver.config.BOT_TOKEN
        webserver.config.BOT_TOKEN = "123456:test-token-not-real"

        def _init_data(user_id=555):
            fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "К"})}
            return _sign_init_data(fields, webserver.config.BOT_TOKEN)

        try:
            app = webserver.create_app(AsyncMock())
            async with TestClient(TestServer(app)) as client:
                created = await client.post(
                    "/api/leads",
                    headers={"X-Telegram-Init-Data": _init_data(), "Content-Type": "application/json"},
                    json={"service_name": "Лендинг"},
                )
                lead_id = (await created.json())["lead_id"]
                await client.post(
                    "/api/leads",
                    headers={"X-Telegram-Init-Data": _init_data(), "Content-Type": "application/json"},
                    json={"mode": "supplement", "lead_id": lead_id, "fields": {}, "wants_file": True},
                )
        finally:
            webserver.config.BOT_TOKEN = self._orig_token

        lead = content_store.find_lead_awaiting_file(555)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["id"], lead_id)
        self.assertEqual(lead["awaiting_tz_file_source"], "supplement")


def make_reply_message(chat_id: int, text: str, send_message: AsyncMock) -> SimpleNamespace:
    """Достаточно для lead_reply_send: message.text, message.chat.id (actor
    для _require_designer), message.bot.send_message (контролируемый —
    успех/исключение задаёт сам тест), message.answer."""
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=chat_id),
        bot=SimpleNamespace(send_message=send_message),
        answer=AsyncMock(),
    )


class OwnerMessageTests(unittest.IsolatedAsyncioTestCase):
    """owner_messages[] — append-only ответы дизайнера клиенту, тот же
    паттерн, что и supplements[]/materials[] (см. аудит): отдельный поток,
    не трогает payload, не теряется при неудачной Telegram-доставке."""

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
            {"service_name": "Лендинг", "task_description": "Исходная задача"},
            {"user_id": 55555, "username": "client", "first_name": "Клиент"},
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- content_store.add_owner_message ----

    def test_new_owner_message_is_saved(self):
        lead = content_store.add_owner_message(self.actor, self.lead["id"], "Первый ответ", "sent")
        self.assertEqual(len(lead["owner_messages"]), 1)
        self.assertEqual(lead["owner_messages"][0]["text"], "Первый ответ")
        self.assertEqual(lead["owner_messages"][0]["delivery_status"], "sent")
        self.assertEqual(lead["owner_messages"][0]["id"], 1)

    def test_two_messages_are_both_kept_append_only(self):
        content_store.add_owner_message(self.actor, self.lead["id"], "Первый", "sent")
        lead = content_store.add_owner_message(self.actor, self.lead["id"], "Второй", "sent")
        self.assertEqual(len(lead["owner_messages"]), 2)
        self.assertEqual(lead["owner_messages"][0]["text"], "Первый")
        self.assertEqual(lead["owner_messages"][1]["text"], "Второй")
        self.assertEqual(lead["owner_messages"][0]["id"], 1)
        self.assertEqual(lead["owner_messages"][1]["id"], 2)

    def test_owner_message_does_not_change_payload(self):
        lead = content_store.add_owner_message(self.actor, self.lead["id"], "Ответ", "sent")
        self.assertEqual(lead["payload"]["task_description"], "Исходная задача")

    def test_owner_message_updates_updated_at(self):
        self.assertIsNone(self.lead["updated_at"])
        lead = content_store.add_owner_message(self.actor, self.lead["id"], "Ответ", "sent")
        self.assertIsNotNone(lead["updated_at"])

    def test_owner_message_requires_designer(self):
        with self.assertRaises(content_store.NotDesignerError):
            content_store.add_owner_message("not-the-designer", self.lead["id"], "Ответ", "sent")

    def test_owner_message_for_unknown_lead_returns_none(self):
        self.assertIsNone(content_store.add_owner_message(self.actor, 999999, "Ответ", "sent"))

    # ---- bot/handlers/admin.py::lead_reply_send (реальный хендлер) ----

    async def test_lead_reply_send_success_sets_delivery_status_sent(self):
        state = make_state(self.actor)
        await state.update_data(lead_id=self.lead["id"])
        await state.set_state(AdminStates.lead_reply_text)

        message = make_reply_message(self.actor, "Всё уточнили, приступаем", AsyncMock())
        await admin.lead_reply_send(message, state)

        lead = content_store.get_lead(self.lead["id"])
        self.assertEqual(len(lead["owner_messages"]), 1)
        self.assertEqual(lead["owner_messages"][0]["delivery_status"], "sent")
        self.assertEqual(lead["owner_messages"][0]["text"], "Всё уточнили, приступаем")
        # Контекст "Ответ по заявке #N" — только в исходящем сообщении клиенту.
        sent_text = message.bot.send_message.await_args.kwargs["text"]
        self.assertIn(f"#{self.lead['id']}", sent_text)
        self.assertIn("Всё уточнили, приступаем", sent_text)

    async def test_lead_reply_send_failure_sets_delivery_status_failed_and_keeps_message(self):
        state = make_state(self.actor)
        await state.update_data(lead_id=self.lead["id"])
        await state.set_state(AdminStates.lead_reply_text)

        failing_send = AsyncMock(side_effect=TelegramAPIError(method=None, message="bot was blocked"))
        message = make_reply_message(self.actor, "Ответ, который не дойдёт", failing_send)
        await admin.lead_reply_send(message, state)

        lead = content_store.get_lead(self.lead["id"])
        self.assertEqual(len(lead["owner_messages"]), 1)  # не потерялось
        self.assertEqual(lead["owner_messages"][0]["delivery_status"], "failed")
        self.assertEqual(lead["owner_messages"][0]["text"], "Ответ, который не дойдёт")
        # Владельцу — понятная ошибка, не тихий сбой.
        admin_text = message.answer.await_args.args[0]
        self.assertIn("Не получилось отправить", admin_text)

    async def test_admin_detail_shows_owner_messages(self):
        content_store.add_owner_message(self.actor, self.lead["id"], "Первый ответ", "sent")
        content_store.add_owner_message(self.actor, self.lead["id"], "Второй, не дошёл", "failed")
        lead = content_store.get_lead(self.lead["id"])

        text = lead_format.format_lead_admin_detail(lead)
        self.assertIn("Ответы дизайнера", text)
        self.assertIn("Первый ответ", text)
        self.assertIn("Второй, не дошёл", text)
        self.assertIn("не доставлено", text)

    # ---- /api/my-leads (HTTP) ----

    async def test_my_leads_returns_owner_messages(self):
        from aiohttp.test_utils import TestClient, TestServer

        content_store.add_owner_message(self.actor, self.lead["id"], "Виден клиенту", "sent")
        self._orig_token = webserver.config.BOT_TOKEN
        webserver.config.BOT_TOKEN = "123456:test-token-not-real"
        try:
            fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 55555, "first_name": "Клиент"})}
            init_data = _sign_init_data(fields, webserver.config.BOT_TOKEN)
            app = webserver.create_app(AsyncMock())
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/my-leads", headers={"X-Telegram-Init-Data": init_data})
                body = await resp.json()
        finally:
            webserver.config.BOT_TOKEN = self._orig_token

        self.assertEqual(resp.status, 200)
        lead = next(l for l in body if l["id"] == self.lead["id"])
        self.assertEqual(len(lead["owner_messages"]), 1)
        self.assertEqual(lead["owner_messages"][0]["text"], "Виден клиенту")

    def test_lead_without_owner_messages_has_empty_list_not_missing_key(self):
        # Существующие лиды (созданные до этой фичи) — отсутствие поля
        # эквивалентно [] на уровне API/frontend; на уровне content_store
        # свежесозданный lead уже содержит owner_messages: [] явно.
        self.assertEqual(self.lead.get("owner_messages", []), [])


class StatusNotificationTests(unittest.IsolatedAsyncioTestCase):
    """Уведомление клиенту при смене статуса (lead_change_status) — см.
    аудит: должно уходить ровно один раз при реальном изменении статуса,
    не должно уходить при повторной установке того же значения, не должно
    ронять сохранение статуса при сбое Telegram, и не должно затрагивать
    owner_messages[] (отдельный, независимый поток)."""

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

    def _make_lead(self, user_id=55555):
        return content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Тест"},
            {"user_id": user_id, "username": "client", "first_name": "Клиент"},
        )

    async def test_status_change_sends_one_notification(self):
        lead = self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        callback.bot.send_message.assert_awaited_once()
        self.assertEqual(callback.bot.send_message.await_args.kwargs["chat_id"], 55555)
        self.assertEqual(content_store.get_lead(lead["id"])["status"], "IN_PROGRESS")

    async def test_status_change_notification_has_service_name_and_label(self):
        # lead.id — глобальный сквозной счётчик по ВСЕМ заявкам от ВСЕХ
        # клиентов (см. content_store.add_lead), клиенту его не показываем
        # (см. UX-аудит) — вместо номера используется service_name.
        lead = self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:WAITING_CLIENT", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Ваша заявка обновлена", text)
        self.assertIn("Лендинг", text)  # service_name из _make_lead()
        self.assertIn("Нужно ваше действие", text)

    async def test_status_change_notification_does_not_contain_lead_id(self):
        lead = self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:DONE", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertNotIn(f"#{lead['id']}", text)
        self.assertNotIn(str(lead["id"]), text)

    async def test_status_change_notification_falls_back_when_service_name_missing(self):
        lead = content_store.add_lead(
            {"task_description": "Без указания услуги"},  # service_name отсутствует в payload
            {"user_id": 55555, "username": "client", "first_name": "Клиент"},
        )
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        # notification всё равно отправлена, с безопасным fallback
        callback.bot.send_message.assert_awaited_once()
        text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Ваша заявка обновлена", text)
        self.assertIn("Ваша заявка", text)  # fallback вместо пустого service_name
        self.assertIn("В работе", text)

    async def test_repeat_same_status_sends_zero_notifications(self):
        lead = self._make_lead()  # уже "NEW" по умолчанию
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:NEW", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        callback.bot.send_message.assert_not_awaited()
        self.assertEqual(content_store.get_lead(lead["id"])["status"], "NEW")

    async def test_missing_user_id_changes_status_but_sends_zero_notifications(self):
        lead = content_store.add_lead(
            {"service_name": "Лендинг"}, {"user_id": None, "username": None, "first_name": None},
        )
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        callback.bot.send_message.assert_not_awaited()
        self.assertEqual(content_store.get_lead(lead["id"])["status"], "IN_PROGRESS")

    async def test_send_message_exception_leaves_status_changed(self):
        lead = self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        failing_bot = AsyncMock()
        failing_bot.send_message.side_effect = TelegramAPIError(method=None, message="bot was blocked")
        callback = make_callback("adminleadstatus:DONE", chat_id=self.actor, bot=failing_bot)

        await admin.lead_change_status(callback, state)  # не должно бросить исключение наружу

        self.assertEqual(content_store.get_lead(lead["id"])["status"], "DONE")

    async def test_status_change_does_not_touch_owner_messages(self):
        lead = self._make_lead()
        content_store.add_owner_message(self.actor, lead["id"], "Ранее написанный ответ", "sent")
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        lead_after = content_store.get_lead(lead["id"])
        self.assertEqual(len(lead_after["owner_messages"]), 1)
        self.assertEqual(lead_after["owner_messages"][0]["text"], "Ранее написанный ответ")


class FormatStatusNotificationTests(unittest.TestCase):
    """Прямые тесты bot/lead.py::format_status_notification — отдельно от
    handler-уровня (StatusNotificationTests выше), для точности fallback-логики."""

    def test_uses_service_name_when_present(self):
        text = lead_format.format_status_notification("Лендинг", "VIEWED")
        self.assertEqual(text, "Ваша заявка обновлена\nЛендинг\nСтатус: 👀 На рассмотрении")

    def test_no_lead_id_in_output_by_construction(self):
        # format_status_notification больше не принимает lead_id вообще —
        # это гарантия на уровне сигнатуры, не только по содержимому текста.
        text = lead_format.format_status_notification("Сайт", "DONE")
        self.assertNotIn("#", text)

    def test_fallback_on_empty_string(self):
        text = lead_format.format_status_notification("", "NEW")
        self.assertEqual(text, "Ваша заявка обновлена\nВаша заявка\nСтатус: 🆕 Заявка получена")

    def test_fallback_on_none(self):
        text = lead_format.format_status_notification(None, "CANCELLED")
        self.assertIn("Ваша заявка обновлена\nВаша заявка\nСтатус:", text)

    def test_fallback_on_whitespace_only(self):
        text = lead_format.format_status_notification("   ", "IN_PROGRESS")
        self.assertIn("Ваша заявка обновлена\nВаша заявка\nСтатус:", text)


if __name__ == "__main__":
    unittest.main()
