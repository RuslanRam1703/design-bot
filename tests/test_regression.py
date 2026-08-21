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

import asyncio
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
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
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
        self.assertIsNotNone(await content_store.find_lead_awaiting_file(message.from_user.id))

        # Тот же draft_id (клиент передумал через "Дополнить информацию" и
        # убрал "пришлю файл") -> upsert той же заявки, флаг должен сняться,
        # а не остаться висеть от предыдущей отправки.
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": False, "draft_id": "d1"})
        self.assertIsNone(await content_store.find_lead_awaiting_file(message.from_user.id))

    async def test_repeat_submission_with_tz_again_still_ends_clean_without_file(self):
        message = make_message()

        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d2"})
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d2"})
        self.assertIsNotNone(await content_store.find_lead_awaiting_file(message.from_user.id))

        # Присланный файл закрывает ожидание (тот же путь, что и в проде).
        message.document = make_fake_document()
        await webapp.handle_tz_file(message)
        self.assertIsNone(await content_store.find_lead_awaiting_file(message.from_user.id))

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

        self.assertIsNotNone(await content_store.find_lead_awaiting_file(1))  # заявка владельца всё ещё ждёт файл
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
        lead = await content_store.find_lead_awaiting_file(message.from_user.id)
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
        self.assertIsNone(await content_store.find_lead_awaiting_file(message.from_user.id))  # attach_tz=False -> ничего не ждём

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
        self.assertIsNotNone(await content_store.find_lead_awaiting_file(message.from_user.id))  # attach_tz=True


class AdminCancelContextTests(unittest.IsolatedAsyncioTestCase):
    """_resolve_cancel — сердце Механизма 2: "Отмена" должна возвращать к
    разделу, с которого начался мастер, а не всегда в корень."""

    async def test_options_target_preserves_service_id_and_returns_to_options_menu(self):
        text, markup, next_state, next_data = await admin._resolve_cancel(
            {"cancel_to": "options", "service_id": "LEND", "opt_name": "черновик, должен быть отброшен"}
        )
        self.assertEqual(next_state, AdminStates.edit_service_field_pick)
        self.assertEqual(next_data, {"service_id": "LEND"})
        self.assertIn("Опции", text)

    async def test_options_target_without_service_id_falls_back_to_root(self):
        # Защита от испорченных данных состояния — cancel_to="options" без
        # service_id не должен приводить к сломанному экрану.
        text, markup, next_state, next_data = await admin._resolve_cancel({"cancel_to": "options"})
        self.assertIsNone(next_state)
        self.assertIn("Админ-меню", text)

    async def test_section_targets_clear_state_and_data(self):
        for target in ("cases", "faq", "pricing", "categories"):
            with self.subTest(target=target):
                text, markup, next_state, next_data = await admin._resolve_cancel({"cancel_to": target, "junk": 1})
                self.assertIsNone(next_state)
                self.assertEqual(next_data, {})

    async def test_unknown_or_missing_target_defaults_to_root(self):
        default_text, *_ = await admin._resolve_cancel({})
        unknown_text, *_ = await admin._resolve_cancel({"cancel_to": "does-not-exist"})
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


def make_callback(data: str, chat_id: int = 777, bot: AsyncMock | None = None, message_id: int = 555) -> SimpleNamespace:
    """Достаточно для admin.py callback-хендлеров: callback.data,
    callback.message.chat.id, callback.message.edit_text (async),
    callback.answer (async), callback.bot (async send_message — нужен
    lead_change_status для уведомления клиента о смене статуса).

    message_id (P1-3, Batch 1) — нужен flow.step_from_callback
    (callback.message.message_id, теперь вызывается из menu_* section
    navigation); дефолт 555 сохраняет прежнее поведение для тестов,
    которым конкретное значение не важно — lifecycle-тесты передают
    уникальный message_id явно, чтобы отличить edit от send нового."""
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id, edit_text=AsyncMock()),
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


def make_flow_message_factory(chat_id: int = 888, start_id: int = 1000):
    """P1-3 аудит, Batch 0: make_flow_message() всегда возвращает
    message_id=555 из .answer() — тест не может отличить "отредактировал
    то же сообщение" от "отправил N новых сообщений", потому что все они
    выглядят одинаково (один и тот же фейковый id). Эта фабрика — для
    сценариев, где важно доказать обратное: каждый вызов .answer() у ЛЮБОГО
    сообщения, порождённого этой фабрикой, увеличивает общий counter и
    возвращает УНИКАЛЬНЫЙ message_id — orphan-message регрессия (новое
    сообщение вместо edit) становится видна в тесте как рост числа
    различных id, а не остаётся замаскированной.

    Возвращает callable make(text=..., delete_raises=...) — каждый вызов
    make() создаёт НОВОЕ "входящее" сообщение (например, следующий шаг
    того же сценария), но все они делят один и тот же counter."""
    counter = {"n": start_id}

    def make(text: str | None = "/start", delete_raises: bool = False) -> SimpleNamespace:
        async def _delete():
            if delete_raises:
                raise TelegramAPIError(method=None, message="can't delete")

        def _next_sent(*args, **kwargs):
            counter["n"] += 1
            return SimpleNamespace(message_id=counter["n"], chat=SimpleNamespace(id=chat_id))

        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            from_user=SimpleNamespace(id=chat_id, username=None, first_name="Тест", last_name=None),
            text=text,
            delete=_delete,
            answer=AsyncMock(side_effect=_next_sent),
            bot=SimpleNamespace(delete_message=AsyncMock(), edit_message_text=AsyncMock()),
        )

    return make


class FlowUtilTests(unittest.IsolatedAsyncioTestCase):
    """bot/flow.py — принцип перенесён из Personal Assistant
    (src/bot/utils/flow.py), не придуман заново. Все три правила: триггер
    удаляется, старый корневой экран удаляется при открытии нового, шаги
    внутри сценария редактируют одно сообщение. Удаления — best-effort."""

    async def test_open_flow_deletes_previous_anchor_and_trigger_then_tracks_new_message(self):
        # Свежий state (нет NAV anchor'а) -> ensure_nav_anchor сам создаёт
        # его первым answer()-вызовом (см. bot/flow.py) — это ЧИСТАЯ
        # отправка, без единого delete_message. Единственный delete_message
        # здесь — RULE 2 (удаление предыдущего TRANSIENT anchor, заранее
        # положенного в state.data).
        state = make_state()
        await state.update_data(_flow_msg_id=111, _flow_chat_id=888)
        msg = make_flow_message()

        await flow.open_flow(msg, state, "Новый экран")

        msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=111)  # RULE 2 — старый TRANSIENT anchor
        self.assertEqual(msg.answer.await_count, 2)  # NAV anchor (создан) + новый TRANSIENT экран
        last_call = msg.answer.await_args_list[-1]
        self.assertEqual(last_call.args[0] if last_call.args else last_call.kwargs.get("text"), "Новый экран")
        data = await state.get_data()
        self.assertEqual(data["_flow_msg_id"], 555)  # новое TRANSIENT-сообщение стало текущим экраном

    async def test_open_flow_survives_delete_failures_best_effort(self):
        state = make_state()
        await state.update_data(_flow_msg_id=111, _flow_chat_id=888)
        msg = make_flow_message(delete_raises=True)
        msg.bot.delete_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="too old"))

        await flow.open_flow(msg, state, "Новый экран")  # не должно упасть

        self.assertEqual(msg.answer.await_count, 2)  # NAV anchor + новый TRANSIENT экран, оба отправлены

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


class NavAnchorTests(unittest.IsolatedAsyncioTestCase):
    """NAV anchor (persistent reply-клавиатура) — независим от TRANSIENT
    anchor'а, которым управляют RULE 1-3 (см. UX-аудит про исчезающую
    клавиатуру после FAQ/admin: zero-width send-then-delete carrier
    оказался ненадёжен в реальном Telegram Desktop даже с is_persistent=
    True — удаление carrier-сообщения сбрасывало клавиатуру). NAV anchor
    никогда не удаляется — только создаётся один раз лениво и, при
    необходимости, редактируется на месте."""

    async def test_ensure_nav_anchor_creates_when_missing(self):
        from aiogram.types import ReplyKeyboardMarkup

        state = make_state()
        msg = make_flow_message()

        created = await flow.ensure_nav_anchor(msg, state)

        self.assertTrue(created)
        msg.answer.assert_awaited_once()
        call = msg.answer.await_args
        sent_text = call.args[0] if call.args else call.kwargs.get("text")
        self.assertEqual(sent_text, texts.WELCOME)
        sent_markup = call.kwargs.get("reply_markup") or call.args[1]
        self.assertIsInstance(sent_markup, ReplyKeyboardMarkup)
        data = await state.get_data()
        self.assertEqual(data[flow._NAV_ANCHOR_MSG_KEY], 555)
        self.assertEqual(data[flow._NAV_ANCHOR_CHAT_KEY], 888)

    async def test_ensure_nav_anchor_noop_when_already_exists(self):
        state = make_state()
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888})
        msg = make_flow_message()

        created = await flow.ensure_nav_anchor(msg, state)

        self.assertFalse(created)
        msg.answer.assert_not_awaited()
        msg.bot.delete_message.assert_not_awaited()

    async def test_reset_nav_screen_edits_existing_anchor_instead_of_new_message(self):
        state = make_state()
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888})
        msg = make_flow_message()

        await flow.reset_nav_screen(msg, state)

        msg.answer.assert_not_awaited()  # ни одного нового сообщения — только edit
        msg.bot.edit_message_text.assert_awaited_once_with(texts.WELCOME, chat_id=888, message_id=111)

    async def test_reset_nav_screen_deletes_transient_and_keeps_nav_anchor(self):
        state = make_state()
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888,
            flow._ANCHOR_MSG_KEY: 222, flow._ANCHOR_CHAT_KEY: 888,
        })
        msg = make_flow_message()

        await flow.reset_nav_screen(msg, state)

        msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=222)  # TRANSIENT удалён
        data = await state.get_data()
        self.assertIsNone(data.get(flow._ANCHOR_MSG_KEY))
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # NAV anchor не тронут

    async def test_reset_nav_screen_ignores_message_not_modified(self):
        # Двойное нажатие "Главное меню" подряд — WELCOME уже показан,
        # edit_message_text бросает "message is not modified": штатный
        # случай, НЕ повод пересоздавать anchor.
        state = make_state()
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888})
        msg = make_flow_message()
        msg.bot.edit_message_text = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="Bad Request: message is not modified")
        )

        await flow.reset_nav_screen(msg, state)  # не должно упасть

        msg.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # anchor не пересоздан

    async def test_reset_nav_screen_recreates_anchor_when_manually_deleted(self):
        # Пользователь мог вручную удалить NAV anchor (Telegram это
        # разрешает в приватных чатах) — edit_message_text падает с ошибкой
        # из _NAV_ANCHOR_GONE_MARKERS ("message to edit not found" —
        # однозначный признак, что сообщения больше нет) -> best-effort
        # пересоздаём, не роняя handler.
        state = make_state()
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888})
        msg = make_flow_message()
        msg.bot.edit_message_text = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="Bad Request: message to edit not found")
        )

        await flow.reset_nav_screen(msg, state)  # не должно упасть

        msg.answer.assert_awaited_once()  # новый NAV anchor создан
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 555)  # обновлён на новый message_id

    async def test_reset_nav_screen_recreates_anchor_when_message_cant_be_edited(self):
        # Второй маркер из _NAV_ANCHOR_GONE_MARKERS — "message can't be
        # edited" (например, сообщение слишком старое для редактирования).
        state = make_state()
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888})
        msg = make_flow_message()
        msg.bot.edit_message_text = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="Bad Request: message can't be edited")
        )

        await flow.reset_nav_screen(msg, state)  # не должно упасть

        msg.answer.assert_awaited_once()  # новый NAV anchor создан
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 555)

    async def test_reset_nav_screen_ignores_unrelated_transient_error_without_recreating(self):
        # Production-аудит: ЛЮБАЯ другая ошибка (не "not modified", не из
        # _NAV_ANCHOR_GONE_MARKERS) — например, транзитный flood-control —
        # НЕ должна приводить к пересозданию anchor'а. Раньше (до этого
        # фикса) любая такая ошибка трактовалась как "anchor удалён" и
        # вызывала каскадное дублирование NAV anchor в production.
        state = make_state()
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888})
        msg = make_flow_message()
        msg.bot.edit_message_text = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="Too Many Requests: retry later")
        )

        await flow.reset_nav_screen(msg, state)  # не должно упасть

        msg.answer.assert_not_awaited()  # НЕ создан новый anchor
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # anchor не изменился

    def test_refresh_reply_keyboard_removed(self):
        self.assertFalse(hasattr(flow, "refresh_reply_keyboard"))

    def test_zero_width_sentinel_not_used_anywhere_in_bot_package(self):
        zero_width_space = "​"
        bot_dir = Path(__file__).resolve().parent.parent / "bot"
        offenders = [
            str(path) for path in bot_dir.rglob("*.py")
            if zero_width_space in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


class NavAnchorRaceConditionTests(unittest.IsolatedAsyncioTestCase):
    """Race condition при конкурентной обработке апдейтов ОДНОГО чата (см.
    root-cause аудит): aiogram Dispatcher._polling(handle_as_tasks=True) —
    default — создаёт независимый asyncio.Task на каждый update; без
    events_isolation (DisabledEventIsolation, no-op lock, тоже default) они
    НЕ сериализуются per StorageKey. ensure_nav_anchor делает check-then-act
    (get_data() -> conditional answer()+update_data(), где answer() —
    реальный сетевой round-trip) без защиты — два "нажатия", выполняющиеся
    конкурентно, могли оба пройти check ДО того, как первая запись попадала
    в state.data, создавая дублирующийся NAV anchor (воспроизведено в
    production: несколько подряд "Главное меню" -> несколько новых WELCOME).
    SimpleEventIsolation — штатный aiogram-механизм (per-StorageKey
    asyncio.Lock из aiogram.fsm.storage.memory), не самодельный."""

    def _make_delayed_msg(self, chat_id):
        """Как make_flow_message, но answer() с искусственной asyncio.sleep
        — имитирует реальный сетевой round-trip к Telegram Bot API,
        открывая то же окно гонки, что и в проде (запись в state.data
        происходит только ПОСЛЕ await message.answer(...) в _create_nav_anchor)."""
        msg = make_flow_message(chat_id=chat_id)
        call_counter = {"n": 0}

        async def _slow_answer(text, reply_markup=None):
            await asyncio.sleep(0.02)
            call_counter["n"] += 1
            return SimpleNamespace(message_id=9000 + call_counter["n"], chat=SimpleNamespace(id=chat_id))

        msg.answer = AsyncMock(side_effect=_slow_answer)
        return msg

    async def test_concurrent_ensure_nav_anchor_without_isolation_can_duplicate(self):
        # Демонстрация самой гонки (БЕЗ защиты) — подтверждает, что race
        # реально воспроизводим на этом коде без events_isolation, а не
        # гипотетичен. Это не regression исправления ниже, а доказательство
        # того, что проблема, которую чинит SimpleEventIsolation, настоящая.
        state = make_state(777)
        msg1 = self._make_delayed_msg(777)
        msg2 = self._make_delayed_msg(777)

        await asyncio.gather(
            flow.ensure_nav_anchor(msg1, state),
            flow.ensure_nav_anchor(msg2, state),
        )

        # Без сериализации обе "конкурентные" таски успели пройти check ДО
        # того, как первая записала anchor -> answer() вызван дважды.
        self.assertEqual(msg1.answer.await_count + msg2.answer.await_count, 2)

    async def test_simple_event_isolation_serializes_concurrent_ensure_nav_anchor(self):
        # Тот же race setup, обёрнутый в SimpleEventIsolation.lock(key) —
        # ровно так, как это делает сам aiogram Dispatcher для апдейтов
        # одного чата при events_isolation=SimpleEventIsolation() (см.
        # bot/main.py). Два конкурентных вызова на cold state должны
        # создать РОВНО один NAV anchor.
        chat_id = 778
        state = make_state(chat_id)
        key = StorageKey(bot_id=0, chat_id=chat_id, user_id=chat_id)
        isolation = SimpleEventIsolation()

        msg1 = self._make_delayed_msg(chat_id)
        msg2 = self._make_delayed_msg(chat_id)

        async def _isolated_call(msg):
            async with isolation.lock(key):
                await flow.ensure_nav_anchor(msg, state)

        await asyncio.gather(_isolated_call(msg1), _isolated_call(msg2))

        # Сериализовано -> ровно ОДИН answer() среди обоих вызовов, второй
        # увидел уже существующий anchor и не создавал новый.
        total_answer_calls = msg1.answer.await_count + msg2.answer.await_count
        self.assertEqual(total_answer_calls, 1)
        data = await state.get_data()
        self.assertIsNotNone(data.get(flow._NAV_ANCHOR_MSG_KEY))
        self.assertIsNotNone(data.get(flow._NAV_ANCHOR_CHAT_KEY))

    async def test_repeated_calls_after_creation_reuse_same_message_id(self):
        # Требование: повторные (не только конкурентные) вызовы ПОСЛЕ
        # создания должны переиспользовать ОДИН message_id, а не плодить
        # новые при каждом вызове.
        state = make_state(779)
        msg1 = make_flow_message(chat_id=779)
        await flow.ensure_nav_anchor(msg1, state)
        data = await state.get_data()
        first_nav_id = data.get(flow._NAV_ANCHOR_MSG_KEY)
        self.assertIsNotNone(first_nav_id)

        for _ in range(3):
            msg = make_flow_message(chat_id=779)
            created = await flow.ensure_nav_anchor(msg, state)
            self.assertFalse(created)
            msg.answer.assert_not_awaited()

        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), first_nav_id)

    async def test_main_menu_cleanup_never_touches_nav_anchor(self):
        # "⌂ Главное меню" (flow.main_menu_cleanup) -- НЕ /start: NAV anchor
        # не создаётся, не редактируется, не пересоздаётся вообще. Только
        # сброс сценария + удаление TRANSIENT-экрана + удаление trigger.
        state = make_state(781)
        await state.set_state(AdminStates.add_faq_answer)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 781,
            flow._ANCHOR_MSG_KEY: 222, flow._ANCHOR_CHAT_KEY: 781,
        })
        msg = make_flow_message(chat_id=781)
        deleted = {"trigger": False}

        async def _tracked_delete():
            deleted["trigger"] = True

        msg.delete = _tracked_delete

        await flow.main_menu_cleanup(msg, state)

        msg.answer.assert_not_awaited()                       # WELCOME не создан
        msg.bot.edit_message_text.assert_not_awaited()         # NAV anchor не отредактирован
        msg.bot.delete_message.assert_awaited_once_with(chat_id=781, message_id=222)  # TRANSIENT удалён
        self.assertTrue(deleted["trigger"])                    # trigger удалён
        self.assertIsNone(await state.get_state())             # FSM сброшен
        data = await state.get_data()
        self.assertIsNone(data.get(flow._ANCHOR_MSG_KEY))
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # NAV anchor не изменился

    async def test_main_menu_cleanup_safe_without_nav_anchor(self):
        # F. Cold-state edge case: main_menu_cleanup вызван, когда NAV
        # anchor вообще не отслеживается -- ничего не создаёт, не падает.
        state = make_state(782)
        msg = make_flow_message(chat_id=782)

        await flow.main_menu_cleanup(msg, state)  # не должно упасть

        msg.answer.assert_not_awaited()
        msg.bot.edit_message_text.assert_not_awaited()
        data = await state.get_data()
        self.assertIsNone(data.get(flow._NAV_ANCHOR_MSG_KEY))  # по-прежнему не создан


class StartHandlerCleanupTests(unittest.IsolatedAsyncioTestCase):
    """Реальные хендлеры bot/handlers/start.py, переведённые на flow.py —
    /start реально пытается удалить триггер и предыдущий корневой экран,
    а не просто существует в коде."""

    async def test_cmd_start_deletes_trigger_and_tracks_new_root_screen(self):
        # /start теперь идёт через flow.reset_nav_screen — на свежем state
        # это создание NAV anchor'а (единственный answer()), а не отдельного
        # TRANSIENT-экрана (см. bot/flow.py — WELCOME сам и есть anchor).
        state = make_state()
        msg = make_flow_message(text="/start")
        await start.cmd_start(msg, state)
        msg.answer.assert_awaited_once()
        data = await state.get_data()
        self.assertEqual(data[flow._NAV_ANCHOR_MSG_KEY], 555)

    async def test_second_root_command_deletes_first_root_screen(self):
        # /start больше не создаёт TRANSIENT-экран (он ушёл в NAV anchor,
        # см. test_cmd_start_deletes_trigger... выше) — RULE 2 здесь
        # проверяется на двух TRANSIENT root-командах подряд.
        state = make_state()
        msg1 = make_flow_message(text="/portfolio")
        await start.cmd_portfolio(msg1, state)

        msg2 = make_flow_message(text="/about")
        await start.cmd_about(msg2, state)

        msg2.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=555)

    async def test_fallback_text_deletes_stray_message_and_shows_menu(self):
        state = make_state()
        msg = make_flow_message(text="случайный текст")
        await start.fallback_text(msg, state)
        # Свежий state -> первый answer() создаёт NAV anchor, второй — сам
        # fallback TRANSIENT-текст.
        self.assertEqual(msg.answer.await_count, 2)

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
        # Свежий state -> NAV anchor (1) + fallback TRANSIENT-текст (2).
        self.assertEqual(msg.answer.await_count, 2)
        sent_text = msg.answer.await_args.args[0]  # последний вызов — сам fallback-текст
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
        self.assertEqual(client_texts, {texts.MAIN_MENU_BUTTON, texts.MENU_FAQ})
        self.assertEqual(owner_texts, {texts.MAIN_MENU_BUTTON, texts.MENU_FAQ, texts.ADMIN_BUTTON})

    def test_reply_keyboard_for_chat_picks_correct_variant(self):
        # Общий helper (bot/keyboards.py) — используется исключительно
        # persistent NAV anchor'ом (bot/flow.py::_create_nav_anchor).
        # Сравнение с DESIGNER_CHAT_ID — та же проверка, что и в
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
        # open_flow теперь сам "освежает" reply-клавиатуру ДО отправки
        # основного inline-ответа (см. bot/flow.py::open_flow, UX-аудит про
        # исчезающую клавиатуру) — первый answer() это невидимый refresh,
        # последний — содержательный ответ (inline web_app-кнопка).
        last_call = msg.answer.await_args_list[-1]
        sent_markup = last_call.kwargs.get("reply_markup") or last_call.args[1]
        self.assertIsInstance(sent_markup, InlineKeyboardMarkup)
        btn = sent_markup.inline_keyboard[0][0]
        self.assertIsInstance(btn.web_app, WebAppInfo)
        self.assertTrue(btn.web_app.url.endswith("/portfolio"))

    async def test_admin_button_triggers_same_as_admin_command(self):
        # /admin отвечает inline-клавиатурой (admin_root_keyboard) -> теперь
        # получает автоматический refresh reply-клавиатуры от open_flow,
        # отсюда 2 answer() вместо 1 (см. UX-аудит про исчезающую клавиатуру).
        state = make_state(888)
        msg = make_flow_message(chat_id=888, text=texts.ADMIN_BUTTON)
        await admin.admin_button(msg, state)
        self.assertEqual(msg.answer.await_count, 2)

    def test_client_commands_are_start_and_faq_only(self):
        # portfolio/about/brief убраны из видимого command list — Menu Button
        # теперь ведёт прямо в Mini App, где эти разделы доступны как
        # навигация (webapp/js/app.js::TAB_SCREENS). Сами handlers НЕ
        # удалены (см. test_legacy_command_handlers_still_registered).
        import bot.main as bot_main

        command_names = [c.command for c in bot_main.CLIENT_COMMANDS]
        self.assertEqual(command_names, ["start", "faq"])
        self.assertNotIn("portfolio", command_names)
        self.assertNotIn("about", command_names)
        self.assertNotIn("brief", command_names)
        self.assertNotIn("calculator", command_names)

    def test_owner_command_scope_is_client_commands_plus_admin(self):
        import bot.main as bot_main

        owner_names = [c.command for c in bot_main.CLIENT_COMMANDS + bot_main.ADMIN_EXTRA_COMMANDS]
        self.assertEqual(owner_names, ["start", "faq", "admin"])

    async def test_legacy_command_handlers_still_registered(self):
        # /portfolio, /about, /brief убраны только из видимого command list —
        # handlers остаются рабочими deep-link'ами.
        state = make_state()
        for cmd, handler in (
            ("/portfolio", start.cmd_portfolio),
            ("/about", start.cmd_about),
            ("/brief", start.cmd_brief),
        ):
            msg = make_flow_message(text=cmd)
            await handler(msg, state)
            msg.answer.assert_awaited()

    async def test_setup_menu_button_is_webapp_direct_launch(self):
        # Финальная архитектура (см. UX-аудит "Telegram launch UX"): Menu
        # Button запускает Mini App напрямую, одним тапом — список команд
        # сокращён (CLIENT_COMMANDS) именно чтобы portfolio/about/brief не
        # "терялись" при этой смене (см. предыдущий откаченный эксперимент,
        # commit 2130dc7/a78dc81 — там список команд ещё не был сокращён).
        from aiogram.types import MenuButtonCommands, MenuButtonWebApp

        import bot.main as bot_main

        fake_bot = AsyncMock()
        await bot_main._setup_menu_button(fake_bot)
        fake_bot.set_chat_menu_button.assert_awaited_once()
        _, call_kwargs = fake_bot.set_chat_menu_button.call_args
        menu_button = call_kwargs["menu_button"]
        self.assertIsInstance(menu_button, MenuButtonWebApp)
        self.assertNotIsInstance(menu_button, MenuButtonCommands)
        self.assertEqual(menu_button.text, "Открыть приложение")
        self.assertEqual(menu_button.web_app.url, bot_main.config.WEBAPP_URL)

    def test_client_reply_keyboard_has_no_admin_button(self):
        client_texts = {btn.text for row in keyboards.main_reply_keyboard(is_owner=False).keyboard for btn in row}
        self.assertEqual(client_texts, {texts.MAIN_MENU_BUTTON, texts.MENU_FAQ})
        self.assertNotIn(texts.ADMIN_BUTTON, client_texts)

    def test_bot_description_texts_fit_telegram_limits(self):
        # setMyShortDescription <=120 символов, setMyDescription <=512
        # (см. bot/main.py::_setup_bot_description, UX-аудит п.4).
        self.assertLessEqual(len(texts.BOT_SHORT_DESCRIPTION), 120)
        self.assertLessEqual(len(texts.BOT_DESCRIPTION), 512)

    async def test_setup_bot_description_calls_both_setters_with_correct_text(self):
        import bot.main as bot_main

        fake_bot = AsyncMock()
        await bot_main._setup_bot_description(fake_bot)
        fake_bot.set_my_short_description.assert_awaited_once_with(short_description=texts.BOT_SHORT_DESCRIPTION)
        fake_bot.set_my_description.assert_awaited_once_with(description=texts.BOT_DESCRIPTION)

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

    def test_dispatcher_uses_simple_event_isolation(self):
        # См. UX-аудит race condition: Dispatcher по умолчанию (без
        # events_isolation) использует DisabledEventIsolation (no-op lock) +
        # polling(handle_as_tasks=True) — апдейты одного чата НЕ
        # сериализуются, что позволяло двум быстрым подряд нажатиям
        # "Главное меню" пройти check ensure_nav_anchor одновременно и
        # создать дублирующийся NAV anchor. SimpleEventIsolation — штатный
        # aiogram-механизм (per-StorageKey asyncio.Lock), не самодельный.
        # main() нельзя безопасно вызвать напрямую (реально стартует polling)
        # — проверяем исходный код, как и test_startup_does_not_drop_pending_updates.
        import inspect

        import bot.main as bot_main

        source = inspect.getsource(bot_main.main)
        self.assertIn("events_isolation=SimpleEventIsolation()", source)
        self.assertIn("SimpleEventIsolation", inspect.getsource(bot_main))  # реально импортирован в модуле

    def test_owner_reply_keyboard_has_admin_button(self):
        owner_texts = {btn.text for row in keyboards.main_reply_keyboard(is_owner=True).keyboard for btn in row}
        self.assertIn(texts.ADMIN_BUTTON, owner_texts)
        self.assertIn(texts.MAIN_MENU_BUTTON, owner_texts)
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

    def test_no_reply_keyboard_remove_anywhere_in_bot_package(self):
        # Статическая проверка: ReplyKeyboardRemove нигде не используется —
        # persistent-клавиатура не должна исчезать ни в одном сценарии
        # (см. UX-аудит "Telegram launch UX", п.12 про keyboard-persistence).
        bot_dir = Path(__file__).resolve().parent.parent / "bot"
        offenders = [
            str(path) for path in bot_dir.rglob("*.py")
            if "ReplyKeyboardRemove" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_no_persistent_cancel_button_for_client_or_owner(self):
        # "❌ Отмена" не бизнес-действие и не постоянная навигация — доступна
        # только контекстно (typed /cancel, inline cancel_keyboard() внутри
        # мастеров admin.py), не как persistent-кнопка reply-клавиатуры.
        for is_owner in (False, True):
            texts_seen = {btn.text for row in keyboards.main_reply_keyboard(is_owner=is_owner).keyboard for btn in row}
            self.assertNotIn("❌ Отмена", texts_seen)

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
        from aiogram.types import ReplyKeyboardMarkup

        state = make_state()
        msg = make_flow_message(text="/cancel")
        await start.cmd_cancel(msg, state)
        # Свежий state -> первый answer() создаёт NAV anchor (persistent
        # клавиатура); сам cancel-TRANSIENT-текст (последний вызов) её НЕ
        # несёт — reply_markup=None, клавиатуру уже обеспечивает NAV anchor
        # (см. bot/flow.py, UX-аудит про исчезающую клавиатуру после FAQ/admin).
        self.assertEqual(msg.answer.await_count, 2)
        nav_call = msg.answer.await_args_list[0]
        nav_markup = nav_call.kwargs.get("reply_markup") or nav_call.args[1]
        self.assertIsInstance(nav_markup, ReplyKeyboardMarkup)
        self.assertTrue(nav_markup.is_persistent)
        content_call = msg.answer.await_args_list[-1]
        self.assertIsNone(content_call.kwargs.get("reply_markup"))

    async def test_portfolio_about_brief_still_use_inline_webapp_keyboard(self):
        # Не меняли и не должны были менять поведение inline WebApp-кнопок —
        # эти хендлеры по-прежнему отвечают InlineKeyboardMarkup с web_app.
        # ПЕРВЫМ теперь идёт создание NAV anchor'а (см. bot/flow.py::
        # open_flow -> ensure_nav_anchor, свежий state — anchor'а ещё нет),
        # ВТОРЫМ — содержательный TRANSIENT-ответ.
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
            nav_call, content_call = msg.answer.await_args_list
            nav_markup = nav_call.kwargs.get("reply_markup") or nav_call.args[1]
            self.assertIsInstance(nav_markup, ReplyKeyboardMarkup)
            sent_markup = content_call.kwargs.get("reply_markup") or content_call.args[1]
            self.assertIsInstance(sent_markup, InlineKeyboardMarkup)
            btn = sent_markup.inline_keyboard[0][0]
            self.assertIsInstance(btn.web_app, WebAppInfo)
            self.assertTrue(btn.web_app.url.endswith(f"/{path}"))

    async def test_faq_command_still_uses_inline_faq_keyboard(self):
        # /faq на свежем state тоже сначала создаёт NAV anchor (см.
        # bot/flow.py::open_flow -> ensure_nav_anchor) — answer() вызывается
        # дважды, порядок как у portfolio/about/brief выше.
        from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

        msg = make_flow_message(text="/faq")
        await faq.cmd_faq(msg, make_state())
        self.assertEqual(msg.answer.await_count, 2)
        nav_call, content_call = msg.answer.await_args_list
        nav_markup = nav_call.kwargs.get("reply_markup") or nav_call.args[1]
        self.assertIsInstance(nav_markup, ReplyKeyboardMarkup)
        sent_markup = content_call.kwargs.get("reply_markup") or content_call.args[1]
        self.assertIsInstance(sent_markup, InlineKeyboardMarkup)

    async def test_main_menu_button_stays_plain_text_not_web_app(self):
        # main_reply_keyboard() уже проверяется на отсутствие web_app в
        # других тестах — здесь отдельно, явно, именно для "⌂ Главное меню"
        # (запуск Mini App теперь только через Telegram Menu Button, не
        # через эту reply-кнопку).
        markup = keyboards.main_reply_keyboard(is_owner=False)
        trigger_btn = next(
            btn for row in markup.keyboard for btn in row if btn.text == texts.MAIN_MENU_BUTTON
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
        # /admin отвечает inline-клавиатурой -> на свежем state open_flow
        # сам создаёт NAV anchor первым answer() (см. bot/flow.py::
        # ensure_nav_anchor), вторым — сам admin-контент.
        state = make_state(self.actor)
        msg1 = make_flow_message(chat_id=self.actor, text="/admin")
        await admin.cmd_admin(msg1, state)
        self.assertEqual(msg1.answer.await_count, 2)

        # Второй /admin в том же чате: NAV anchor уже существует (no-op) —
        # единственный delete_message здесь — RULE 2 (старый TRANSIENT admin-
        # экран из msg1).
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

        faq_items = await content_store.list_faq()
        self.assertTrue(any(i["question"] == "Сколько стоит лендинг?" and i["answer"] == "От 25 000 рублей" for i in faq_items))


class AdminMultiStepWizardAnchorTests(unittest.IsolatedAsyncioTestCase):
    """P1-3 аудит (read-only на commit 7c52a1d), Batch 1: 5 multi-step
    wizard'ов, гарантированно получавших stale _flow_msg_id уже на
    ВАЛИДНОМ пути (не только на retry) — их continuation message.answer()
    заменены на flow.step_from_text (тот же RULE 3 primitive, что уже
    работает в FAQ-add wizard, см. AdminCleanupFlowTests выше). Здесь — не
    сам primitive (уже покрыт FlowUtilTests/AdminCleanupFlowTests), а то,
    что РЕАЛЬНЫЕ хендлеры используют его так, что anchor остаётся
    синхронизирован через 2+ шага и /cancel после второго шага удаляет
    актуальный prompt, а не осиротевшее сообщение с первого шага.

    about_experience_add_role и option_add_name мигрированы вместе со
    своими соседями по цепочке, хотя явно не были названы в scope Batch 1:
    без них company/price (уже названные) редактировали бы сообщение,
    точно так же, как раньше — их правка была бы no-op. См. финальный
    отчёт коммита."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "777"
        self.actor = 777

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_anchor(self, anchor_id: int = 500, nav_msg_id: int = 111, **extra_data) -> FSMContext:
        # anchor_id имитирует _flow_msg_id, уже установленный /admin (open_root)
        # и сохраняемый в неизменном виде через чисто callback-навигацию —
        # именно такое состояние застаёт первый TEXT-хендлер мастера в проде
        # (см. аудит: raw callback.message.edit_text не трогает этот ключ,
        # но сохраняет message_id физически тем же).
        state = make_state(self.actor)
        await state.update_data(**{
            flow._ANCHOR_MSG_KEY: anchor_id,
            flow._ANCHOR_CHAT_KEY: self.actor,
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            **extra_data,
        })
        return state

    # ---- 1. Case add: callback prompt A -> text -> prompt B -> /cancel ----
    async def test_case_add_cancel_after_photo_step_deletes_current_prompt(self):
        state = await self._state_with_anchor(anchor_id=500, cancel_to="cases")
        await admin.cases_add_start(make_callback("admincasesaction:add", chat_id=self.actor), state)
        await admin.cases_add_category(make_callback("admincat:landing", chat_id=self.actor), state)

        title_msg = make_flow_message_factory(chat_id=self.actor, start_id=4000)(text="Новый лендинг")
        await admin.cases_add_title(title_msg, state)
        title_msg.bot.edit_message_text.assert_awaited_once_with(
            "Пришлите фото кейса (как фото):", chat_id=self.actor, message_id=500, reply_markup=kb.cancel_keyboard()
        )
        title_msg.answer.assert_not_awaited()  # RULE 3: редактирование на месте, не новое сообщение

        photo_msg = make_photo_message(self.actor)
        await admin.cases_add_photo(photo_msg, state)
        photo_msg.bot.edit_message_text.assert_awaited_once_with(
            "Короткое описание задачи (пара предложений):", chat_id=self.actor, message_id=500, reply_markup=kb.cancel_keyboard()
        )
        photo_msg.answer.assert_not_awaited()

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 500)  # anchor не уехал за 2 шага

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=4100)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        # актуальный prompt (500) удалён, а не какое-то стороннее сообщение —
        # именно та проверка, которую старое поведение (raw message.answer)
        # не прошло бы: см. discriminating-эксперимент в отчёте.
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=500)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # NAV не задет

    # ---- 2. Service add: 2+ последовательных text step (+ retry) -> /cancel ----
    async def test_service_add_retry_and_multistep_cancel_deletes_current_prompt(self):
        state = await self._state_with_anchor(anchor_id=600, cancel_to="pricing")
        await admin.price_add_start(make_callback("adminpriceaction:add", chat_id=self.actor), state)

        name_msg = make_flow_message_factory(chat_id=self.actor, start_id=4200)(text="Лендинг")
        await admin.price_add_name(name_msg, state)
        name_msg.bot.edit_message_text.assert_awaited_once_with(
            "Базовая цена, ₽ (число):", chat_id=self.actor, message_id=600, reply_markup=kb.cancel_keyboard()
        )

        invalid_price_msg = make_flow_message_factory(chat_id=self.actor, start_id=4300)(text="не число")
        await admin.price_add_price(invalid_price_msg, state)
        invalid_price_msg.bot.edit_message_text.assert_awaited_once_with(
            "Нужно число, например 25000. Попробуйте ещё раз:", chat_id=self.actor, message_id=600, reply_markup=kb.cancel_keyboard()
        )
        invalid_price_msg.answer.assert_not_awaited()  # retry тоже не создаёт orphan

        price_msg = make_flow_message_factory(chat_id=self.actor, start_id=4400)(text="25000")
        await admin.price_add_price(price_msg, state)
        price_msg.bot.edit_message_text.assert_awaited_once_with(
            "Минимальный срок, дней (число):", chat_id=self.actor, message_id=600, reply_markup=kb.cancel_keyboard()
        )

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 600)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=4500)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=600)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 3. Case sections add: continuation step -> /cancel ----
    async def test_case_sections_add_cancel_after_title_step_deletes_current_prompt(self):
        state = await self._state_with_anchor(anchor_id=700, case_id="case_x", cancel_to="sections")
        await admin.case_section_add_start(make_callback("admincasesecaction:add", chat_id=self.actor), state)
        await admin.case_section_add_type(make_callback("admincasesectype:text", chat_id=self.actor), state)

        title_msg = make_flow_message_factory(chat_id=self.actor, start_id=4600)(text="Задача")
        await admin.case_section_add_title(title_msg, state)
        title_msg.bot.edit_message_text.assert_awaited_once_with(
            "Текст раздела:", chat_id=self.actor, message_id=700, reply_markup=kb.cancel_keyboard()
        )

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 700)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=4700)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=700)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 4. About experience add: 2+ последовательных text step -> /cancel ----
    async def test_about_experience_add_cancel_after_two_steps_deletes_current_prompt(self):
        state = await self._state_with_anchor(anchor_id=800, cancel_to="root")
        await admin.about_experience_add_start(make_callback("adminaboutexpaction:add", chat_id=self.actor), state)

        role_msg = make_flow_message_factory(chat_id=self.actor, start_id=4800)(text="Дизайнер")
        await admin.about_experience_add_role(role_msg, state)
        role_msg.bot.edit_message_text.assert_awaited_once_with(
            "Компания / проект:", chat_id=self.actor, message_id=800, reply_markup=kb.cancel_keyboard()
        )

        company_msg = make_flow_message_factory(chat_id=self.actor, start_id=4900)(text="Acme")
        await admin.about_experience_add_company(company_msg, state)
        company_msg.bot.edit_message_text.assert_awaited_once_with(
            "Период (например «2019 — настоящее время»):", chat_id=self.actor, message_id=800, reply_markup=kb.cancel_keyboard()
        )

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 800)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=5000)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=800)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 5. Option add: последовательные text step -> /cancel ----
    async def test_option_add_cancel_after_two_steps_deletes_current_prompt(self):
        state = await self._state_with_anchor(anchor_id=900, service_id="LEND", cancel_to="options")
        await state.set_state(AdminStates.edit_service_field_pick)
        await admin.option_action(make_callback("adminoptaction:add", chat_id=self.actor), state)

        name_msg = make_flow_message_factory(chat_id=self.actor, start_id=5100)(text="Доп. страница")
        await admin.option_add_name(name_msg, state)
        name_msg.bot.edit_message_text.assert_awaited_once_with(
            "Цена опции, +₽ (число):", chat_id=self.actor, message_id=900, reply_markup=kb.cancel_keyboard()
        )

        price_msg = make_flow_message_factory(chat_id=self.actor, start_id=5200)(text="3000")
        await admin.option_add_price(price_msg, state)
        price_msg.bot.edit_message_text.assert_awaited_once_with(
            "Срок опции, +дней (число, можно дробное, например 0.5):", chat_id=self.actor, message_id=900, reply_markup=kb.cancel_keyboard()
        )

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 900)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=5300)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=900)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 7. ensure_nav_anchor() после multi-step cancel не дублирует WELCOME ----
    async def test_ensure_nav_anchor_after_multistep_cancel_does_not_recreate(self):
        state = await self._state_with_anchor(anchor_id=500, cancel_to="cases")
        await admin.cases_add_start(make_callback("admincasesaction:add", chat_id=self.actor), state)
        await admin.cases_add_category(make_callback("admincat:landing", chat_id=self.actor), state)
        title_msg = make_flow_message_factory(chat_id=self.actor, start_id=5400)(text="Тест")
        await admin.cases_add_title(title_msg, state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=5500)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=5600)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 8. Успешное завершение каждого мастера — бизнес-результат/state не сломаны ----
    # (case add и case-sections add уже покрыты существующими
    # AdminNavAnchorResetTests.test_completing_add_case_wizard_preserves_nav_anchor
    # и AdminCaseConstructorTests.test_opening_sections_menu_does_not_crash_and_add_edit_works
    # — оба обновлены под make_flow_message_factory в этом же батче и
    # продолжают проходить. Ниже — недостающее покрытие для трёх остальных.)

    async def test_service_add_full_wizard_completes_with_correct_business_result(self):
        state = make_state(self.actor)
        await admin.price_add_start(make_callback("adminpriceaction:add", chat_id=self.actor), state)
        make_msg = make_flow_message_factory(chat_id=self.actor, start_id=6000)
        await admin.price_add_name(make_msg(text="Тестовая услуга"), state)
        await admin.price_add_price(make_msg(text="15000"), state)
        await admin.price_add_term_min(make_msg(text="3"), state)
        await admin.price_add_term_max(make_msg(text="10"), state)
        await admin.price_add_includes(make_msg(text="Дизайн + вёрстка"), state)

        self.assertIsNone(await state.get_state())
        services = await content_store.list_services()
        added = next((s for s in services if s["name"] == "Тестовая услуга"), None)
        self.assertIsNotNone(added)
        self.assertEqual(added["base_price"], 15000)
        self.assertEqual(added["term_min"], 3)
        self.assertEqual(added["term_max"], 10)
        self.assertEqual(added["includes"], "Дизайн + вёрстка")

    async def test_about_experience_add_full_wizard_completes_with_correct_business_result(self):
        state = make_state(self.actor)
        await admin.about_experience_add_start(make_callback("adminaboutexpaction:add", chat_id=self.actor), state)
        make_msg = make_flow_message_factory(chat_id=self.actor, start_id=6100)
        await admin.about_experience_add_role(make_msg(text="Дизайнер"), state)
        await admin.about_experience_add_company(make_msg(text="Acme"), state)
        await admin.about_experience_add_period(make_msg(text="2020 — 2022"), state)
        await admin.about_experience_add_description(make_msg(text="-"), state)

        self.assertEqual(await state.get_state(), AdminStates.about_experience_menu.state)
        entries = (await content_store.get_about()).get("experience", [])
        added = next((e for e in entries if e["role"] == "Дизайнер" and e["company"] == "Acme"), None)
        self.assertIsNotNone(added)
        self.assertEqual(added["period"], "2020 — 2022")
        self.assertEqual(added["description"], "")  # "-" -> пустая строка, как и раньше

    async def test_option_add_full_wizard_completes_with_correct_business_result(self):
        services = await content_store.list_services()
        service_id = services[0]["id"]
        state = make_state(self.actor)
        await state.update_data(service_id=service_id)
        await state.set_state(AdminStates.edit_service_field_pick)
        await admin.option_action(make_callback("adminoptaction:add", chat_id=self.actor), state)
        make_msg = make_flow_message_factory(chat_id=self.actor, start_id=6200)
        await admin.option_add_name(make_msg(text="Доп. страница"), state)
        await admin.option_add_price(make_msg(text="3000"), state)
        await admin.option_add_days(make_msg(text="2"), state)
        await admin.option_add_multipliable(make_callback("adminoptmultipliable:yes", chat_id=self.actor), state)

        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick.state)
        options = await content_store.list_options(service_id)
        added = next((o for o in options if o["name"] == "Доп. страница"), None)
        self.assertIsNotNone(added)
        self.assertEqual(added["price"], 3000)
        self.assertEqual(added["days"], 2)
        self.assertTrue(added["multipliable"])

    # ---- 9. Inline "❌ Отмена" не затронута миграцией этих 5 мастеров ----
    async def test_inline_cancel_unaffected_by_multistep_wizard_migration(self):
        state = await self._state_with_anchor(anchor_id=500, cancel_to="cases")
        cb = make_callback("admincancel", chat_id=self.actor)
        await admin.admin_cancel(cb, state)
        cb.message.edit_text.assert_awaited_once()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)


class AdminRetryFragileFlowAnchorTests(unittest.IsolatedAsyncioTestCase):
    """P1-3 аудит (read-only на commit 7c52a1d), Batch 2: 13 single-step
    wizard'ов, у которых _flow_msg_id оставался корректным на первой
    попытке, но становился stale именно на invalid/wrong-type RETRY (не на
    валидном пути — это Batch 1). Fresh-аудит на commit e5e7ad1 нашёл 11
    реальных retry-веток в 9 хендлерах (см. bot/handlers/admin.py) — все
    они теперь используют flow.step_from_text вместо raw message.answer.

    Явно НЕ мигрированы (см. commit message и финальный отчёт): "success"/
    terminal-ветки этих же хендлеров (например "Обновлено ✅..." — Type C
    по классификации из задачи) и 3 из 4 error-веток backup_import_receive
    (BackupValidationError/BackupSnapshotError/BackupRestoreFailedError —
    они переводят в AdminStates.backup_menu, т.е. НЕ retry-loop, а
    terminal-результат, несмотря на слово "попробуйте" в тексте)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "444"
        self.actor = 444

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_anchor(self, anchor_id: int = 500, nav_msg_id: int = 111, **extra_data) -> FSMContext:
        # anchor_id имитирует _flow_msg_id, установленный тем callback'ом,
        # что открыл текущий prompt (case_image_add_start/about_edit_field/
        # price_edit_field/...) — все они raw callback.message.edit_text,
        # который сохраняет anchor актуальным чисто за счёт того, что
        # message_id не меняется (см. Batch 1 отчёт).
        state = make_state(self.actor)
        await state.update_data(**{
            flow._ANCHOR_MSG_KEY: anchor_id,
            flow._ANCHOR_CHAT_KEY: self.actor,
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            **extra_data,
        })
        return state

    # ---- numeric validation: price_edit_value ----
    async def test_price_edit_value_retry_then_second_retry_keeps_same_anchor(self):
        services = await content_store.list_services()
        service_id = services[0]["id"]
        state = await self._state_with_anchor(anchor_id=500, service_id=service_id, field="base_price", cancel_to="pricing")

        bad1 = make_flow_message_factory(chat_id=self.actor, start_id=7000)(text="не число")
        await admin.price_edit_value(bad1, state)
        bad1.bot.edit_message_text.assert_awaited_once_with(
            "Нужно число. Попробуйте ещё раз:", chat_id=self.actor, message_id=500, reply_markup=kb.cancel_keyboard()
        )
        bad1.answer.assert_not_awaited()

        # второй invalid input подряд — редактирует ТО ЖЕ сообщение (500),
        # а не создаёт цепочку orphan-сообщений B->C
        bad2 = make_flow_message_factory(chat_id=self.actor, start_id=7100)(text="снова не число")
        await admin.price_edit_value(bad2, state)
        bad2.bot.edit_message_text.assert_awaited_once_with(
            "Нужно число. Попробуйте ещё раз:", chat_id=self.actor, message_id=500, reply_markup=kb.cancel_keyboard()
        )
        bad2.answer.assert_not_awaited()

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 500)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=7200)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=500)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_price_edit_value_valid_after_retry_updates_service_and_state(self):
        services = await content_store.list_services()
        service_id = services[0]["id"]
        state = await self._state_with_anchor(anchor_id=500, service_id=service_id, field="base_price", cancel_to="pricing")

        await admin.price_edit_value(make_flow_message_factory(chat_id=self.actor, start_id=7300)(text="не число"), state)
        good = make_flow_message_factory(chat_id=self.actor, start_id=7400)(text="99000")
        await admin.price_edit_value(good, state)
        # success-ветка теперь тоже flow.step_from_text (P1-3, Batch 3) —
        # редактирует тот же anchor (500), а не отправляет новое сообщение.
        good.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто изменить?", chat_id=self.actor, message_id=500, reply_markup=kb.service_field_keyboard()
        )
        good.answer.assert_not_awaited()
        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick.state)
        updated = await content_store.get_service(service_id)
        self.assertEqual(updated["base_price"], 99000)

    # ---- numeric validation: price_coef_value, option_edit_value_text ----
    async def test_price_coef_value_retry_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=1100, kind="coef", key="urgency", cancel_to="pricing")
        bad = make_flow_message_factory(chat_id=self.actor, start_id=8400)(text="не число")
        await admin.price_coef_value(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужно число. Попробуйте ещё раз:", chat_id=self.actor, message_id=1100, reply_markup=kb.cancel_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 1100)

    async def test_option_edit_value_text_retry_keeps_anchor_synced(self):
        services = await content_store.list_services()
        service_id = services[0]["id"]
        option_id = await content_store.next_option_id(service_id)
        await content_store.add_option(
            str(self.actor), option_id=option_id, service_id=service_id,
            name="Тест опция", price=1000, days=1, multipliable=False,
        )
        state = await self._state_with_anchor(anchor_id=1000, option_id=option_id, field="price", cancel_to="options")

        bad = make_flow_message_factory(chat_id=self.actor, start_id=8300)(text="не число")
        await admin.option_edit_value_text(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужно число. Попробуйте ещё раз:", chat_id=self.actor, message_id=1000, reply_markup=kb.cancel_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 1000)

    # ---- text validation: cases_edit_value, case_section_edit_value ----
    async def test_cases_edit_value_text_retry_deletes_current_prompt_on_cancel(self):
        await content_store.add_case(
            str(self.actor), case_id="case_retry", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        state = await self._state_with_anchor(anchor_id=600, case_id="case_retry", field="title", cancel_to="cases")

        bad = make_flow_message_factory(chat_id=self.actor, start_id=7500)(text=None)
        await admin.cases_edit_value(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужен текст.", chat_id=self.actor, message_id=600, reply_markup=kb.cancel_keyboard()
        )
        bad.answer.assert_not_awaited()

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 600)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=7600)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=600)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_case_section_edit_value_text_retry_keeps_anchor_synced(self):
        await content_store.add_case(
            str(self.actor), case_id="case_sec", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        await content_store.add_case_section(str(self.actor), "case_sec", section_type="text", title="Задача", content="старый текст")
        state = await self._state_with_anchor(
            anchor_id=900, case_id="case_sec", section_index=0, section_field="content", cancel_to="sections",
        )

        bad = make_flow_message_factory(chat_id=self.actor, start_id=8100)(text=None)
        await admin.case_section_edit_value(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужен текст.", chat_id=self.actor, message_id=900, reply_markup=kb.cancel_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 900)

    # ---- photo/file validation: cases_edit_value(cover), case_section_edit_value(addimg),
    #      case_image_add_wrong, about_edit_photo_wrong ----
    async def test_cases_edit_value_photo_retry_keeps_anchor_synced(self):
        await content_store.add_case(
            str(self.actor), case_id="case_retry2", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        state = await self._state_with_anchor(anchor_id=610, case_id="case_retry2", field="cover", cancel_to="cases")

        bad = make_flow_message_factory(chat_id=self.actor, start_id=7700)(text="это не фото")
        bad.photo = None
        bad.document = None
        await admin.cases_edit_value(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужно фото 📎.", chat_id=self.actor, message_id=610, reply_markup=kb.cancel_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 610)

    async def test_case_section_edit_value_photo_retry_keeps_anchor_synced(self):
        await content_store.add_case(
            str(self.actor), case_id="case_sec2", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        await content_store.add_case_section(str(self.actor), "case_sec2", section_type="gallery", title="Галерея", images=[])
        state = await self._state_with_anchor(
            anchor_id=910, case_id="case_sec2", section_index=0, section_field="addimg", cancel_to="sections",
        )

        bad = make_flow_message_factory(chat_id=self.actor, start_id=8200)(text="не фото")
        bad.photo = None
        bad.document = None
        await admin.case_section_edit_value(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужно фото 📎.", chat_id=self.actor, message_id=910, reply_markup=kb.cancel_keyboard()
        )

    async def test_case_image_add_wrong_deletes_current_prompt_on_cancel(self):
        state = await self._state_with_anchor(anchor_id=700, case_id="case_img", cancel_to="images")
        bad = make_flow_message_factory(chat_id=self.actor, start_id=7800)(text="не фото")
        await admin.case_image_add_wrong(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужно фото 📎.", chat_id=self.actor, message_id=700, reply_markup=kb.cancel_keyboard()
        )
        bad.answer.assert_not_awaited()

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 700)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=7900)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=700)

    async def test_about_edit_photo_wrong_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=800, field="avatar", cancel_to="root")
        bad = make_flow_message_factory(chat_id=self.actor, start_id=8000)(text="не фото")
        await admin.about_edit_photo_wrong(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужно фото 📎.", chat_id=self.actor, message_id=800, reply_markup=kb.cancel_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 800)

    # ---- backup/import validation: backup_import_receive (BadZipFile only), backup_import_wrong ----
    async def test_backup_import_bad_zip_retry_deletes_current_prompt_on_cancel(self):
        state = await self._state_with_anchor(anchor_id=1200, cancel_to="backup")
        bad = make_flow_message_factory(chat_id=self.actor, start_id=8500)()
        bad.document = SimpleNamespace(file_id="fake_zip_id")
        bad.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="documents/backup.zip"))
        bad.bot.download_file = AsyncMock(return_value=io.BytesIO(b"not a zip"))

        await admin.backup_import_receive(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Файл повреждён или не .zip — пришлите другой файл.", chat_id=self.actor, message_id=1200, reply_markup=kb.cancel_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 1200)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=8600)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=1200)

    async def test_backup_import_wrong_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=1300, cancel_to="backup")
        bad = make_flow_message_factory(chat_id=self.actor, start_id=8700)(text="случайный текст")
        await admin.backup_import_wrong(bad, state)
        bad.bot.edit_message_text.assert_awaited_once_with(
            "Нужен .zip файл 📎.", chat_id=self.actor, message_id=1300, reply_markup=kb.cancel_keyboard()
        )

    # ---- NAV preservation / ensure_nav_anchor после cancel-после-retry ----
    async def test_ensure_nav_anchor_after_retry_cancel_does_not_recreate(self):
        services = await content_store.list_services()
        service_id = services[0]["id"]
        state = await self._state_with_anchor(anchor_id=500, service_id=service_id, field="base_price", cancel_to="pricing")
        await admin.price_edit_value(make_flow_message_factory(chat_id=self.actor, start_id=8800)(text="не число"), state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=8900)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=9000)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- Inline "❌ Отмена" не затронута этим batch'ем ----
    async def test_inline_cancel_unaffected_by_retry_fix(self):
        state = await self._state_with_anchor(anchor_id=500, cancel_to="cases")
        cb = make_callback("admincancel", chat_id=self.actor)
        with patch("bot.handlers.admin.flow.cancel_transient", new=AsyncMock()) as mocked:
            await admin.admin_cancel(cb, state)
        mocked.assert_not_awaited()
        cb.message.edit_text.assert_awaited_once()


class AdminSuccessorStalenessAnchorTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 3 (read-only audit confirmed on commit 599071e): 11
    handlers whose SUCCESS/terminal message.answer() (not a retry — Batch 2
    already covers retries) transitions into an active AdminStates state
    with a persisted cancel_to, so _flow_msg_id went stale on the normal,
    valid path — the exact mechanism Batch 1 fixed for multi-step wizards,
    here occurring in single-response wizards. All 15 call sites now use
    flow.step_from_text instead of raw message.answer.

    Deliberately NOT touched (confirmed false positives / no practical
    effect during the audit, see report): admin_unexpected_input (never
    replaces the actual awaited prompt — a supplementary aside, not a
    competing screen), cat_add_label/cat_rename_value/lead_reply_send's
    "lead not found" branch (all call flow.reset_state_keep_nav, so state
    is None afterward and /cancel's StateFilter(AdminStates) no longer
    matches), backup_export's BackupExportError branch (rare failure mode,
    never replaces callback.message as the current screen)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "333"
        self.actor = 333

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_anchor(self, anchor_id: int = 500, nav_msg_id: int = 111, **extra_data) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._ANCHOR_MSG_KEY: anchor_id,
            flow._ANCHOR_CHAT_KEY: self.actor,
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            **extra_data,
        })
        return state

    # ---- case_image_add_receive ----
    async def test_case_image_add_receive_cancel_deletes_current_prompt(self):
        await content_store.add_case(
            str(self.actor), case_id="case_img", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        state = await self._state_with_anchor(anchor_id=500, case_id="case_img", cancel_to="images")
        photo = make_photo_message(self.actor)
        await admin.case_image_add_receive(photo, state)
        photo.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(photo.bot.edit_message_text.await_args.kwargs["chat_id"], self.actor)
        self.assertEqual(photo.bot.edit_message_text.await_args.kwargs["message_id"], 500)
        photo.answer.assert_not_awaited()
        self.assertEqual(await state.get_state(), AdminStates.case_images_menu.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=9300)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=500)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- case_section_edit_value / cases_edit_value (success) ----
    async def test_case_section_edit_value_success_keeps_anchor_synced(self):
        await content_store.add_case(
            str(self.actor), case_id="case_sec", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        await content_store.add_case_section(str(self.actor), "case_sec", section_type="text", title="Задача", content="старый")
        state = await self._state_with_anchor(
            anchor_id=600, case_id="case_sec", section_index=0, section_field="content", cancel_to="sections",
        )
        msg = make_flow_message_factory(chat_id=self.actor, start_id=9400)(text="новый текст")
        await admin.case_section_edit_value(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 600)
        msg.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 600)

    async def test_cases_edit_value_success_keeps_anchor_synced(self):
        await content_store.add_case(
            str(self.actor), case_id="case_val", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        state = await self._state_with_anchor(anchor_id=610, case_id="case_val", field="title", cancel_to="cases")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=9500)(text="Новое название")
        await admin.cases_edit_value(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто ещё изменить?", chat_id=self.actor, message_id=610, reply_markup=kb.case_field_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 610)

    # ---- faq_edit_value ----
    async def test_faq_edit_value_success_keeps_anchor_synced(self):
        item = await content_store.add_faq(str(self.actor), "Старый вопрос?", "Старый ответ")
        state = await self._state_with_anchor(anchor_id=620, faq_id=item["id"], field="answer", cancel_to="faq")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=9600)(text="Новый ответ")
        await admin.faq_edit_value(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто ещё изменить?", chat_id=self.actor, message_id=620, reply_markup=kb.faq_field_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 620)

    # ---- about_edit_photo / about_edit_value ----
    async def test_about_edit_photo_success_deletes_current_prompt_on_cancel(self):
        state = await self._state_with_anchor(anchor_id=630, field="avatar")
        photo = make_photo_message(self.actor)
        await admin.about_edit_photo(photo, state)
        photo.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(photo.bot.edit_message_text.await_args.kwargs["message_id"], 630)
        self.assertEqual(await state.get_state(), AdminStates.edit_about_field_pick.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=9700)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=630)

    async def test_about_edit_value_success_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=640, field="location")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=9800)(text="Москва")
        await admin.about_edit_value(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 640)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 640)

    # ---- price_edit_value / price_coef_value / option_edit_value_text (success) ----
    async def test_price_coef_value_success_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=650, kind="coef", key="urgency", cancel_to="pricing")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=9900)(text="1.3")
        await admin.price_coef_value(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто ещё изменить?", chat_id=self.actor, message_id=650, reply_markup=kb.coefficients_menu_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 650)

    async def test_option_edit_value_text_success_keeps_anchor_synced(self):
        services = await content_store.list_services()
        service_id = services[0]["id"]
        option_id = await content_store.next_option_id(service_id)
        await content_store.add_option(
            str(self.actor), option_id=option_id, service_id=service_id,
            name="Тест опция", price=1000, days=1, multipliable=False,
        )
        state = await self._state_with_anchor(anchor_id=660, option_id=option_id, field="price", cancel_to="options")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=10000)(text="2000")
        await admin.option_edit_value_text(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто изменить?", chat_id=self.actor, message_id=660, reply_markup=kb.option_field_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 660)

    # ---- lead_reply_send (success) ----
    async def test_lead_reply_send_success_deletes_current_prompt_on_cancel(self):
        lead = await content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Задача"},
            {"user_id": 55555, "username": "client", "first_name": "Клиент"},
        )
        state = await self._state_with_anchor(anchor_id=670, lead_id=lead["id"], cancel_to="root")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=10100)(text="Ответ клиенту")
        msg.bot.send_message = AsyncMock()
        await admin.lead_reply_send(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 670)
        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=10200)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=670)

    # ---- backup_import_receive: остальные 4 не-retry ветки (BadZipFile уже в Batch 2) ----
    def _backup_message(self, anchor_id: int) -> SimpleNamespace:
        msg = make_flow_message_factory(chat_id=self.actor, start_id=anchor_id * 10)()
        msg.document = SimpleNamespace(file_id="fake_zip_id")
        msg.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="documents/backup.zip"))
        msg.bot.download_file = AsyncMock(return_value=io.BytesIO(b"irrelevant, import_backup_bytes is mocked"))
        return msg

    async def test_backup_import_validation_error_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=680, cancel_to="backup")
        msg = self._backup_message(680)
        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupValidationError("pricing.json", "not valid JSON", ["pricing.json"])),
        ):
            await admin.backup_import_receive(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 680)
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_backup_import_snapshot_error_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=690, cancel_to="backup")
        msg = self._backup_message(690)
        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupSnapshotError("pricing.json")),
        ):
            await admin.backup_import_receive(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 690)

    async def test_backup_import_restore_failed_rollback_failed_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=700, cancel_to="backup")
        msg = self._backup_message(700)
        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupRestoreFailedError("pricing.json", ["faq.json"], ["pricing.json"])),
        ):
            await admin.backup_import_receive(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 700)

    async def test_backup_import_restore_failed_rollback_ok_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=710, cancel_to="backup")
        msg = self._backup_message(710)
        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupRestoreFailedError("pricing.json", ["faq.json"], [])),
        ):
            await admin.backup_import_receive(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 710)

    async def test_backup_import_success_deletes_current_prompt_on_cancel(self):
        state = await self._state_with_anchor(anchor_id=720, cancel_to="backup")
        msg = self._backup_message(720)
        fake_result = SimpleNamespace(restored_json=["pricing.json"], missing_json=[], restored_images=[], failed_images=[])
        with patch("bot.content_store.import_backup_bytes", new=AsyncMock(return_value=fake_result)):
            await admin.backup_import_receive(msg, state)
        msg.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(msg.bot.edit_message_text.await_args.kwargs["message_id"], 720)
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=10300)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=720)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- ensure_nav_anchor() после cancel не дублирует WELCOME ----
    async def test_ensure_nav_anchor_after_successor_staleness_cancel_does_not_recreate(self):
        state = await self._state_with_anchor(anchor_id=630, field="avatar")
        await admin.about_edit_photo(make_photo_message(self.actor), state)
        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=10400)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=10500)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- Inline "❌ Отмена" не затронута этим batch'ем ----
    async def test_inline_cancel_unaffected_by_successor_staleness_fix(self):
        state = await self._state_with_anchor(anchor_id=500, cancel_to="cases")
        cb = make_callback("admincancel", chat_id=self.actor)
        with patch("bot.handlers.admin.flow.cancel_transient", new=AsyncMock()) as mocked:
            await admin.admin_cancel(cb, state)
        mocked.assert_not_awaited()
        cb.message.edit_text.assert_awaited_once()


class AdminNavAnchorResetTests(unittest.IsolatedAsyncioTestCase):
    """P1-3 аудит, Batch 0: aiogram FSMContext.clear() == set_state(None) +
    set_data({}) — стирает ВЕСЬ per-chat data dict, включая
    flow._NAV_ANCHOR_MSG_KEY/_NAV_ANCHOR_CHAT_KEY, хотя физическое
    NAV-сообщение (persistent reply-клавиатура) никуда не девается —
    следующий flow.ensure_nav_anchor() создавал бы дублирующий WELCOME.
    Все 24 call site'а state.clear() в admin.py заменены на
    flow.reset_state_keep_nav — здесь проверяется каждый КЛАСС сценария
    через реальные хендлеры (не сам helper в изоляции): простая навигация,
    завершение мастера, отмена, удаление/редактирование, и что именно
    происходит ПОСЛЕ сброса (ensure_nav_anchor, "Главное меню")."""

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

    async def _state_with_nav(self, nav_msg_id: int = 111, **extra_data) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            **extra_data,
        })
        return state

    # ---- 1/6/7 (сценарий A): простая admin-навигация ----
    async def test_simple_admin_navigation_preserves_nav_anchor(self):
        state = await self._state_with_nav(case_id="stale-value", cancel_to="cases")
        cb = make_callback("adminmenu:cases", chat_id=self.actor)
        await admin.menu_cases(cb, state)

        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # 6: тот же message_id
        self.assertEqual(data.get(flow._NAV_ANCHOR_CHAT_KEY), self.actor)
        self.assertNotIn("case_id", data)  # 7: admin-local данные реально стёрты
        self.assertNotIn("cancel_to", data)

    # ---- 2: завершение полного мастера (add case, 4 шага: callback×2, text, photo, text) ----
    async def test_completing_add_case_wizard_preserves_nav_anchor(self):
        make_msg = make_flow_message_factory(chat_id=self.actor, start_id=2000)
        state = await self._state_with_nav()

        await admin.cases_add_start(make_callback("admincasesaction:add", chat_id=self.actor), state)
        await admin.cases_add_category(make_callback("admincat:landing", chat_id=self.actor), state)
        await admin.cases_add_title(make_msg(text="Новый лендинг"), state)
        await admin.cases_add_photo(make_photo_message(self.actor), state)
        final_msg = make_msg(text="Короткое описание задачи")
        await admin.cases_add_description(final_msg, state)  # один из 24 сайтов

        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        # P1-3, Batch 4: cases_add_description теперь использует
        # flow.finish_flow (не reset_state_keep_nav) после step_from_text
        # — оставшиеся admin-local ключи (case_id/title/type_id/cover)
        # больше НЕ стираются намеренно: их сохранность — не баг, а именно
        # то, что позволяет _flow_msg_id/_flow_chat_id (тоже в data)
        # пережить этот шаг, чтобы "⌂ Главное меню" сразу после завершения
        # мастера могло найти и удалить актуальный финальный экран, а не
        # оставить его orphan (см. AdminRemainingAnchorGapsTests). Раньше
        # этот тест ошибочно требовал их отсутствия — assertNotIn ниже
        # заменены на assertIn ровно для _flow_msg_id, единственного ключа,
        # чья сохранность здесь действительно important.
        self.assertIn(flow._ANCHOR_MSG_KEY, data)
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), final_msg.bot.edit_message_text.await_args.kwargs["message_id"])
        # сама бизнес-операция не пострадала — кейс реально создан
        cases = await content_store.list_cases()
        self.assertTrue(any(c["title"] == "Новый лендинг" for c in cases))

        # И главное — Главное меню сразу после завершения мастера
        # действительно удаляет актуальный финальный экран, а не оставляет
        # его orphan (то, что было физически невозможно проверить до
        # исправления: anchor был бы уже стёрт).
        trigger = make_flow_message_factory(chat_id=self.actor, start_id=2100)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        trigger.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=data.get(flow._ANCHOR_MSG_KEY))

    # ---- 3 (сценарий, близкий к B): отмена внутри confirm-диалога ("Нет") ----
    async def test_cancelling_delete_confirmation_preserves_nav_anchor(self):
        cases = await content_store.list_cases()
        target_id = cases[0]["id"]
        state = await self._state_with_nav(case_id=target_id)
        await admin.cases_delete_do(make_callback("admindelcaseconfirm:no", chat_id=self.actor), state)

        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertNotIn("case_id", data)
        # это отмена, не удаление — кейс должен остаться на месте
        still_there = next((c for c in await content_store.list_cases() if c["id"] == target_id), None)
        self.assertIsNotNone(still_there)

    # ---- 4: удаление ----
    async def test_delete_faq_preserves_nav_anchor(self):
        item = await content_store.add_faq(str(self.actor), "Тестовый вопрос?", "Тестовый ответ.")
        state = await self._state_with_nav(faq_id=item["id"])
        await admin.faq_delete_do(make_callback("admindelfaqconfirm:yes", chat_id=self.actor), state)

        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertNotIn("faq_id", data)
        # удаление реально произошло — поведение бизнес-операции не изменилось
        remaining = await content_store.list_faq()
        self.assertFalse(any(i["id"] == item["id"] for i in remaining))

    # ---- 4b: редактирование (завершение через "Готово") ----
    async def test_finishing_about_edit_preserves_nav_anchor(self):
        state = await self._state_with_nav(field="tagline")
        await admin.about_edit_field(make_callback("admineditabout:done", chat_id=self.actor), state)

        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertNotIn("field", data)

    # ---- 5/6 (сценарий B): ensure_nav_anchor после сброса ничего не пересоздаёт ----
    async def test_ensure_nav_anchor_after_reset_does_not_recreate(self):
        state = await self._state_with_nav()
        await admin.menu_faq(make_callback("adminmenu:faq", chat_id=self.actor), state)  # один из 24 сайтов

        probe = make_flow_message_factory(chat_id=self.actor, start_id=3000)()
        created = await flow.ensure_nav_anchor(probe, state)

        self.assertFalse(created)  # anchor уже "существует" по мнению ensure_nav_anchor
        probe.answer.assert_not_awaited()  # ни одного нового WELCOME
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # id не изменился

    # ---- 8 (сценарий C): "Главное меню" после admin-действия не шлёт новый WELCOME ----
    async def test_main_menu_after_admin_action_sends_no_new_welcome(self):
        state = await self._state_with_nav()
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor), state)  # один из 24 сайтов

        mm_msg = make_flow_message_factory(chat_id=self.actor, start_id=4000)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(mm_msg, state)

        mm_msg.answer.assert_not_awaited()  # main_menu_cleanup, не confirmation и не WELCOME
        mm_msg.bot.edit_message_text.assert_not_awaited()  # NAV anchor не тронут ни одним сетевым вызовом
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)


class AdminSectionNavigationAnchorTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 1: верхнеуровневая admin-навигация (9 adminmenu:*
    entry points). flow.reset_state_keep_nav() стирает _flow_msg_id/
    _flow_chat_id (сохраняет только NAV), а следовавший за ним raw
    callback.message.edit_text() ничего не ставил взамен — экран
    физически корректен (тот же message_id), но TRANSIENT anchor на него
    больше не указывает. Это НЕ retry (Batch 2) и НЕ successor-staleness
    отдельного wizard'а (Batch 3) — это staleness самой section navigation,
    воспроизводимая при заходе в ЛЮБОЙ раздел админки. Все 9 handlers
    переведены на flow.step_from_callback, которая делает тот же
    edit_text и дополнительно фиксирует anchor."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "222"
        self.actor = 222

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_nav(self, nav_msg_id: int = 111) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
        })
        return state

    # ---- 1-4: все 9 entry points фиксируют текущий экран как anchor, NAV не тронут ----
    async def test_all_nine_sections_track_current_screen_and_preserve_nav(self):
        sections = [
            ("adminmenu:root", admin.menu_root),
            ("adminmenu:cases", admin.menu_cases),
            ("adminmenu:faq", admin.menu_faq),
            ("adminmenu:pricing", admin.menu_pricing),
            ("adminmenu:categories", admin.menu_categories),
            ("adminmenu:nav", admin.menu_nav),
            ("adminmenu:about", admin.menu_about),
            ("adminmenu:leads", admin.menu_leads),  # без заявок -> ветка "Заявок пока нет."
            ("adminmenu:backup", admin.menu_backup),
        ]
        for i, (data, handler) in enumerate(sections):
            with self.subTest(section=data):
                msg_id = 3000 + i
                state = await self._state_with_nav()
                cb = make_callback(data, chat_id=self.actor, message_id=msg_id)
                await handler(cb, state)

                cb.message.edit_text.assert_awaited_once()  # экран реально показан (edit, не send)
                result = await state.get_data()
                self.assertEqual(result.get(flow._ANCHOR_MSG_KEY), msg_id)  # anchor -> этот экран
                self.assertEqual(result.get(flow._ANCHOR_CHAT_KEY), self.actor)
                self.assertEqual(result.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # NAV не тронут
                self.assertEqual(result.get(flow._NAV_ANCHOR_CHAT_KEY), self.actor)

    async def test_menu_leads_with_active_leads_tracks_current_screen(self):
        # Второй call site внутри menu_leads (непустой список) — не
        # покрыт предыдущим тестом, у которого заявок нет.
        await content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Задача"},
            {"user_id": 66666, "username": "client", "first_name": "Клиент"},
        )
        state = await self._state_with_nav()
        msg_id = 3900
        cb = make_callback("adminmenu:leads", chat_id=self.actor, message_id=msg_id)
        await admin.menu_leads(cb, state)

        cb.message.edit_text.assert_awaited_once()
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- Переход между секциями: реалистично один физический message_id ----
    async def test_section_transition_keeps_same_message_id_and_nav(self):
        # Telegram callback-кнопки живут на конкретном сообщении — переход
        # между секциями в реальности редактирует ОДНО и то же сообщение
        # (пока не встретится текстовый/фото шаг мастера, вне scope этой
        # задачи), а не создаёт новое на каждый клик.
        state = await self._state_with_nav()
        msg_id = 4000
        await admin.menu_cases(make_callback("adminmenu:cases", chat_id=self.actor, message_id=msg_id), state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)

        await admin.menu_faq(make_callback("adminmenu:faq", chat_id=self.actor, message_id=msg_id), state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # anchor не уехал
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)  # NAV не создан заново

        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- Повторный вход A -> B -> A: без накопления orphan ----
    async def test_repeated_reentry_does_not_accumulate_orphans(self):
        state = await self._state_with_nav()
        msg_id = 4100
        cb_a1 = make_callback("adminmenu:cases", chat_id=self.actor, message_id=msg_id)
        cb_b = make_callback("adminmenu:faq", chat_id=self.actor, message_id=msg_id)
        cb_a2 = make_callback("adminmenu:cases", chat_id=self.actor, message_id=msg_id)

        await admin.menu_cases(cb_a1, state)
        await admin.menu_faq(cb_b, state)
        await admin.menu_cases(cb_a2, state)

        # Все три перехода — edit_text ОДНОГО и того же сообщения (RULE 3):
        # anchor остаётся тем же id через весь цикл, ни разу не "уехав" на
        # новое сообщение — именно так выглядело бы накопление orphan.
        cb_a1.message.edit_text.assert_awaited_once()
        cb_b.message.edit_text.assert_awaited_once()
        cb_a2.message.edit_text.assert_awaited_once()
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- Main Menu после admin-раздела: текущий экран удаляется, NAV сохраняется ----
    async def test_main_menu_after_admin_section_deletes_current_screen(self):
        state = await self._state_with_nav()
        msg_id = 4200
        await admin.menu_cases(make_callback("adminmenu:cases", chat_id=self.actor, message_id=msg_id), state)
        self.assertIsNone(await state.get_state())  # reset_state_keep_nav -> None

        trigger = make_flow_message_factory(chat_id=self.actor, start_id=4300)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)

        # До миграции anchor был бы absent -> ничего не удалялось бы, и
        # экран "Кейсы портфолио:" оставался бы orphan навсегда.
        trigger.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        trigger.answer.assert_not_awaited()  # нет нового WELCOME
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- /cancel из раздела, где он архитектурно доступен (menu_about -> active state) ----
    async def test_cancel_from_about_section_deletes_current_screen(self):
        state = await self._state_with_nav()
        msg_id = 4400
        await admin.menu_about(make_callback("adminmenu:about", chat_id=self.actor, message_id=msg_id), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_about_field_pick.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=4500)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)

        # До миграции cancel_transient не находил бы отслеживаемый anchor
        # (absent) и не удалял бы вообще ничего — текущий экран оставался
        # бы висеть рядом с новым "Отменено..." сообщением.
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- IMPORTANT ARCHITECTURAL CHECK: existing NAV -> section nav -> ensure_nav_anchor -> reused, no duplicate ----
    async def test_ensure_nav_anchor_after_section_visit_does_not_recreate(self):
        state = await self._state_with_nav()
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=4600), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=4700)()
        created = await flow.ensure_nav_anchor(probe, state)

        self.assertFalse(created)  # существующий NAV переиспользован
        probe.answer.assert_not_awaited()  # ни одного нового WELCOME/NAV
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertEqual(data.get(flow._NAV_ANCHOR_CHAT_KEY), self.actor)


class AdminFaqEditDeleteAnchorTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 2: FAQ edit/delete lifecycle. faq_edit_start/
    faq_edit_picked/faq_edit_field (non-"done") и faq_delete_start/
    faq_delete_confirm all edit the SAME physical message that menu_faq
    (Batch 1) already tracks — since editing never changes message_id and
    none of them touch state.data, the anchor stays accurate through the
    whole pick/field-select chain without any code change (verified below,
    not just assumed). The two REAL bugs, matching Batch 1's exact
    mechanism, were faq_edit_field's "done" branch and faq_delete_do:
    both called flow.reset_state_keep_nav() (wipes the anchor, keeps only
    NAV) followed by a raw edit_text that never restored it — fixed here
    via flow.step_from_callback, same primitive Batch 1 used.

    faq_edit_value has no retry/invalid-input branch at all (update_faq is
    called unconditionally on any text) — confirmed in the read-only audit,
    so there is no "invalid input" scenario to test for FAQ edit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "555"
        self.actor = 555

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_nav(self, nav_msg_id: int = 111) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
        })
        return state

    # ---- 1/2/6/11: edit entry -> pick -> field select -> /cancel, все на одном message_id ----
    async def test_faq_edit_navigation_chain_tracks_transient_and_cancel_removes_it(self):
        item = await content_store.add_faq(str(self.actor), "Старый вопрос?", "Старый ответ")
        state = await self._state_with_nav()
        msg_id = 6000

        # Реалистичный вход — сначала menu_faq (Batch 1), которая реально
        # закладывает anchor; без него faq_edit_start и не должен был бы
        # ничего найти (self-healing работает только при уже корректном
        # anchor на входе, см. аудит).
        await admin.menu_faq(make_callback("adminmenu:faq", chat_id=self.actor, message_id=msg_id), state)
        await admin.faq_edit_start(make_callback("adminfaqaction:edit", chat_id=self.actor, message_id=msg_id), state)
        data = await state.get_data()
        self.assertEqual(await state.get_state(), AdminStates.edit_faq_pick.state)

        await admin.faq_edit_picked(make_callback(f"admineditfaq:{item['id']}", chat_id=self.actor, message_id=msg_id), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_faq_field_pick.state)

        await admin.faq_edit_field(make_callback("admineditfaqfield:answer", chat_id=self.actor, message_id=msg_id), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_faq_value.state)

        # anchor всё это время указывает на то же физическое сообщение —
        # ни один из этих трёх raw edit_text-хендлеров не должен был его
        # менять, ему это и не нужно (тот же message_id, RULE 3).
        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=6100)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 4: успешное завершение поля -> корректный финальный экран (уже Batch 3, здесь сквозная проверка) ----
    async def test_faq_edit_value_success_then_done_reaches_faq_root_correctly(self):
        item = await content_store.add_faq(str(self.actor), "Вопрос", "Ответ")
        state = await self._state_with_nav()
        msg_id = 6200
        await admin.menu_faq(make_callback("adminmenu:faq", chat_id=self.actor, message_id=msg_id), state)
        await admin.faq_edit_start(make_callback("adminfaqaction:edit", chat_id=self.actor, message_id=msg_id), state)
        await admin.faq_edit_picked(make_callback(f"admineditfaq:{item['id']}", chat_id=self.actor, message_id=msg_id), state)
        await admin.faq_edit_field(make_callback("admineditfaqfield:answer", chat_id=self.actor, message_id=msg_id), state)

        value_msg = make_flow_message_factory(chat_id=self.actor, start_id=6300)(text="Новый ответ")
        await admin.faq_edit_value(value_msg, state)
        value_msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто ещё изменить?", chat_id=self.actor, message_id=msg_id, reply_markup=kb.faq_field_keyboard()
        )
        updated_items = await content_store.list_faq()
        self.assertEqual(next(i for i in updated_items if i["id"] == item["id"])["answer"], "Новый ответ")

        # 5/D/G/H: "done" -> Main Menu должно удалить именно этот, актуальный экран
        done_cb = make_callback("admineditfaqfield:done", chat_id=self.actor, message_id=msg_id)
        await admin.faq_edit_field(done_cb, state)
        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # anchor теперь на "FAQ:" root

        trigger = make_flow_message_factory(chat_id=self.actor, start_id=6400)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        trigger.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        trigger.answer.assert_not_awaited()  # ни одного нового WELCOME
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 5: faq_edit_field "done" сам по себе корректно фиксирует FAQ root anchor ----
    async def test_faq_edit_done_tracks_faq_root_as_current_transient(self):
        state = await self._state_with_nav()
        msg_id = 6500
        await state.update_data(field="answer", faq_id=1, cancel_to="faq")
        await state.set_state(AdminStates.edit_faq_field_pick)
        cb = make_callback("admineditfaqfield:done", chat_id=self.actor, message_id=msg_id)
        await admin.faq_edit_field(cb, state)

        cb.message.edit_text.assert_awaited_once_with("FAQ:", reply_markup=kb.admin_faq_menu_keyboard())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._ANCHOR_CHAT_KEY), self.actor)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertIsNone(await state.get_state())

    # ---- 7: delete confirmation отслеживает актуальный transient ----
    async def test_faq_delete_confirmation_tracks_transient(self):
        item = await content_store.add_faq(str(self.actor), "Удалить меня?", "Ответ")
        state = await self._state_with_nav()
        msg_id = 6600

        await admin.menu_faq(make_callback("adminmenu:faq", chat_id=self.actor, message_id=msg_id), state)
        await admin.faq_delete_start(make_callback("adminfaqaction:delete", chat_id=self.actor, message_id=msg_id), state)
        self.assertEqual(await state.get_state(), AdminStates.delete_faq_pick.state)

        cb = make_callback(f"admindelfaq:{item['id']}", chat_id=self.actor, message_id=msg_id)
        await admin.faq_delete_confirm(cb, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertEqual(await state.get_state(), AdminStates.delete_faq_confirm.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=6700)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)

    # ---- 8: delete cancellation ("Нет") сохраняет NAV и корректно фиксирует FAQ root anchor ----
    async def test_faq_delete_cancellation_preserves_nav_and_tracks_faq_root(self):
        item = await content_store.add_faq(str(self.actor), "Вопрос", "Ответ")
        state = await self._state_with_nav()
        msg_id = 6800
        await state.update_data(faq_id=item["id"], cancel_to="faq")
        await state.set_state(AdminStates.delete_faq_confirm)

        cb = make_callback("admindelfaqconfirm:no", chat_id=self.actor, message_id=msg_id)
        await admin.faq_delete_do(cb, state)

        cb.message.edit_text.assert_awaited_once_with("Отменено.", reply_markup=kb.admin_faq_menu_keyboard())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # FAQ root теперь корректно отслеживается
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertEqual(data.get(flow._NAV_ANCHOR_CHAT_KEY), self.actor)
        self.assertIsNone(await state.get_state())
        # вопрос не был удалён — "Нет" действительно отменяет, а не подтверждает
        remaining = await content_store.list_faq()
        self.assertTrue(any(i["id"] == item["id"] for i in remaining))

    # ---- 9: delete success возвращает на FAQ root, не оставляя confirmation orphan ----
    async def test_faq_delete_success_returns_to_faq_root_without_orphaning_confirmation(self):
        item = await content_store.add_faq(str(self.actor), "Вопрос", "Ответ")
        state = await self._state_with_nav()
        msg_id = 6900
        await state.update_data(faq_id=item["id"], cancel_to="faq")
        await state.set_state(AdminStates.delete_faq_confirm)

        cb = make_callback("admindelfaqconfirm:yes", chat_id=self.actor, message_id=msg_id)
        await admin.faq_delete_do(cb, state)

        cb.message.edit_text.assert_awaited_once_with("Вопрос удалён ✅", reply_markup=kb.admin_faq_menu_keyboard())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # тот же экран (confirmation), теперь "удалён ✅"
        remaining = await content_store.list_faq()
        self.assertFalse(any(i["id"] == item["id"] for i in remaining))

        # Main Menu после этого должно удалить именно этот, актуальный экран —
        # до фикса confirmation-сообщение осталось бы orphan навсегда.
        trigger = make_flow_message_factory(chat_id=self.actor, start_id=7000)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        trigger.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 10/14: отсутствующий FAQ item не ломает навигацию (content_store.delete_faq -> False, без исключения) ----
    async def test_faq_delete_missing_item_does_not_corrupt_navigation(self):
        state = await self._state_with_nav()
        msg_id = 7100
        await state.update_data(faq_id=999999, cancel_to="faq")  # id, которого нет
        await state.set_state(AdminStates.delete_faq_confirm)

        cb = make_callback("admindelfaqconfirm:yes", chat_id=self.actor, message_id=msg_id)
        await admin.faq_delete_do(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once_with("Вопрос удалён ✅", reply_markup=kb.admin_faq_menu_keyboard())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertIsNone(await state.get_state())  # FSM корректно сброшен, не завис

    # ---- 13: ensure_nav_anchor после edit-done и delete-do не дублирует WELCOME ----
    async def test_ensure_nav_anchor_after_faq_edit_done_does_not_recreate(self):
        state = await self._state_with_nav()
        await state.update_data(field="answer", faq_id=1, cancel_to="faq")
        await state.set_state(AdminStates.edit_faq_field_pick)
        await admin.faq_edit_field(make_callback("admineditfaqfield:done", chat_id=self.actor, message_id=7200), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=7300)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_ensure_nav_anchor_after_faq_delete_do_does_not_recreate(self):
        item = await content_store.add_faq(str(self.actor), "Вопрос", "Ответ")
        state = await self._state_with_nav()
        await state.update_data(faq_id=item["id"], cancel_to="faq")
        await state.set_state(AdminStates.delete_faq_confirm)
        await admin.faq_delete_do(make_callback("admindelfaqconfirm:yes", chat_id=self.actor, message_id=7400), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=7500)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)


class AdminPricingServiceAnchorTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 3: pricing/service/coefficient/option lifecycle. Every
    reset_state_keep_nav() call site in this block (price_add_includes,
    price_edit_field's "done", price_delete_do) was audited; only the
    latter two paired raw callback.message.edit_text() with it — same bug
    Batch 1/2 fixed elsewhere, now fixed via flow.step_from_callback. All
    other price_*/option_* callback handlers edit the SAME physical
    message menu_pricing (Batch 1) already tracks and never call
    reset_state_keep_nav/state.set_data — confirmed self-healing, left
    unchanged (verified below, not just assumed)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "666"
        self.actor = 666

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_nav(self, nav_msg_id: int = 111) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
        })
        return state

    # ---- 2/3: service add wizard — вся цепочка на одном message_id ----
    async def test_service_add_wizard_chain_tracks_one_transient_through_all_steps(self):
        state = await self._state_with_nav()
        msg_id = 8000
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_start(make_callback("adminpriceaction:add", chat_id=self.actor, message_id=msg_id), state)

        name_msg = make_flow_message_factory(chat_id=self.actor, start_id=8100)(text="Тестовая услуга")
        await admin.price_add_name(name_msg, state)
        name_msg.bot.edit_message_text.assert_awaited_once_with(
            "Базовая цена, ₽ (число):", chat_id=self.actor, message_id=msg_id, reply_markup=kb.cancel_keyboard()
        )

        price_msg = make_flow_message_factory(chat_id=self.actor, start_id=8200)(text="15000")
        await admin.price_add_price(price_msg, state)
        price_msg.bot.edit_message_text.assert_awaited_once_with(
            "Минимальный срок, дней (число):", chat_id=self.actor, message_id=msg_id, reply_markup=kb.cancel_keyboard()
        )

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # anchor не уехал за 2 шага
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 4: invalid input на шаге цены — retry редактирует тот же экран ----
    async def test_service_add_invalid_price_retry_keeps_correct_transient(self):
        state = await self._state_with_nav()
        msg_id = 8300
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_start(make_callback("adminpriceaction:add", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_name(make_flow_message_factory(chat_id=self.actor, start_id=8400)(text="Услуга"), state)

        bad_msg = make_flow_message_factory(chat_id=self.actor, start_id=8500)(text="не число")
        await admin.price_add_price(bad_msg, state)
        bad_msg.bot.edit_message_text.assert_awaited_once_with(
            "Нужно число, например 25000. Попробуйте ещё раз:", chat_id=self.actor, message_id=msg_id, reply_markup=kb.cancel_keyboard()
        )
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # ни одного orphan retry-сообщения

    # ---- 5: успешное завершение мастера -> корректный экран Услуги и цены ----
    async def test_service_add_success_returns_to_pricing_root(self):
        state = await self._state_with_nav()
        msg_id = 8600
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_start(make_callback("adminpriceaction:add", chat_id=self.actor, message_id=msg_id), state)
        make_msg = make_flow_message_factory(chat_id=self.actor, start_id=8700)
        await admin.price_add_name(make_msg(text="Полный тест"), state)
        await admin.price_add_price(make_msg(text="20000"), state)
        await admin.price_add_term_min(make_msg(text="3"), state)
        await admin.price_add_term_max(make_msg(text="10"), state)
        final_msg = make_msg(text="Дизайн под ключ")
        await admin.price_add_includes(final_msg, state)

        final_msg.bot.edit_message_text.assert_awaited_once_with(
            "Услуга «Полный тест» добавлена ✅", chat_id=self.actor, message_id=msg_id, reply_markup=kb.pricing_menu_keyboard()
        )
        self.assertIsNone(await state.get_state())
        services = await content_store.list_services()
        self.assertTrue(any(s["name"] == "Полный тест" for s in services))
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 6/7: мастер добавления услуги -> Главное меню и /cancel ----
    async def test_service_add_then_main_menu_shows_confirm_without_touching_transient(self):
        state = await self._state_with_nav()
        msg_id = 8800
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_start(make_callback("adminpriceaction:add", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_name(make_flow_message_factory(chat_id=self.actor, start_id=8900)(text="Услуга"), state)
        self.assertEqual(await state.get_state(), AdminStates.add_service_price.state)  # активный мастер, не None

        trigger = make_flow_message_factory(chat_id=self.actor, start_id=9000)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        # add_service_price — активный AdminStates, значит "Главное меню"
        # сначала спрашивает подтверждение (main_menu_confirm_keyboard), а
        # не удаляет сразу — тот же путь, что и у любого другого активного
        # мастера (см. bot/handlers/start.py::main_menu_or_confirm). Anchor
        # и transient-экран мастера при этом не тронуты — подтверждение
        # ещё не получено.
        trigger.answer.assert_awaited_once()
        trigger.bot.delete_message.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # anchor не пострадал
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_service_add_then_cancel_removes_current_prompt(self):
        state = await self._state_with_nav()
        msg_id = 9100
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_start(make_callback("adminpriceaction:add", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_add_name(make_flow_message_factory(chat_id=self.actor, start_id=9200)(text="Услуга"), state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=9300)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 8/11: service edit — pick/field-select self-healing, /cancel удаляет актуальный экран ----
    async def test_service_edit_chain_tracks_transient_and_cancel_removes_it(self):
        service = await content_store.add_service(
            str(self.actor), service_id="SVC_T1", name="Тест", base_price=10000, term_min=1, term_max=5, includes="—",
        )
        state = await self._state_with_nav()
        msg_id = 9400
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_edit_start(make_callback("adminpriceaction:edit", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_edit_picked(make_callback(f"admineditservice:{service['id']}", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_edit_field(make_callback("admineditservicefield:name", chat_id=self.actor, message_id=msg_id), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_service_value.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=9500)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 9/10: service edit success -> "done" -> Главное меню удаляет актуальный pricing-root экран ----
    async def test_service_edit_value_success_then_done_and_main_menu_cleans_it(self):
        service = await content_store.add_service(
            str(self.actor), service_id="SVC_T2", name="Тест2", base_price=10000, term_min=1, term_max=5, includes="—",
        )
        state = await self._state_with_nav()
        msg_id = 9600
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_edit_start(make_callback("adminpriceaction:edit", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_edit_picked(make_callback(f"admineditservice:{service['id']}", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_edit_field(make_callback("admineditservicefield:name", chat_id=self.actor, message_id=msg_id), state)

        value_msg = make_flow_message_factory(chat_id=self.actor, start_id=9700)(text="Новое имя")
        await admin.price_edit_value(value_msg, state)
        value_msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто изменить?", chat_id=self.actor, message_id=msg_id, reply_markup=kb.service_field_keyboard()
        )

        # До фикса reset_state_keep_nav в этой "done"-ветке стирал anchor,
        # и Главное меню не находило бы, что удалять.
        done_cb = make_callback("admineditservicefield:done", chat_id=self.actor, message_id=msg_id)
        await admin.price_edit_field(done_cb, state)
        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)

        trigger = make_flow_message_factory(chat_id=self.actor, start_id=9800)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        trigger.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        trigger.answer.assert_not_awaited()  # ни одного нового WELCOME
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 15: service delete confirmation отслеживает актуальный transient ----
    async def test_service_delete_confirmation_tracks_transient(self):
        service = await content_store.add_service(
            str(self.actor), service_id="SVC_T3", name="Тест3", base_price=5000, term_min=1, term_max=3, includes="—",
        )
        state = await self._state_with_nav()
        msg_id = 9900
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_delete_start(make_callback("adminpriceaction:delete", chat_id=self.actor, message_id=msg_id), state)
        cb = make_callback(f"admindelservice:{service['id']}", chat_id=self.actor, message_id=msg_id)
        await admin.price_delete_confirm(cb, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertEqual(await state.get_state(), AdminStates.delete_service_confirm.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=10000)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)

    # ---- 17: delete cancellation ("Нет") сохраняет NAV и фиксирует pricing-root anchor ----
    async def test_service_delete_cancellation_preserves_nav_and_tracks_pricing_root(self):
        state = await self._state_with_nav()
        msg_id = 10100
        await state.update_data(service_id="whatever", cancel_to="pricing")
        await state.set_state(AdminStates.delete_service_confirm)

        cb = make_callback("admindelserviceconfirm:no", chat_id=self.actor, message_id=msg_id)
        await admin.price_delete_do(cb, state)

        cb.message.edit_text.assert_awaited_once_with("Отменено.", reply_markup=kb.pricing_menu_keyboard())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertIsNone(await state.get_state())

    # ---- 16: delete success -> Главное меню не оставляет confirmation orphan ----
    async def test_service_delete_success_returns_to_pricing_root_without_orphaning_confirmation(self):
        service = await content_store.add_service(
            str(self.actor), service_id="SVC_T4", name="Тест4", base_price=8000, term_min=1, term_max=4, includes="—",
        )
        state = await self._state_with_nav()
        msg_id = 10200
        await state.update_data(service_id=service["id"], cancel_to="pricing")
        await state.set_state(AdminStates.delete_service_confirm)

        cb = make_callback("admindelserviceconfirm:yes", chat_id=self.actor, message_id=msg_id)
        await admin.price_delete_do(cb, state)

        cb.message.edit_text.assert_awaited_once_with("Услуга удалена ✅", reply_markup=kb.pricing_menu_keyboard())
        remaining = await content_store.list_services()
        self.assertFalse(any(s["id"] == service["id"] for s in remaining))

        trigger = make_flow_message_factory(chat_id=self.actor, start_id=10300)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        trigger.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        data_after = await state.get_data()
        self.assertEqual(data_after.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 18: отсутствующая услуга не ломает навигацию (delete_service -> False, без исключения) ----
    async def test_service_delete_missing_service_does_not_corrupt_navigation(self):
        state = await self._state_with_nav()
        msg_id = 10400
        await state.update_data(service_id="NOPE_NOT_REAL", cancel_to="pricing")
        await state.set_state(AdminStates.delete_service_confirm)

        cb = make_callback("admindelserviceconfirm:yes", chat_id=self.actor, message_id=msg_id)
        await admin.price_delete_do(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once_with("Услуга удалена ✅", reply_markup=kb.pricing_menu_keyboard())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertIsNone(await state.get_state())

    # ---- 12: coefficient edit — вся цепочка self-healing, /cancel удаляет актуальный экран ----
    async def test_coefficient_edit_chain_tracks_transient_and_cancel_removes_it(self):
        state = await self._state_with_nav()
        msg_id = 10500
        await admin.menu_pricing(make_callback("adminmenu:pricing", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_coef_start(make_callback("adminpriceaction:coef", chat_id=self.actor, message_id=msg_id), state)
        await admin.price_coef_pick(make_callback("admineditcoef:urgent", chat_id=self.actor, message_id=msg_id), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_coefficients_value.state)

        value_msg = make_flow_message_factory(chat_id=self.actor, start_id=10600)(text="1.3")
        await admin.price_coef_value(value_msg, state)
        value_msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто ещё изменить?", chat_id=self.actor, message_id=msg_id, reply_markup=kb.coefficients_menu_keyboard()
        )
        self.assertEqual(await state.get_state(), AdminStates.edit_coefficients_pick.state)

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=10700)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 13: option add — вся цепочка на одном message_id ----
    async def test_option_add_wizard_chain_tracks_one_transient_through_all_steps(self):
        service = await content_store.add_service(
            str(self.actor), service_id="SVC_T5", name="Тест5", base_price=6000, term_min=1, term_max=3, includes="—",
        )
        state = await self._state_with_nav()
        msg_id = 10800
        # anchor уже отслеживается (пришли сюда через price_edit_field's
        # "options" ветку, которая, как и option_action, self-healing —
        # см. аудит) — здесь сокращаем цепочку до сути, не отрицая её.
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor})
        await state.update_data(service_id=service["id"])
        await state.set_state(AdminStates.edit_service_field_pick)
        await admin.option_action(make_callback("adminoptaction:add", chat_id=self.actor, message_id=msg_id), state)

        make_msg = make_flow_message_factory(chat_id=self.actor, start_id=10900)
        await admin.option_add_name(make_msg(text="Доп. страница"), state)
        await admin.option_add_price(make_msg(text="3000"), state)
        await admin.option_add_days(make_msg(text="2"), state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # anchor не уехал за 3 шага

        done_cb = make_callback("adminoptmultipliable:yes", chat_id=self.actor, message_id=msg_id)
        await admin.option_add_multipliable(done_cb, state)
        options = await content_store.list_options(service["id"])
        self.assertTrue(any(o["name"] == "Доп. страница" for o in options))
        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick.state)

    # ---- 13 (продолжение): option edit — self-healing, /cancel удаляет актуальный экран ----
    async def test_option_edit_chain_tracks_transient_and_cancel_removes_it(self):
        service = await content_store.add_service(
            str(self.actor), service_id="SVC_T6", name="Тест6", base_price=6000, term_min=1, term_max=3, includes="—",
        )
        option_id = await content_store.next_option_id(service["id"])
        await content_store.add_option(str(self.actor), option_id=option_id, service_id=service["id"], name="Опция", price=1000, days=1, multipliable=False)
        state = await self._state_with_nav()
        msg_id = 11000
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor})
        await state.update_data(service_id=service["id"])
        await state.set_state(AdminStates.edit_service_field_pick)
        await admin.option_action(make_callback("adminoptaction:edit", chat_id=self.actor, message_id=msg_id), state)
        await admin.option_edit_picked(make_callback(f"admineditoption:{option_id}", chat_id=self.actor, message_id=msg_id), state)
        await admin.option_edit_field(make_callback("admineditoptionfield:price", chat_id=self.actor, message_id=msg_id), state)
        self.assertEqual(await state.get_state(), AdminStates.option_edit_value.state)

        value_msg = make_flow_message_factory(chat_id=self.actor, start_id=11100)(text="4000")
        await admin.option_edit_value_text(value_msg, state)
        value_msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто изменить?", chat_id=self.actor, message_id=msg_id, reply_markup=kb.option_field_keyboard()
        )

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=11200)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)

    # ---- 14: option delete — self-healing (option_delete_do не сбрасывает state.data) ----
    async def test_option_delete_chain_tracks_transient_and_cancel_removes_it(self):
        service = await content_store.add_service(
            str(self.actor), service_id="SVC_T7", name="Тест7", base_price=6000, term_min=1, term_max=3, includes="—",
        )
        option_id = await content_store.next_option_id(service["id"])
        await content_store.add_option(str(self.actor), option_id=option_id, service_id=service["id"], name="Опция", price=1000, days=1, multipliable=False)
        state = await self._state_with_nav()
        msg_id = 11300
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor})
        await state.update_data(service_id=service["id"], cancel_to="options")
        await state.set_state(AdminStates.edit_service_field_pick)
        await admin.option_action(make_callback("adminoptaction:delete", chat_id=self.actor, message_id=msg_id), state)
        cb = make_callback(f"admindeloption:{option_id}", chat_id=self.actor, message_id=msg_id)
        await admin.option_delete_confirm(cb, state)
        self.assertEqual(await state.get_state(), AdminStates.option_delete_confirm.state)

        do_cb = make_callback("admindeloptionconfirm:yes", chat_id=self.actor, message_id=msg_id)
        await admin.option_delete_do(do_cb, state)
        do_cb.message.edit_text.assert_awaited_once_with("Опция удалена ✅\n\nОпции этой услуги:", reply_markup=kb.options_menu_keyboard())
        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick.state)
        remaining = await content_store.list_options(service["id"])
        self.assertFalse(any(o["id"] == option_id for o in remaining))

        # option_delete_do не вызывает reset_state_keep_nav — anchor всё
        # ещё корректно указывает на то же физическое сообщение.
        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=11400)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)

    # ---- 20: ensure_nav_anchor после price_edit_field "done" и price_delete_do не дублирует WELCOME ----
    async def test_ensure_nav_anchor_after_price_edit_done_does_not_recreate(self):
        state = await self._state_with_nav()
        await state.update_data(field="name", service_id="whatever", cancel_to="pricing")
        await state.set_state(AdminStates.edit_service_field_pick)
        await admin.price_edit_field(make_callback("admineditservicefield:done", chat_id=self.actor, message_id=11500), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=11600)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_ensure_nav_anchor_after_price_delete_do_does_not_recreate(self):
        state = await self._state_with_nav()
        await state.update_data(service_id="whatever", cancel_to="pricing")
        await state.set_state(AdminStates.delete_service_confirm)
        await admin.price_delete_do(make_callback("admindelserviceconfirm:yes", chat_id=self.actor, message_id=11700), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=11800)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)


class AdminRemainingAnchorGapsTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 4: last 9 of the 24 reset_state_keep_nav() call sites in
    admin.py, spanning Cases (edit-done, delete), About (edit-done),
    Categories (add, rename, related-service, delete x2), Leads
    (reply-to-missing-lead) — same bug Batch 1-3 fixed elsewhere: reset
    wipes _flow_msg_id/_flow_chat_id, a raw edit_text()/answer() right
    after never restored it.

    Correction to the Batch 2 audit: these sites all leave state=None, so
    /cancel (which requires an active AdminStates value) is never
    reachable from them — but start.py::main_menu_or_confirm fires
    flow.main_menu_cleanup() *immediately* whenever state is None, no
    confirmation gate. So "Главное меню" — not /cancel — is the actually
    reachable path that exposed the orphan here, and every test below
    proves the fix through that path.

    With this batch, all 24 reset_state_keep_nav() call sites in admin.py
    are paired with a lifecycle primitive that restores the anchor —
    confirmed by a repo-wide sweep (see PART A of the delivery report),
    not just by this test list."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "777"
        self.actor = 777

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_nav(self, nav_msg_id: int = 111) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
        })
        return state

    async def _assert_main_menu_cleans_current_screen(self, state: FSMContext, msg_id: int, start_id: int) -> None:
        self.assertIsNone(await state.get_state())  # все 9 сайтов оставляют state=None
        trigger = make_flow_message_factory(chat_id=self.actor, start_id=start_id)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        trigger.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=msg_id)
        trigger.answer.assert_not_awaited()  # ни одного нового WELCOME
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- Cases: edit "done" ----
    async def test_cases_edit_done_then_main_menu_cleans_current_screen(self):
        state = await self._state_with_nav()
        msg_id = 12000
        await state.update_data(case_id="whatever")
        await state.set_state(AdminStates.edit_case_field_pick)
        cb = make_callback("admineditfield:done", chat_id=self.actor, message_id=msg_id)
        await admin.cases_edit_field(cb, state)
        cb.message.edit_text.assert_awaited_once_with("Кейсы портфолио:", reply_markup=kb.admin_cases_menu_keyboard())
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 12100)

    # ---- Cases: delete confirm/cancel ----
    async def test_cases_delete_do_then_main_menu_cleans_current_screen(self):
        case = await content_store.add_case(
            str(self.actor), case_id="case_del", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        state = await self._state_with_nav()
        msg_id = 12200
        await state.update_data(case_id=case["id"])
        await state.set_state(AdminStates.delete_case_confirm)
        cb = make_callback("admindelcaseconfirm:yes", chat_id=self.actor, message_id=msg_id)
        await admin.cases_delete_do(cb, state)
        cb.message.edit_text.assert_awaited_once_with("Кейс удалён ✅", reply_markup=kb.admin_cases_menu_keyboard())
        remaining = await content_store.list_cases()
        self.assertFalse(any(c["id"] == case["id"] for c in remaining))
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 12300)

    # ---- About: edit "done" ----
    async def test_about_edit_done_then_main_menu_cleans_current_screen(self):
        state = await self._state_with_nav()
        msg_id = 12400
        await state.set_state(AdminStates.edit_about_field_pick)
        cb = make_callback("admineditabout:done", chat_id=self.actor, message_id=msg_id)
        await admin.about_edit_field(cb, state)
        cb.message.edit_text.assert_awaited_once_with("Админ-меню:", reply_markup=kb.admin_root_keyboard())
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 12500)

    # ---- Categories: add ----
    async def test_cat_add_label_then_main_menu_cleans_current_screen(self):
        state = await self._state_with_nav()
        msg_id = 12600
        await state.set_state(AdminStates.add_category_label)
        msg = make_flow_message_factory(chat_id=self.actor, start_id=msg_id * 10)(text="Новая категория")
        # anchor уже отслеживается (пришли через cat_add_start, self-healing).
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor})
        await admin.cat_add_label(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Категория «Новая категория» добавлена ✅", chat_id=self.actor, message_id=msg_id, reply_markup=kb.categories_menu_keyboard()
        )
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 12700)

    # ---- Categories: rename ----
    async def test_cat_rename_value_then_main_menu_cleans_current_screen(self):
        cat = await content_store.add_portfolio_type(str(self.actor), type_id="cat_ren", label="Старое имя")
        state = await self._state_with_nav()
        msg_id = 12800
        await state.update_data(type_id=cat["id"], **{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor})
        await state.set_state(AdminStates.rename_category_value)
        msg = make_flow_message_factory(chat_id=self.actor, start_id=12900)(text="Новое имя")
        await admin.cat_rename_value(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Переименовано ✅", chat_id=self.actor, message_id=msg_id, reply_markup=kb.categories_menu_keyboard()
        )
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 13000)

    # ---- Categories: related-service set ----
    async def test_cat_relservice_set_then_main_menu_cleans_current_screen(self):
        cat = await content_store.add_portfolio_type(str(self.actor), type_id="cat_rel", label="Категория")
        state = await self._state_with_nav()
        msg_id = 13100
        await state.update_data(type_id=cat["id"])
        await state.set_state(AdminStates.category_related_service_pick)
        cb = make_callback("admincatrelservice:none", chat_id=self.actor, message_id=msg_id)
        await admin.cat_relservice_set(cb, state)
        cb.message.edit_text.assert_awaited_once_with("Обновлено ✅", reply_markup=kb.categories_menu_keyboard())
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 13200)

    # ---- Categories: delete blocked (in use) ----
    async def test_cat_delete_confirm_in_use_then_main_menu_cleans_current_screen(self):
        case = await content_store.add_case(
            str(self.actor), case_id="case_using_cat", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        state = await self._state_with_nav()
        msg_id = 13300
        await state.set_state(AdminStates.delete_category_pick)
        cb = make_callback("admindelcat:landing", chat_id=self.actor, message_id=msg_id)
        await admin.cat_delete_confirm(cb, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertIn("используется", cb.message.edit_text.await_args.args[0])
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 13400)
        await content_store.delete_case(str(self.actor), case["id"])  # cleanup, не влияет на тест

    # ---- Categories: delete confirm/cancel ----
    async def test_cat_delete_do_then_main_menu_cleans_current_screen(self):
        cat = await content_store.add_portfolio_type(str(self.actor), type_id="cat_del", label="Удалить меня")
        state = await self._state_with_nav()
        msg_id = 13500
        await state.update_data(type_id=cat["id"])
        await state.set_state(AdminStates.delete_category_confirm)
        cb = make_callback("admindelcatconfirm:yes", chat_id=self.actor, message_id=msg_id)
        await admin.cat_delete_do(cb, state)
        cb.message.edit_text.assert_awaited_once_with("Категория удалена ✅", reply_markup=kb.categories_menu_keyboard())
        remaining = await content_store.list_portfolio_types()
        self.assertFalse(any(t["id"] == cat["id"] for t in remaining))
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 13600)

    # ---- Leads: reply to a lead that no longer exists ----
    async def test_lead_reply_send_missing_lead_then_main_menu_cleans_current_screen(self):
        state = await self._state_with_nav()
        msg_id = 13700
        await state.update_data(lead_id=999999, **{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor})
        await state.set_state(AdminStates.lead_reply_text)
        msg = make_flow_message_factory(chat_id=self.actor, start_id=13800)(text="Ответ")
        await admin.lead_reply_send(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Заявка не найдена.", chat_id=self.actor, message_id=msg_id, reply_markup=kb.admin_root_keyboard()
        )
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 13900)

    # ---- ensure_nav_anchor: представительная выборка (callback- и text-триггерные) ----
    async def test_ensure_nav_anchor_after_cases_edit_done_does_not_recreate(self):
        state = await self._state_with_nav()
        await state.update_data(case_id="whatever")
        await state.set_state(AdminStates.edit_case_field_pick)
        await admin.cases_edit_field(make_callback("admineditfield:done", chat_id=self.actor, message_id=14000), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=14100)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_ensure_nav_anchor_after_cat_add_label_does_not_recreate(self):
        state = await self._state_with_nav()
        await state.update_data(**{flow._ANCHOR_MSG_KEY: 14200, flow._ANCHOR_CHAT_KEY: self.actor})
        await state.set_state(AdminStates.add_category_label)
        await admin.cat_add_label(make_flow_message_factory(chat_id=self.actor, start_id=14300)(text="Категория"), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=14400)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- Additional finding discovered while implementing this batch: ----
    # step_from_text's successful-edit path doesn't touch state.data (the
    # anchor is already correct and unchanged), so a reset_state_keep_nav()
    # immediately after it still wiped _flow_msg_id anyway — a real,
    # already-shipped gap in cases_add_description (Batch 1) and
    # price_add_includes (Batch 3), empirically confirmed and fixed in
    # this batch by swapping reset_state_keep_nav for flow.finish_flow
    # (same primitive faq_add_answer already used). cases_add_description
    # is covered by the updated
    # AdminNavAnchorResetTests.test_completing_add_case_wizard_preserves_nav_anchor;
    # price_add_includes did not have an equivalent pre-existing test, so
    # it is covered fresh here.
    async def test_price_add_includes_then_main_menu_cleans_current_screen(self):
        state = await self._state_with_nav()
        msg_id = 14500
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor})
        await state.update_data(name="Услуга", base_price=10000, term_min=1, term_max=5)
        await state.set_state(AdminStates.add_service_includes)
        msg = make_flow_message_factory(chat_id=self.actor, start_id=14600)(text="Дизайн под ключ")
        await admin.price_add_includes(msg, state)
        msg.bot.edit_message_text.assert_awaited_once_with(
            "Услуга «Услуга» добавлена ✅", chat_id=self.actor, message_id=msg_id, reply_markup=kb.pricing_menu_keyboard()
        )
        services = await content_store.list_services()
        self.assertTrue(any(s["name"] == "Услуга" for s in services))
        await self._assert_main_menu_cleans_current_screen(state, msg_id, 14700)


class AdminLeadsFullSequenceTests(unittest.IsolatedAsyncioTestCase):
    """TEST D из ТЗ: /admin -> Заявки -> открыть -> изменить статус ->
    вернуться, одной непрерывной последовательностью реальных хендлеров
    (не по отдельности), с проверкой persistence на каждом шаге."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "888"
        self.actor = 888
        self.lead = await content_store.add_lead(
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
        # Открытие карточки NEW-заявки автоматически переводит её в VIEWED
        # (см. UX-аудит "Заявки как рабочая очередь") — раньше здесь
        # оставался NEW, статус менялся только явным кликом по кнопке.
        self.assertEqual((await content_store.get_lead(self.lead["id"]))["status"], "VIEWED")

        await admin.lead_change_status(make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor), state)
        self.assertEqual((await content_store.get_lead(self.lead["id"]))["status"], "IN_PROGRESS")  # реально сохранилось

        await admin.lead_back_to_list(make_callback("adminleadaction:back", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)

        # Статус пережил весь проход, не только момент смены
        self.assertEqual((await content_store.get_lead(self.lead["id"]))["status"], "IN_PROGRESS")


class AdminLeadsQueueUxTests(unittest.IsolatedAsyncioTestCase):
    """UX-аудит "Заявки как рабочая очередь" (продуктовый блок, пункты 1-4):
    дефолтный фильтр /admin -> Заявки, статус+дата в списке, авто NEW ->
    VIEWED при открытии карточки — без уведомления клиента/owner_message."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "777"
        self.actor = 777
        self.telegram = {"user_id": 55555, "username": "client", "first_name": "Клиент"}

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _button_texts(self, markup) -> list[str]:
        return [btn.text for row in markup.inline_keyboard for btn in row]

    async def test_default_filter_excludes_done_and_cancelled(self):
        active_lead = await content_store.add_lead({"service_name": "Активная"}, self.telegram)
        done_lead = await content_store.add_lead({"service_name": "Завершённая"}, self.telegram)
        await content_store.update_lead_status(self.actor, done_lead["id"], "DONE")
        cancelled_lead = await content_store.add_lead({"service_name": "Отменённая"}, self.telegram)
        await content_store.update_lead_status(self.actor, cancelled_lead["id"], "CANCELLED")

        state = make_state(self.actor)
        cb = make_callback("adminmenu:leads", chat_id=self.actor)
        await admin.menu_leads(cb, state)

        self.assertEqual((await state.get_data())["lead_filter"], "ACTIVE")
        texts = " ".join(self._button_texts(cb.message.edit_text.await_args.kwargs["reply_markup"]))
        self.assertIn(f"#{active_lead['id']}", texts)
        self.assertNotIn(f"#{done_lead['id']}", texts)
        self.assertNotIn(f"#{cancelled_lead['id']}", texts)

    async def test_explicit_filter_still_works(self):
        await content_store.add_lead({"service_name": "Новая"}, self.telegram)
        done_lead = await content_store.add_lead({"service_name": "Завершённая"}, self.telegram)
        await content_store.update_lead_status(self.actor, done_lead["id"], "DONE")

        state = make_state(self.actor)
        cb = make_callback("adminleadfilter:DONE", chat_id=self.actor)
        await admin.leads_filter_apply(cb, state)

        self.assertEqual((await state.get_data())["lead_filter"], "DONE")
        texts = " ".join(self._button_texts(cb.message.edit_text.await_args.kwargs["reply_markup"]))
        self.assertIn(f"#{done_lead['id']}", texts)

    async def test_list_keyboard_shows_status_and_updated_at(self):
        lead = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "WAITING_CLIENT")
        lead = await content_store.get_lead(lead["id"])

        markup = kb.leads_list_keyboard([lead], "ACTIVE")
        text = markup.inline_keyboard[0][0].text

        self.assertIn(f"#{lead['id']}", text)
        self.assertIn("⏸", text)  # эмодзи WAITING_CLIENT из LEAD_STATUS_LABELS, тот же набор
        self.assertIn(lead["updated_at"][5:16].replace("T", " "), text)  # "MM-DD HH:MM"

    async def test_new_lead_opened_once_becomes_viewed_and_updates_timestamp(self):
        lead = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        self.assertIsNone(lead["updated_at"])

        state = make_state(self.actor)
        await admin.lead_open_detail(make_callback(f"adminleadpick:{lead['id']}", chat_id=self.actor), state)

        updated = await content_store.get_lead(lead["id"])
        self.assertEqual(updated["status"], "VIEWED")
        self.assertIsNotNone(updated["updated_at"])  # реально обновилось — заявка "просмотрена по-настоящему"

    async def test_already_viewed_lead_opened_again_no_extra_update(self):
        lead = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "VIEWED")
        before = (await content_store.get_lead(lead["id"]))["updated_at"]

        state = make_state(self.actor)
        await admin.lead_open_detail(make_callback(f"adminleadpick:{lead['id']}", chat_id=self.actor), state)

        after = await content_store.get_lead(lead["id"])
        self.assertEqual(after["status"], "VIEWED")
        self.assertEqual(after["updated_at"], before)  # ни одной лишней записи

    async def test_non_new_status_untouched_on_open(self):
        lead = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "DONE")
        before = (await content_store.get_lead(lead["id"]))["updated_at"]

        state = make_state(self.actor)
        await admin.lead_open_detail(make_callback(f"adminleadpick:{lead['id']}", chat_id=self.actor), state)

        after = await content_store.get_lead(lead["id"])
        self.assertEqual(after["status"], "DONE")
        self.assertEqual(after["updated_at"], before)

    async def test_auto_viewed_does_not_send_client_notification(self):
        lead = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        state = make_state(self.actor)
        cb = make_callback(f"adminleadpick:{lead['id']}", chat_id=self.actor)

        await admin.lead_open_detail(cb, state)

        cb.bot.send_message.assert_not_awaited()  # смена статуса кликом уведомляет, авто-VIEWED — нет

    async def test_auto_viewed_does_not_create_owner_message(self):
        lead = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        state = make_state(self.actor)

        await admin.lead_open_detail(make_callback(f"adminleadpick:{lead['id']}", chat_id=self.actor), state)

        updated = await content_store.get_lead(lead["id"])
        self.assertEqual(updated.get("owner_messages", []), [])


class MainMenuConfirmationTests(unittest.IsolatedAsyncioTestCase):
    """"⌂ Главное меню" (см. production-аудит про дублирование NAV anchor):
    нет активного bot/FSM-состояния -> только cleanup (flow.main_menu_cleanup
    -- НЕ /start, WELCOME/NAV anchor не трогает); есть -> inline-подтверждение
    без отдельного долгоживущего state, "Да" тоже ведёт в main_menu_cleanup,
    не в cmd_start. Отдельно — что owner-версия (bot/handlers/admin.py::
    admin_main_menu_button) реально побеждает раньше AdminStates.*, F.text
    мастеров."""

    def setUp(self):
        self._orig_designer = start.config.DESIGNER_CHAT_ID
        start.config.DESIGNER_CHAT_ID = "888"

    def tearDown(self):
        start.config.DESIGNER_CHAT_ID = self._orig_designer

    def _fake_callback(self, data: str, chat_id: int = 888):
        return SimpleNamespace(data=data, message=make_flow_message(chat_id=chat_id), answer=AsyncMock())

    async def test_no_active_state_cleans_up_without_touching_nav_anchor(self):
        # A. Главное меню без active state -- ТОЛЬКО cleanup: WELCOME не
        # создаётся, NAV anchor не редактируется, TRANSIENT и trigger
        # удаляются, FSM сброшен.
        state = make_state(999)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 999,
            flow._ANCHOR_MSG_KEY: 222, flow._ANCHOR_CHAT_KEY: 999,
        })
        msg = make_flow_message(chat_id=999, text=texts.MAIN_MENU_BUTTON)
        deleted = {"trigger": False}

        async def _tracked_delete():
            deleted["trigger"] = True

        msg.delete = _tracked_delete

        await start.main_menu_button(msg, state)

        msg.answer.assert_not_awaited()
        msg.bot.edit_message_text.assert_not_awaited()
        msg.bot.delete_message.assert_awaited_once_with(chat_id=999, message_id=222)
        self.assertTrue(deleted["trigger"])
        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertIsNone(data.get(flow._ANCHOR_MSG_KEY))
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_active_state_shows_confirmation_without_resetting_it(self):
        from aiogram.types import InlineKeyboardMarkup

        state = make_state(888)
        await state.set_state(AdminStates.add_faq_answer)
        msg = make_flow_message(chat_id=888, text=texts.MAIN_MENU_BUTTON)
        await start.main_menu_button(msg, state)
        sent_text = msg.answer.await_args_list[0].args[0] if msg.answer.await_args_list[0].args else msg.answer.await_args_list[0].kwargs.get("text")
        sent_markup = msg.answer.await_args_list[0].kwargs.get("reply_markup") or msg.answer.await_args_list[0].args[1]
        self.assertEqual(sent_text, texts.MAIN_MENU_CONFIRM_TEXT)
        self.assertIsInstance(sent_markup, InlineKeyboardMarkup)
        self.assertEqual(await state.get_state(), AdminStates.add_faq_answer.state)  # ничего не сброшено самим показом

    async def test_confirm_calls_main_menu_cleanup_without_welcome(self):
        # B. "Да" -> flow.main_menu_cleanup: сценарий сброшен, WELCOME НЕ
        # создан, существующий NAV anchor не тронут.
        state = make_state(888)
        await state.set_state(AdminStates.add_faq_answer)
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 888})
        cb = self._fake_callback("mainmenu:confirm")
        await start.main_menu_confirm(cb, state)
        self.assertIsNone(await state.get_state())
        cb.message.answer.assert_not_awaited()  # WELCOME не создан
        cb.message.bot.edit_message_text.assert_not_awaited()  # NAV anchor не тронут
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_repeated_main_menu_presses_create_zero_new_nav_messages(self):
        # C. Главное меню -> Главное меню -> Главное меню: ни одного нового
        # NAV/WELCOME сообщения, message_id NAV anchor не меняется.
        state = make_state(999)
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: 999})
        total_answer = 0
        total_edit = 0
        for _ in range(3):
            msg = make_flow_message(chat_id=999, text=texts.MAIN_MENU_BUTTON)
            await start.main_menu_button(msg, state)
            total_answer += msg.answer.await_count
            total_edit += msg.bot.edit_message_text.await_count
        self.assertEqual(total_answer, 0)
        self.assertEqual(total_edit, 0)
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_main_menu_paths_never_call_cmd_start(self):
        # Critical regression: ни main_menu_button (client), ни
        # admin_main_menu_button (owner), ни confirm-путь НЕ должны
        # вызывать start.cmd_start ни при каких обстоятельствах —
        # единственный вызывающий cmd_start во всём проекте — сам
        # /start-роутер (см. static grep в аудите).
        original_cmd_start = start.cmd_start
        spy = AsyncMock(side_effect=original_cmd_start)
        start.cmd_start = spy
        try:
            # client, без активного state
            state1 = make_state(999)
            msg1 = make_flow_message(chat_id=999, text=texts.MAIN_MENU_BUTTON)
            await start.main_menu_button(msg1, state1)

            # owner, без активного state
            state2 = make_state(888)
            msg2 = make_flow_message(chat_id=888, text=texts.MAIN_MENU_BUTTON)
            await admin.admin_main_menu_button(msg2, state2)

            # confirm-путь при активном state
            state3 = make_state(888)
            await state3.set_state(AdminStates.add_faq_answer)
            cb = self._fake_callback("mainmenu:confirm")
            await start.main_menu_confirm(cb, state3)

            spy.assert_not_awaited()
        finally:
            start.cmd_start = original_cmd_start

    async def test_decline_preserves_state_and_sends_nothing_new(self):
        state = make_state(888)
        await state.set_state(AdminStates.add_faq_answer)
        cb = self._fake_callback("mainmenu:decline")
        await start.main_menu_decline(cb, state)
        self.assertEqual(await state.get_state(), AdminStates.add_faq_answer.state)  # ничего не сброшено
        cb.message.answer.assert_not_awaited()  # ничего не отправлено — только confirmation убрана

    async def test_admin_main_menu_button_wins_over_wizard_text_handler(self):
        # Критично: без регистрации РАНЬШЕ мастеров текст кнопки был бы
        # проглочен как введённые данные (напр. ответ FAQ) вместо срабатывания
        # как аварийный выход (см. bot/handlers/admin.py::admin_main_menu_button).
        handler_names = [h.callback.__name__ for h in admin.router.message.handlers]
        self.assertIn("admin_main_menu_button", handler_names)
        self.assertIn("faq_add_answer", handler_names)
        self.assertLess(
            handler_names.index("admin_main_menu_button"),
            handler_names.index("faq_add_answer"),
        )

    async def test_owner_main_menu_button_handled_by_admin_router_not_client(self):
        # Владелец мидвэй в мастере — "⌂ Главное меню" должен показать
        # confirmation через admin.admin_main_menu_button (тот же общий
        # helper, что и у клиента, но зарегистрированный в admin.router).
        state = make_state(888)
        await state.set_state(AdminStates.add_faq_answer)
        msg = make_flow_message(chat_id=888, text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(msg, state)
        self.assertEqual(await state.get_state(), AdminStates.add_faq_answer.state)
        msg.answer.assert_awaited()


class FaqCleanupRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Regression после 88acc40: FAQ-список никогда не регистрировался как
    flow.py anchor (см. UX-аудит) — "⌂ Главное меню"/"/start"/выход в admin
    после FAQ не могли найти его, чтобы удалить, старый список оставался в
    чате. Разрыв существовал с первого коммита faq.py, просто не был
    заметен без persistent-кнопки, чья явная задача — вернуть в чистый
    корень. Фикс: _send_faq_list теперь идёт через flow.open_root, как
    /portfolio, /about, /brief, /admin уже делают."""

    def setUp(self):
        self._orig_designer = admin.config.DESIGNER_CHAT_ID
        admin.config.DESIGNER_CHAT_ID = "888"

    def tearDown(self):
        admin.config.DESIGNER_CHAT_ID = self._orig_designer

    async def test_faq_then_main_menu_cleans_up_old_faq_message(self):
        state = make_state(888)
        faq_msg = make_flow_message(chat_id=888, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg, state)

        menu_msg = make_flow_message(chat_id=888, text=texts.MAIN_MENU_BUTTON)
        await start.main_menu_button(menu_msg, state)

        # Ровно один delete — именно старого FAQ-сообщения, ничего больше
        # (в частности, ни одного другого/пользовательского сообщения).
        menu_msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=555)

    async def test_faq_then_main_menu_leaves_exactly_one_current_root_message(self):
        # E. NAV anchor уже существует после FAQ -> Главное меню ТОЛЬКО
        # чистит TRANSIENT (FAQ), NAV anchor не трогает вообще (см.
        # bot/flow.py::main_menu_cleanup — main_menu_or_confirm больше не
        # проходит ни через reset_nav_screen, ни через cmd_start для этого
        # случая).
        state = make_state(888)
        faq_msg = make_flow_message(chat_id=888, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg, state)
        data = await state.get_data()
        nav_id_after_faq = data.get(flow._NAV_ANCHOR_MSG_KEY)
        self.assertIsNotNone(nav_id_after_faq)

        menu_msg = make_flow_message(chat_id=888, text=texts.MAIN_MENU_BUTTON)
        await start.main_menu_button(menu_msg, state)

        menu_msg.answer.assert_not_awaited()  # WELCOME не создан
        menu_msg.bot.edit_message_text.assert_not_awaited()  # NAV anchor не тронут
        data = await state.get_data()
        self.assertIsNone(data.get(flow._ANCHOR_MSG_KEY))  # старый FAQ TRANSIENT-anchor очищен
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), nav_id_after_faq)  # NAV anchor тот же самый

    async def test_admin_then_main_menu_cleans_up_admin_screen_without_touching_nav(self):
        # E. Owner: Admin -> Главное меню -- transient (admin-экран)
        # удаляется, NAV anchor не трогается, WELCOME не создаётся.
        state = make_state(888)
        admin_msg = make_flow_message(chat_id=888, text="/admin")
        await admin.cmd_admin(admin_msg, state)
        data = await state.get_data()
        nav_id_after_admin = data.get(flow._NAV_ANCHOR_MSG_KEY)
        transient_id_after_admin = data.get(flow._ANCHOR_MSG_KEY)
        self.assertIsNotNone(nav_id_after_admin)
        self.assertIsNotNone(transient_id_after_admin)

        menu_msg = make_flow_message(chat_id=888, text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(menu_msg, state)

        menu_msg.answer.assert_not_awaited()
        menu_msg.bot.edit_message_text.assert_not_awaited()
        menu_msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=transient_id_after_admin)
        data = await state.get_data()
        self.assertIsNone(data.get(flow._ANCHOR_MSG_KEY))
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), nav_id_after_admin)

    async def test_faq_then_start_cleans_up_old_faq_message(self):
        # /start не менялся в этом фиксе — тот же open_root, что и раньше,
        # просто теперь находит реальный anchor, а не пустоту.
        state = make_state(888)
        faq_msg = make_flow_message(chat_id=888, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg, state)

        start_msg = make_flow_message(chat_id=888, text="/start")
        await start.cmd_start(start_msg, state)

        start_msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=555)

    async def test_faq_then_admin_cleans_up_old_faq_message(self):
        # NAV anchor уже существует после FAQ -> /admin его не трогает
        # вообще (ни delete, ни edit, ни новое сообщение) — единственный
        # delete_message здесь — RULE 2 (старый FAQ TRANSIENT-anchor).
        state = make_state(888)
        faq_msg = make_flow_message(chat_id=888, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg, state)
        data = await state.get_data()
        nav_id_after_faq = data.get(flow._NAV_ANCHOR_MSG_KEY)

        admin_msg = make_flow_message(chat_id=888, text="/admin")
        await admin.cmd_admin(admin_msg, state)

        admin_msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=555)
        admin_msg.answer.assert_awaited_once()  # только admin-контент, NAV anchor не пересоздан
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), nav_id_after_faq)

    async def test_faq_then_cancel_cleans_up_old_faq_message(self):
        # Клиентский /cancel тоже идёт через open_root — тот же принцип.
        state = make_state(888)
        faq_msg = make_flow_message(chat_id=888, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg, state)

        cancel_msg = make_flow_message(chat_id=888, text="/cancel")
        await start.cmd_cancel(cancel_msg, state)

        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=888, message_id=555)

    async def test_faq_list_still_refreshes_persistent_keyboard(self):
        # Свежий state -> открытие FAQ само создаёт NAV anchor ПЕРВЫМ
        # answer()-вызовом (см. bot/flow.py::open_flow -> ensure_nav_anchor),
        # ВТОРЫМ — сам FAQ-список.
        from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

        state = make_state()
        msg = make_flow_message(text=texts.MENU_FAQ)
        await faq.show_faq_list(msg, state)
        self.assertEqual(msg.answer.await_count, 2)
        nav_call, content_call = msg.answer.await_args_list
        nav_markup = nav_call.kwargs.get("reply_markup") or nav_call.args[1]
        self.assertIsInstance(nav_markup, ReplyKeyboardMarkup)
        self.assertTrue(nav_markup.is_persistent)
        sent_markup = content_call.kwargs.get("reply_markup") or content_call.args[1]
        self.assertIsInstance(sent_markup, InlineKeyboardMarkup)

    async def test_admin_transitions_do_not_break_when_no_faq_anchor_exists(self):
        # Regression-safety: если пользователь НЕ был на FAQ (TRANSIENT
        # anchor не установлен), /admin по-прежнему должен отрабатывать без
        # ошибок. На свежем state NAV anchor тоже ещё не существует — его
        # создание НЕ вызывает delete_message вообще (это чистая отправка,
        # см. bot/flow.py::ensure_nav_anchor), а удалять несуществующий
        # TRANSIENT-anchor нечего — delete_message не вызывается вовсе.
        state = make_state(888)
        admin_msg = make_flow_message(chat_id=888, text="/admin")
        await admin.cmd_admin(admin_msg, state)
        admin_msg.bot.delete_message.assert_not_awaited()
        admin_msg.answer.assert_awaited()

    async def test_faq_trigger_deletion_targets_only_the_trigger_message_itself(self):
        # RULE 1 (см. bot/flow.py) — то же самое, уже давно происходящее для
        # /portfolio, /about, /brief, /cancel, /start: удаляется СВОЁ же
        # сообщение-триггер (тап по кнопке/команда), не произвольный чужой
        # контент. delete() здесь — метод самого triggering message, не
        # bot.delete_message с посторонним message_id.
        state = make_state()
        msg = make_flow_message(text=texts.MENU_FAQ)
        deleted = {"called": False}

        async def _tracked_delete():
            deleted["called"] = True

        msg.delete = _tracked_delete
        await faq.show_faq_list(msg, state)
        self.assertTrue(deleted["called"])  # именно сам триггер, best-effort, как и везде

    async def test_client_faq_reply_keyboard_content_has_main_menu_and_faq(self):
        # Второй production regression (после cleanup-фикса): persistent
        # reply-клавиатура исчезала после FAQ. Проверяем не только ТИП
        # (ReplyKeyboardMarkup), а конкретное СОДЕРЖИМОЕ, которое реально
        # должно остаться доступным клиенту.
        state = make_state(999)  # НЕ owner chat_id (888 занят DESIGNER_CHAT_ID в setUp)
        msg = make_flow_message(chat_id=999, text=texts.MENU_FAQ)
        await faq.show_faq_list(msg, state)
        nav_call = msg.answer.await_args_list[0]
        nav_markup = nav_call.kwargs.get("reply_markup") or nav_call.args[1]
        button_texts = {btn.text for row in nav_markup.keyboard for btn in row}
        self.assertEqual(button_texts, {texts.MAIN_MENU_BUTTON, texts.MENU_FAQ})
        self.assertNotIn(texts.ADMIN_BUTTON, button_texts)

    async def test_owner_faq_reply_keyboard_content_has_admin_too(self):
        state = make_state(888)  # 888 == DESIGNER_CHAT_ID (см. setUp)
        msg = make_flow_message(chat_id=888, text=texts.MENU_FAQ)
        await faq.show_faq_list(msg, state)
        nav_call = msg.answer.await_args_list[0]
        nav_markup = nav_call.kwargs.get("reply_markup") or nav_call.args[1]
        button_texts = {btn.text for row in nav_markup.keyboard for btn in row}
        self.assertEqual(button_texts, {texts.MAIN_MENU_BUTTON, texts.MENU_FAQ, texts.ADMIN_BUTTON})

    async def test_faq_inline_controls_still_work_after_the_fix(self):
        # faq_back/faq_answer/faq_price_answer не менялись (edit_text того
        # же anchor-сообщения) — явная regression-проверка, что это
        # по-прежнему так после переноса _send_faq_list на open_root.
        cb = make_callback("faq:back", chat_id=999)
        await faq.faq_back(cb)
        cb.message.edit_text.assert_awaited_once()
        args, kwargs = cb.message.edit_text.call_args
        self.assertEqual(args[0] if args else kwargs.get("text"), texts.FAQ_INTRO)

    async def test_start_reply_keyboard_content_unchanged(self):
        # /start на свежем state создаёт NAV anchor единственным answer() —
        # не должно внезапно приобрести лишний вызов или потерять кнопки
        # из-за разделения NAV/TRANSIENT anchor'ов в open_flow.
        state = make_state(999)
        msg = make_flow_message(chat_id=999, text="/start")
        await start.cmd_start(msg, state)
        self.assertEqual(msg.answer.await_count, 1)
        sent_markup = msg.answer.await_args.kwargs.get("reply_markup") or msg.answer.await_args.args[1]
        button_texts = {btn.text for row in sent_markup.keyboard for btn in row}
        self.assertEqual(button_texts, {texts.MAIN_MENU_BUTTON, texts.MENU_FAQ})

    async def test_cold_state_faq_creates_nav_anchor_separately_from_transient(self):
        # Cold state (никогда не было /start) -> прямой FAQ обязан сам
        # создать NAV anchor (WELCOME + persistent-клавиатура) ПЕРЕД
        # собственным TRANSIENT-контентом, а не оставить клиента без
        # клавиатуры вовсе (см. bot/flow.py::ensure_nav_anchor).
        state = make_state(999)
        msg = make_flow_message(chat_id=999, text=texts.MENU_FAQ)

        await faq.show_faq_list(msg, state)

        self.assertEqual(msg.answer.await_count, 2)
        nav_call, content_call = msg.answer.await_args_list
        nav_text = nav_call.args[0] if nav_call.args else nav_call.kwargs.get("text")
        self.assertEqual(nav_text, texts.WELCOME)
        data = await state.get_data()
        self.assertIsNotNone(data.get(flow._NAV_ANCHOR_MSG_KEY))
        self.assertIsNotNone(data.get(flow._ANCHOR_MSG_KEY))  # TRANSIENT (FAQ) тоже отслеживается — отдельно

    async def test_main_menu_faq_main_menu_faq_keeps_same_nav_anchor_id(self):
        # Главное меню -> FAQ -> Главное меню -> FAQ: один и тот же NAV
        # anchor message_id на всём протяжении, TRANSIENT anchor меняется.
        state = make_state(999)
        faq_msg1 = make_flow_message(chat_id=999, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg1, state)
        data = await state.get_data()
        nav_id = data.get(flow._NAV_ANCHOR_MSG_KEY)
        self.assertIsNotNone(nav_id)

        menu_msg = make_flow_message(chat_id=999, text=texts.MAIN_MENU_BUTTON)
        await start.main_menu_button(menu_msg, state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), nav_id)

        faq_msg2 = make_flow_message(chat_id=999, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg2, state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), nav_id)  # тот же NAV anchor всё время

    async def test_cancel_transient_message_carries_no_keyboard_and_keeps_nav_anchor(self):
        # FAQ, затем /cancel — TRANSIENT informational-сообщение НЕ несёт
        # reply-клавиатуру (её обеспечивает NAV anchor), а сам NAV anchor,
        # созданный на шаге FAQ, остаётся нетронутым.
        state = make_state(999)
        faq_msg = make_flow_message(chat_id=999, text=texts.MENU_FAQ)
        await faq.show_faq_list(faq_msg, state)
        data = await state.get_data()
        nav_id_after_faq = data.get(flow._NAV_ANCHOR_MSG_KEY)

        cancel_msg = make_flow_message(chat_id=999, text="/cancel")
        await start.cmd_cancel(cancel_msg, state)

        cancel_msg.answer.assert_awaited_once()  # только сам cancel-текст, NAV anchor не пересоздан
        self.assertIsNone(cancel_msg.answer.await_args.kwargs.get("reply_markup"))
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), nav_id_after_faq)


class AdminCancelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Реальный проход через хендлеры (не изолированный вызов
    _resolve_cancel) для сценария, который явно попросили перепроверить:
    Услуги -> редактирование услуги -> Опции -> Добавить опцию -> Отмена
    должна вернуть в меню опций ИМЕННО этой услуги, с сохранённым
    service_id, а не в корень /admin."""

    async def test_cancel_from_nested_option_add_returns_to_options_with_service_id(self):
        # NAV anchor засеян заранее — до P1-3 (_resolve_cancel bug class,
        # продолжение Batch 0) admin_cancel уничтожал бы его через
        # state.set_data(next_data); теперь flow.set_data_keep_nav должен
        # сохранить И cancel context (service_id), И NAV anchor одновременно.
        state = make_state()
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 1001, flow._NAV_ANCHOR_CHAT_KEY: 555})

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
        data_after_cancel = await state.get_data()
        self.assertEqual(data_after_cancel.get("service_id"), "LEND")  # cancel context сохранён
        self.assertEqual(data_after_cancel.get(flow._NAV_ANCHOR_MSG_KEY), 1001)  # NAV anchor сохранён
        self.assertEqual(data_after_cancel.get(flow._NAV_ANCHOR_CHAT_KEY), 555)
        # ровно эти 3 ключа — "cancel_to" (уже неактуальный) реально исчез,
        # ничего постороннего не протекло
        self.assertEqual(set(data_after_cancel.keys()), {"service_id", flow._NAV_ANCHOR_MSG_KEY, flow._NAV_ANCHOR_CHAT_KEY})
        cancel_cb.message.edit_text.assert_awaited_once()
        self.assertIn("Опции", cancel_cb.message.edit_text.await_args.args[0])


class AdminCancelNavAnchorTests(unittest.IsolatedAsyncioTestCase):
    """P1-3 аудит, продолжение Batch 0: admin_cancel/admin_cancel_command
    оба вызывали _resolve_cancel(...) -> state.set_data(next_data), что
    полностью заменяло FSM data и уничтожало flow._NAV_ANCHOR_MSG_KEY/
    _NAV_ANCHOR_CHAT_KEY, хотя физический NAV anchor оставался в Telegram.
    flow.reset_state_keep_nav здесь НЕ подходит (она стёрла бы и cancel
    context — service_id/case_id, подтверждено эмпирически при аудите) —
    новый flow.set_data_keep_nav сохраняет NAV anchor ПОВЕРХ next_data, не
    вместо него: cancel context и NAV tracking сохраняются одновременно."""

    def setUp(self):
        # sections/images ветки _resolve_cancel читают content_store.list_cases()
        # — изолируем, как и остальные admin-тесты в этом файле, а не
        # полагаемся на реальный data/portfolio.json проекта.
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)
        self.actor = 888

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_with_nav(self, nav_msg_id: int = 1001, **extra_data) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            **extra_data,
        })
        return state

    # ---- A. Inline "❌ Отмена" с контекстом ----
    async def test_inline_cancel_preserves_nav_and_service_id_context(self):
        state = await self._state_with_nav(service_id="LEND", cancel_to="options")
        cb = make_callback("admincancel", chat_id=self.actor)
        await admin.admin_cancel(cb, state)

        data = await state.get_data()
        self.assertEqual(data.get("service_id"), "LEND")  # cancel context сохранён
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)  # NAV anchor сохранён
        self.assertEqual(data.get(flow._NAV_ANCHOR_CHAT_KEY), self.actor)
        self.assertNotIn("cancel_to", data)  # старый нерелевантный key реально исчез
        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick.state)

    # ---- B. Текстовый /cancel ----
    async def test_cancel_command_preserves_nav_and_context(self):
        # make_flow_message_factory, а не make_text_message: с Batch про
        # message lifecycle (см. ниже, item F) admin_cancel_command реально
        # вызывает message.delete()/message.bot.delete_message() через
        # flow.cancel_transient — make_text_message их не предоставляет.
        state = await self._state_with_nav(case_id="case_1", cancel_to="sections")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=2900)(text="/cancel")
        await admin.admin_cancel_command(msg, state)

        data = await state.get_data()
        self.assertEqual(data.get("case_id"), "case_1")
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)
        self.assertEqual(data.get(flow._NAV_ANCHOR_CHAT_KEY), self.actor)
        self.assertNotIn("cancel_to", data)
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)
        msg.answer.assert_awaited_once()  # тот же next_state/next_data behavior, что и раньше

    # ---- C. Context-free cancel (root/cases/faq/pricing/categories/backup) ----
    async def test_context_free_cancel_preserves_only_nav(self):
        state = await self._state_with_nav(cancel_to="faq", junk="must disappear")
        cb = make_callback("admincancel", chat_id=self.actor)
        await admin.admin_cancel(cb, state)

        data = await state.get_data()
        self.assertEqual(data, {flow._NAV_ANCHOR_MSG_KEY: 1001, flow._NAV_ANCHOR_CHAT_KEY: self.actor})
        self.assertIsNone(await state.get_state())

    # ---- D. Context-bearing cancel: images -> case_id ----
    async def test_images_cancel_preserves_case_id_and_nav(self):
        state = await self._state_with_nav(case_id="case_7", cancel_to="images")
        cb = make_callback("admincancel", chat_id=self.actor)
        await admin.admin_cancel(cb, state)

        data = await state.get_data()
        self.assertEqual(data.get("case_id"), "case_7")
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)
        self.assertEqual(await state.get_state(), AdminStates.case_images_menu.state)

    # ---- E. ensure_nav_anchor после cancel ничего не пересоздаёт ----
    async def test_ensure_nav_anchor_after_cancel_does_not_recreate(self):
        state = await self._state_with_nav(cancel_to="pricing")
        cb = make_callback("admincancel", chat_id=self.actor)
        await admin.admin_cancel(cb, state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=6000)()
        created = await flow.ensure_nav_anchor(probe, state)

        self.assertFalse(created)  # anchor уже "существует" по мнению ensure_nav_anchor
        probe.answer.assert_not_awaited()  # ни одного нового WELCOME
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)  # id не изменился

    # ---- F. /cancel: message lifecycle (P1-3, задача про orphan messages) ----
    # Inline "❌ Отмена" избегает orphan-сообщений структурно — редактирует
    # callback.message на месте (см. admin_cancel), поэтому там нечего
    # чистить. У текстового /cancel такой прямой ссылки нет, поэтому он
    # опирается на flow.cancel_transient (best-effort по _ANCHOR_MSG_KEY/
    # _ANCHOR_CHAT_KEY) — см. её докстринг в bot/flow.py про архитектурную
    # границу: надёжно работает только пока сценарий ещё не прошёл ни
    # одного своего текстового/фото шага (первый вопрос после чистой
    # inline-навигации, и весь FAQ-add wizard).

    async def test_cancel_command_deletes_tracked_transient_and_trigger(self):
        """Plain root cancel, TRANSIENT-сообщение отслеживается (типичный
        случай — /cancel на первом текстовом шаге мастера): /cancel должен
        удалить и старый prompt, и сам себя (триггер), сохранив NAV."""
        state = await self._state_with_nav(
            cancel_to="root",
            **{flow._ANCHOR_MSG_KEY: 2001, flow._ANCHOR_CHAT_KEY: self.actor},
        )
        msg = make_flow_message_factory(chat_id=self.actor, start_id=3000)(text="/cancel")
        msg.delete = AsyncMock(wraps=msg.delete)

        await admin.admin_cancel_command(msg, state)

        msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=2001)
        msg.delete.assert_awaited_once()  # RULE 1: сам /cancel тоже удалён
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)
        self.assertEqual(data.get(flow._NAV_ANCHOR_CHAT_KEY), self.actor)
        self.assertNotIn(flow._ANCHOR_MSG_KEY, data)  # старый transient-ключ дальше не протёк
        self.assertIsNone(await state.get_state())
        msg.answer.assert_awaited_once()

    async def test_cancel_command_context_service_id_cleans_messages(self):
        """Контекстный cancel (service_id/options) — тот же lifecycle
        cleanup, плюс сохранённый context и корректный next_state."""
        state = await self._state_with_nav(
            service_id="LEND", cancel_to="options",
            **{flow._ANCHOR_MSG_KEY: 2002, flow._ANCHOR_CHAT_KEY: self.actor},
        )
        msg = make_flow_message_factory(chat_id=self.actor, start_id=3100)(text="/cancel")
        msg.delete = AsyncMock(wraps=msg.delete)

        await admin.admin_cancel_command(msg, state)

        msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=2002)
        msg.delete.assert_awaited_once()
        data = await state.get_data()
        self.assertEqual(data.get("service_id"), "LEND")
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)
        self.assertEqual(await state.get_state(), AdminStates.edit_service_field_pick.state)
        msg.answer.assert_awaited_once()

    async def test_cancel_command_context_case_id_cleans_messages(self):
        """Контекстный cancel (case_id/images) — тот же lifecycle cleanup,
        context/next_state как и раньше (Batch 1)."""
        state = await self._state_with_nav(
            case_id="case_9", cancel_to="images",
            **{flow._ANCHOR_MSG_KEY: 2003, flow._ANCHOR_CHAT_KEY: self.actor},
        )
        msg = make_flow_message_factory(chat_id=self.actor, start_id=3200)(text="/cancel")
        msg.delete = AsyncMock(wraps=msg.delete)

        await admin.admin_cancel_command(msg, state)

        msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=2003)
        msg.delete.assert_awaited_once()
        data = await state.get_data()
        self.assertEqual(data.get("case_id"), "case_9")
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)
        self.assertEqual(await state.get_state(), AdminStates.case_images_menu.state)

    async def test_ensure_nav_anchor_after_cancel_command_does_not_recreate(self):
        """/cancel не должен приводить к дублированию NAV anchor при
        следующем ensure_nav_anchor — тот же принцип, что и для inline
        cancel выше (item E)."""
        state = await self._state_with_nav(cancel_to="pricing")
        msg = make_flow_message_factory(chat_id=self.actor, start_id=3300)(text="/cancel")
        await admin.admin_cancel_command(msg, state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=6100)()
        created = await flow.ensure_nav_anchor(probe, state)

        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)

    async def test_cancel_command_without_tracked_transient_only_deletes_trigger(self):
        """Для большинства admin.py-мастеров _ANCHOR_MSG_KEY НЕ
        отслеживается (см. P1-3 диагностику — только /admin root и
        FAQ-add wizard используют flow.py) — /cancel не должен ни падать,
        ни пытаться удалить несуществующий id; триггер всё равно
        удаляется (RULE 1 применим независимо от tracking) — это и есть
        документированная архитектурная граница cancel_transient, а не
        баг: полное решение требует миграции остальных хендлеров на
        flow.py (вне scope этой задачи)."""
        state = await self._state_with_nav(cancel_to="root")  # без _ANCHOR_MSG_KEY
        msg = make_flow_message_factory(chat_id=self.actor, start_id=3400)(text="/cancel")
        msg.delete = AsyncMock(wraps=msg.delete)

        await admin.admin_cancel_command(msg, state)

        msg.bot.delete_message.assert_not_awaited()  # нечего было удалять
        msg.delete.assert_awaited_once()  # но триггер всё равно чистится
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 1001)
        self.assertIsNone(await state.get_state())

    async def test_inline_cancel_does_not_invoke_cancel_transient(self):
        """Регрессия: cancel_transient (новая message-lifecycle логика
        текстового /cancel) не должна затрагивать inline "❌ Отмена" — она
        продолжает работать как раньше, просто edit_text на месте."""
        state = await self._state_with_nav(cancel_to="root")
        cb = make_callback("admincancel", chat_id=self.actor)
        with patch("bot.handlers.admin.flow.cancel_transient", new=AsyncMock()) as mocked:
            await admin.admin_cancel(cb, state)
        mocked.assert_not_awaited()
        cb.message.edit_text.assert_awaited_once()


def make_photo_message(chat_id: int) -> SimpleNamespace:
    # delete/bot.delete_message/bot.edit_message_text (P1-3, Batch 1) —
    # нужны для photo-хендлеров, переведённых на flow.step_from_text
    # (сейчас только cases_add_photo); хендлеры, которые их не вызывают
    # (например case_image_add_receive, всё ещё raw message.answer),
    # этот довесок не задевает.
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        photo=[SimpleNamespace(file_id="fake_file_id")],
        document=None,
        text=None,
        delete=AsyncMock(),
        bot=SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_path="photos/fake.jpg")),
            download_file=AsyncMock(),
            delete_message=AsyncMock(),
            edit_message_text=AsyncMock(),
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

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = 999
        await content_store.add_case(
            str(self.actor), case_id="case_ctor_test", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _case(self):
        return next(c for c in await content_store.list_cases() if c["id"] == "case_ctor_test")

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
        self.assertEqual(len((await self._case())["images"]), 2)

        await admin.case_image_picked(make_callback("admincaseimgpick:1", chat_id=self.actor), state)
        await admin.case_image_action(make_callback("admincaseimgact:cover", chat_id=self.actor), state)
        case = await self._case()
        self.assertEqual(case["cover"], case["images"][1])

        await admin.case_image_picked(make_callback("admincaseimgpick:1", chat_id=self.actor), state)
        await admin.case_image_action(make_callback("admincaseimgact:delete", chat_id=self.actor), state)
        self.assertEqual(len((await self._case())["images"]), 1)

    async def test_opening_sections_menu_does_not_crash_and_add_edit_works(self):
        state = await self._state()
        cb = make_callback("admineditfield:sections", chat_id=self.actor)
        await admin.cases_edit_field(cb, state)  # раньше падало здесь с TypeError
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)

        # make_flow_message_factory, а не make_text_message (P1-3, Batch 1):
        # оба хендлера теперь реально вызывают flow.step_from_text
        # (message.delete()/message.bot.edit_message_text) — make_text_message
        # их не предоставляет.
        make_sec_msg = make_flow_message_factory(chat_id=self.actor, start_id=5000)
        await admin.case_section_add_start(make_callback("admincasesecaction:add", chat_id=self.actor), state)
        await admin.case_section_add_type(make_callback("admincasesectype:text", chat_id=self.actor), state)
        await admin.case_section_add_title(make_sec_msg(text="Задача"), state)
        await admin.case_section_add_content(make_sec_msg(text="Описание задачи"), state)
        self.assertEqual((await self._case())["sections"][0], {"type": "text", "title": "Задача", "content": "Описание задачи"})

        await admin.case_section_picked(make_callback("admincasesecpick:0", chat_id=self.actor), state)
        await admin.case_section_action(make_callback("admincasesecact:title", chat_id=self.actor), state)
        # make_flow_message_factory, а не make_text_message (P1-3, Batch 3):
        # case_section_edit_value теперь вызывает flow.step_from_text и на
        # success-ветке, не только на retry.
        await admin.case_section_edit_value(make_flow_message_factory(chat_id=self.actor, start_id=5100)(text="Задача проекта"), state)
        self.assertEqual((await self._case())["sections"][0]["title"], "Задача проекта")

    async def test_category_and_external_url_edit_via_real_handlers(self):
        state = await self._state()
        await admin.cases_edit_field(make_callback("admineditfield:category", chat_id=self.actor), state)
        await admin.cases_edit_category(make_callback("admincasenewcat:site", chat_id=self.actor), state)
        self.assertEqual((await self._case())["type"], "site")

        await admin.cases_edit_field(make_callback("admineditfield:external_url", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_case_value.state)
        # make_flow_message_factory, а не make_text_message (P1-3, Batch 3):
        # cases_edit_value теперь вызывает flow.step_from_text и на
        # success-ветке, не только на retry.
        await admin.cases_edit_value(make_flow_message_factory(chat_id=self.actor, start_id=5200)(text="https://behance.net/gallery/x"), state)
        self.assertEqual((await self._case())["external_url"], "https://behance.net/gallery/x")


class AdminBackupHandlersTests(unittest.IsolatedAsyncioTestCase):
    """Реальный проход через bot/handlers/admin.py для /admin -> Бэкап:
    экспорт шлёт .zip документом, импорт принимает загруженный .zip и
    реально восстанавливает изменённые данные — не мок, настоящий
    content_store.import_backup_bytes через настоящий хендлер."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json", "leads.json"):
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
                answer=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    def _make_zip_document_message(self, zip_bytes: bytes):
        # delete/bot.delete_message/bot.edit_message_text (P1-3, Batch 2) —
        # BadZipFile-ветка backup_import_receive теперь реально вызывает
        # flow.step_from_text; остальные ветки (validation/snapshot/restore
        # failed, success) остаются raw message.answer (terminal-переход в
        # AdminStates.backup_menu, не retry — см. Batch 2 отчёт), им эти
        # атрибуты не нужны, но лишний AsyncMock их не задевает.
        return SimpleNamespace(
            chat=SimpleNamespace(id=self.actor),
            document=SimpleNamespace(file_id="fake_zip_id"),
            delete=AsyncMock(),
            bot=SimpleNamespace(
                get_file=AsyncMock(return_value=SimpleNamespace(file_path="documents/backup.zip")),
                download_file=AsyncMock(return_value=io.BytesIO(zip_bytes)),
                delete_message=AsyncMock(),
                edit_message_text=AsyncMock(),
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
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(str(self.actor), "landing", "SITE")
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "SITE")

        state = make_state(self.actor)
        await admin.backup_import_start(make_callback("adminbackupaction:import", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_wait_file.state)

        await admin.backup_import_receive(self._make_zip_document_message(zip_bytes), state)

        self.assertEqual(await content_store.default_related_service_for_type("landing"), "LEND")
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_import_bad_zip_via_real_handler_does_not_crash(self):
        state = make_state(self.actor)
        msg = self._make_zip_document_message(b"not a zip")
        await admin.backup_import_receive(msg, state)
        msg.answer.assert_awaited_once()
        self.assertIn("повреждён", msg.answer.await_args.args[0])

    async def test_import_snapshot_failure_via_real_handler_gives_clear_message_and_resets_state(self):
        # P2-6, второй design review: Phase 1 (снапшот) падает НЕ из-за
        # UpstashKeyMissingError — реальная сетевая/Upstash-ошибка при
        # чтении текущего значения перед записью. Phase 2 не должен
        # начаться (zero writes), админ должен получить понятное сообщение
        # без утечки внутренних деталей исходного исключения, а FSM-state
        # должен сброситься к backup_menu, а не зависнуть.
        zip_bytes = await content_store.export_backup_bytes()
        original_before = (Path(self.tmpdir) / "leads.json").read_bytes()
        state = make_state(self.actor)
        msg = self._make_zip_document_message(zip_bytes)

        with patch("bot.content_store._read", side_effect=RuntimeError("simulated snapshot read failure")):
            await admin.backup_import_receive(msg, state)

        msg.answer.assert_awaited_once()
        reply_text = msg.answer.await_args.args[0]
        self.assertIn("отменено", reply_text.lower())
        self.assertNotIn("simulated snapshot read failure", reply_text)  # деталь исходного исключения не утекает
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)
        self.assertEqual((Path(self.tmpdir) / "leads.json").read_bytes(), original_before)  # ничего не записано


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
        # make_flow_message_factory, а не make_text_message (P1-3, Batch 3):
        # about_edit_value теперь реально вызывает flow.step_from_text
        # (message.delete()/message.bot.edit_message_text) на success-ветке.
        self.assertIn("location", (await content_store.get_about())["needs_review_fields"])
        state = await self._state()
        await admin.about_edit_field(make_callback("admineditabout:location", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.edit_about_value.state)
        await admin.about_edit_value(make_flow_message_factory(chat_id=self.actor, start_id=9100)(text="Москва, удалённо"), state)
        about = await content_store.get_about()
        self.assertEqual(about["location"], "Москва, удалённо")
        self.assertNotIn("location", about["needs_review_fields"])

    async def test_skills_edit_is_comma_split_list_separate_from_tools(self):
        state = await self._state()
        await admin.about_edit_field(make_callback("admineditabout:skills", chat_id=self.actor), state)
        await admin.about_edit_value(make_flow_message_factory(chat_id=self.actor, start_id=9200)(text="UX-исследования, Прототипирование"), state)
        about = await content_store.get_about()
        self.assertEqual(about["skills"], ["UX-исследования", "Прототипирование"])
        self.assertNotEqual(about["skills"], about["tools"])


class FakeUpstash:
    """Имитирует Upstash Redis REST API в памяти (без сети) — команды
    GET/SET кодируются как JSON-массив в теле POST-запроса, ответ —
    {"result": ...}, ровно как настоящий Upstash REST.

    fail_on — опциональный набор (cmd, key) пар, на которых urlopen должен
    вместо ответа поднять исключение (симуляция сетевого сбоя) —
    используется тестами P0-1 storage-инициализации, чтобы проверить, что
    MARKER_KEY не выставляется при сбое GET/SET.

    error_on — опциональный набор (cmd, key) пар, на которых Upstash
    отвечает НЕ исключением (соединение прошло успешно), а собственным
    JSON-телом с полем "error" — используется тестами P2 (structured
    logging), чтобы проверить именно ветку `body.get("error")` в
    _upstash_command, отдельную от сетевых сбоев."""

    def __init__(self, fail_on: set[tuple[str, str]] | None = None, error_on: set[tuple[str, str]] | None = None):
        self.store: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.fail_on = fail_on or set()
        self.error_on = error_on or set()

    def urlopen(self, req, timeout=10):
        args = json.loads(req.data.decode("utf-8"))
        self.calls.append(tuple(args))
        cmd = args[0]
        if (cmd, args[1]) in self.fail_on:
            raise ConnectionError(f"simulated Upstash failure on {args[:2]}")
        if (cmd, args[1]) in self.error_on:
            body = json.dumps({"error": "simulated Upstash error response"}).encode("utf-8")
            return _FakeUpstashResponse(body)
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


class UpstashPersistenceTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_first_read_seeds_from_local_file_and_persists_to_redis(self):
        data = await content_store._read("ui_config.json")
        self.assertIn("menu", data)
        self.assertIn("ui_config.json", self.fake.store)  # засеяно в Redis сразу

    async def test_write_then_read_round_trips_through_redis_not_local_disk(self):
        await content_store._write("ui_config.json", {"menu": {"portfolio": False}})
        self.assertEqual(json.loads(self.fake.store["ui_config.json"]), {"menu": {"portfolio": False}})

        # локальный файл на диске НЕ тронут — вся мутация ушла только в Redis
        with open(Path(__file__).resolve().parent.parent / "data" / "ui_config.json", encoding="utf-8") as f:
            real_local = json.load(f)
        self.assertNotEqual(real_local, {"menu": {"portfolio": False}})

        # повторное чтение отдаёт то, что записали, а не переседевает заново
        self.assertEqual(await content_store._read("ui_config.json"), {"menu": {"portfolio": False}})
        self.assertEqual(self.fake.calls.count(("GET", "ui_config.json")), 1)  # ровно одно чтение из Redis


class StorageInitializationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_empty_new_database_seeds_all_and_sets_marker(self):
        await content_store.ensure_storage_initialized()
        for filename in content_store.DATA_FILENAMES:
            self.assertIn(filename, self.fake.store)
        self.assertIn(content_store.MARKER_KEY, self.fake.store)

    async def test_partially_initialized_database_seeds_only_missing_keys(self):
        self.fake.store["leads.json"] = json.dumps({"leads": [{"id": 999, "custom": True}]})
        await content_store.ensure_storage_initialized()
        # уже существовавший ключ не тронут
        self.assertEqual(json.loads(self.fake.store["leads.json"]), {"leads": [{"id": 999, "custom": True}]})
        # остальные — досеяны
        for filename in content_store.DATA_FILENAMES:
            self.assertIn(filename, self.fake.store)
        self.assertIn(content_store.MARKER_KEY, self.fake.store)

    async def test_existing_production_data_without_marker_is_untouched(self):
        # Симулируем уже давно работающий продакшн: реальные данные во всех
        # 6 ключах, но marker ещё не существовал (появился только в этом
        # фиксе) — критический тест безопасной миграции, см. аудит.
        original = {}
        for filename in content_store.DATA_FILENAMES:
            value = json.dumps({"marker_test_sentinel": filename})
            self.fake.store[filename] = value
            original[filename] = value

        await content_store.ensure_storage_initialized()

        for filename in content_store.DATA_FILENAMES:
            self.assertEqual(self.fake.store[filename], original[filename])  # ни байта не изменилось
        self.assertIn(content_store.MARKER_KEY, self.fake.store)

    async def test_marker_present_and_key_missing_raises_without_seeding(self):
        self.fake.store[content_store.MARKER_KEY] = "2026-01-01T00:00:00+00:00"
        with self.assertRaises(content_store.UpstashKeyMissingError):
            await content_store._read("leads.json")
        self.assertNotIn("leads.json", self.fake.store)  # НЕ засеяно
        self.assertFalse(any(c[0] == "SET" for c in self.fake.calls))  # ни одного SET

    async def test_marker_present_and_all_keys_present_reads_normally(self):
        self.fake.store[content_store.MARKER_KEY] = "2026-01-01T00:00:00+00:00"
        self.fake.store["ui_config.json"] = json.dumps({"menu": {"portfolio": True}})
        data = await content_store._read("ui_config.json")
        self.assertEqual(data, {"menu": {"portfolio": True}})

    async def test_invalid_json_in_existing_key_still_fails_loud(self):
        self.fake.store["ui_config.json"] = "{not valid json"
        with self.assertRaises(json.JSONDecodeError):
            await content_store._read("ui_config.json")

    async def test_network_failure_during_init_leaves_marker_unset(self):
        self.fake.fail_on = {("GET", "faq.json")}
        with self.assertRaises(ConnectionError):
            await content_store.ensure_storage_initialized()
        self.assertNotIn(content_store.MARKER_KEY, self.fake.store)

    async def test_set_failure_during_init_leaves_marker_unset(self):
        self.fake.fail_on = {("SET", "about.json")}
        with self.assertRaises(ConnectionError):
            await content_store.ensure_storage_initialized()
        self.assertNotIn(content_store.MARKER_KEY, self.fake.store)

    async def test_last_key_failure_still_prevents_marker(self):
        # DATA_FILENAMES = (portfolio, pricing, faq, about, ui_config, leads)
        # — валим именно последний (6-й) ключ цикла: даже если первые 5
        # успешно засеялись до него, marker всё равно не должен появиться.
        last_filename = content_store.DATA_FILENAMES[-1]
        self.fake.fail_on = {("SET", last_filename)}
        with self.assertRaises(ConnectionError):
            await content_store.ensure_storage_initialized()
        self.assertNotIn(content_store.MARKER_KEY, self.fake.store)
        for filename in content_store.DATA_FILENAMES[:-1]:
            self.assertIn(filename, self.fake.store)  # первые 5 уже успели засеяться — это ожидаемо

    async def test_upstash_disabled_is_noop(self):
        content_store.config.UPSTASH_REDIS_REST_URL = ""
        content_store.config.UPSTASH_REDIS_REST_TOKEN = ""
        await content_store.ensure_storage_initialized()
        self.assertEqual(self.fake.calls, [])  # ни одного сетевого вызова — локальный dev не затронут

    def test_marker_key_not_in_backup_filenames(self):
        self.assertNotIn(content_store.MARKER_KEY, content_store.DATA_FILENAMES)


class UpstashLoggingTests(unittest.IsolatedAsyncioTestCase):
    """_upstash_command centralized structured logging (P2, production-
    hardening аудит): любой сбой GET/SET должен быть однозначно виден в
    Render logs — операция + ключ, без содержимого/секретов — и не должен
    менять fail-loud поведение (исключение всегда пробрасывается как есть,
    без изменения типа/текста)."""

    FAKE_TOKEN = "fake-token-should-never-appear-in-logs"
    FAKE_PAYLOAD_MARKER = "SECRET_CONTACT_PHONE_+79990001122"

    def setUp(self):
        self.fake = FakeUpstash()
        self._orig_url = content_store.config.UPSTASH_REDIS_REST_URL
        self._orig_token = content_store.config.UPSTASH_REDIS_REST_TOKEN
        content_store.config.UPSTASH_REDIS_REST_URL = "https://fake-upstash.example/"
        content_store.config.UPSTASH_REDIS_REST_TOKEN = self.FAKE_TOKEN
        self._patch = patch("bot.content_store.urllib.request.urlopen", side_effect=self.fake.urlopen)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        content_store.config.UPSTASH_REDIS_REST_URL = self._orig_url
        content_store.config.UPSTASH_REDIS_REST_TOKEN = self._orig_token

    async def test_get_network_failure_logs_one_warning(self):
        self.fake.fail_on = {("GET", "leads.json")}
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(ConnectionError):
                await content_store._read("leads.json")
        self.assertEqual(len(log_ctx.output), 1)
        self.assertIn("Upstash GET failed: leads.json", log_ctx.output[0])

    async def test_set_network_failure_logs_one_warning(self):
        self.fake.fail_on = {("SET", "leads.json")}
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(ConnectionError):
                await content_store._write("leads.json", {"leads": []})
        self.assertEqual(len(log_ctx.output), 1)
        self.assertIn("Upstash SET failed: leads.json", log_ctx.output[0])

    async def test_upstash_explicit_error_response_logs_warning_and_raises_runtime_error(self):
        self.fake.error_on = {("GET", "leads.json")}
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(RuntimeError) as ctx:
                await content_store._read("leads.json")
        self.assertEqual(len(log_ctx.output), 1)
        self.assertIn("Upstash GET failed: leads.json", log_ctx.output[0])
        self.assertIn("Upstash error on GET", str(ctx.exception))  # текст существующего исключения не изменился

    async def test_successful_get_and_set_log_no_warning(self):
        self.fake.store["ui_config.json"] = json.dumps({"menu": {"portfolio": True}})
        with self.assertNoLogs("bot.content_store", level="WARNING"):
            await content_store._read("ui_config.json")
            await content_store._write("ui_config.json", {"menu": {"portfolio": False}})

    async def test_key_missing_logs_error_only_no_extra_warning(self):
        self.fake.store[content_store.MARKER_KEY] = "2026-01-01T00:00:00+00:00"
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(content_store.UpstashKeyMissingError):
                await content_store._read("leads.json")
        self.assertEqual(len(log_ctx.output), 1)  # ровно одна запись, не две
        self.assertTrue(log_ctx.output[0].startswith("ERROR"))
        self.assertIn("Upstash key missing: leads.json", log_ctx.output[0])

    async def test_initialization_failure_logs_warning_and_high_level_exception(self):
        self.fake.fail_on = {("SET", "about.json")}
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(ConnectionError):
                await content_store.ensure_storage_initialized()
        self.assertTrue(any("Upstash SET failed: about.json" in msg for msg in log_ctx.output))
        self.assertTrue(any("Upstash initialization failed" in msg for msg in log_ctx.output))

    async def test_local_mode_unaffected_no_warnings(self):
        # Локальный режим пишет на реальный диск (_write_local) — реальный
        # data/ трогать нельзя, изолируем DATA_DIR отдельным tempdir'ом
        # только для этого теста (остальные тесты класса работают через
        # fake Upstash в памяти и диска не касаются вовсе).
        tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        shutil.copy(real_data_dir / "ui_config.json", Path(tmpdir) / "ui_config.json")
        orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(tmpdir)
        content_store.config.UPSTASH_REDIS_REST_URL = ""
        content_store.config.UPSTASH_REDIS_REST_TOKEN = ""
        try:
            with self.assertNoLogs("bot.content_store", level="WARNING"):
                await content_store._write("ui_config.json", await content_store._read("ui_config.json"))
        finally:
            content_store.DATA_DIR = orig_data_dir
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.assertEqual(self.fake.calls, [])  # ни одного сетевого вызова вообще — Upstash выключен

    # ---- Secret-safety (P2, явное требование аудита) ----

    async def test_no_secrets_or_content_in_logs_across_all_failure_scenarios(self):
        real_payload = {
            "leads": [{
                "id": 1,
                "telegram": {"user_id": 123456789},
                "payload": {"phone": self.FAKE_PAYLOAD_MARKER},
            }]
        }
        all_output: list[str] = []

        self.fake.fail_on = {("GET", "leads.json")}
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(ConnectionError):
                await content_store._read("leads.json")
        all_output += log_ctx.output

        # SET-сбой с реальным чувствительным контентом в args[2]
        self.fake.fail_on = {("SET", "leads.json")}
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(ConnectionError):
                await content_store._write("leads.json", real_payload)
        all_output += log_ctx.output

        # explicit error-ответ Upstash тоже с реальным контентом в запросе
        self.fake.fail_on = set()
        self.fake.error_on = {("SET", "leads.json")}
        with self.assertLogs("bot.content_store", level="WARNING") as log_ctx:
            with self.assertRaises(RuntimeError):
                await content_store._write("leads.json", real_payload)
        all_output += log_ctx.output

        combined = "\n".join(all_output)
        self.assertNotIn(self.FAKE_TOKEN, combined)  # Upstash token
        self.assertNotIn(content_store.config.BOT_TOKEN, combined)  # BOT_TOKEN
        self.assertNotIn(self.FAKE_PAYLOAD_MARKER, combined)  # payload/contacts
        self.assertNotIn("123456789", combined)  # user_id


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """Собирает .zip с произвольными записями напрямую (в обход
    export_backup_bytes) — нужен тестам P1-1, которым нужно намеренно
    подсунуть битый/неполный/посторонний контент, который сам
    export_backup_bytes никогда бы не произвёл."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


class BackupExportImportTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_export_zip_contains_data_files_and_images(self):
        zip_bytes = await content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        self.assertIn("data/portfolio.json", names)
        self.assertIn("data/leads.json", names)
        self.assertIn("img/portfolio/case_1.jpg", names)
        self.assertIn("img/about/avatar.jpg", names)

    async def test_import_restores_json_field_that_changed_after_export(self):
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(self.actor, "landing", "SITE")  # мутируем после бэкапа
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "SITE")

        await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(await content_store.default_related_service_for_type("landing"), "LEND")  # вернулось из бэкапа

    async def test_import_restores_deleted_image_file(self):
        zip_bytes = await content_store.export_backup_bytes()
        (content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").unlink()
        self.assertFalse((content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").exists())

        await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertTrue((content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").exists())
        self.assertEqual((content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").read_bytes(), b"fake-jpeg-bytes")

    async def test_import_requires_designer(self):
        zip_bytes = await content_store.export_backup_bytes()
        with self.assertRaises(content_store.NotDesignerError):
            await content_store.import_backup_bytes("not-the-designer", zip_bytes)

    async def test_import_rejects_non_zip_bytes(self):
        with self.assertRaises(zipfile.BadZipFile):
            await content_store.import_backup_bytes(self.actor, b"not a zip file at all")

    # ---- P1-1 (production-hardening аудит): all-or-nothing restore + snapshot/rollback ----

    async def test_full_valid_backup_restores_all_json_and_reports_no_missing(self):
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(self.actor, "landing", "SITE")  # мутируем после бэкапа

        result = await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(sorted(result.restored_json), sorted(content_store.DATA_FILENAMES))
        self.assertEqual(result.missing_json, [])
        self.assertEqual(result.failed_images, [])
        self.assertIn("img/portfolio/case_1.jpg", result.restored_images)
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "LEND")  # вернулось из бэкапа

    async def test_import_corrupted_json_writes_nothing(self):
        original_faq = (Path(self.tmpdir) / "faq.json").read_bytes()
        original_leads = (Path(self.tmpdir) / "leads.json").read_bytes()
        zip_bytes = _make_zip({
            "data/faq.json": original_faq,
            "data/leads.json": b"{not valid json",
        })

        with self.assertRaises(content_store.BackupValidationError) as ctx:
            await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(ctx.exception.filename, "leads.json")
        self.assertIn("leads.json", ctx.exception.found_filenames)
        self.assertIn("faq.json", ctx.exception.found_filenames)
        # all-or-nothing: 0 записей, оба файла на диске не изменились — даже
        # заведомо валидный faq.json из того же архива не восстановлен
        self.assertEqual((Path(self.tmpdir) / "faq.json").read_bytes(), original_faq)
        self.assertEqual((Path(self.tmpdir) / "leads.json").read_bytes(), original_leads)

    async def test_import_invalid_shape_writes_nothing(self):
        original_leads = (Path(self.tmpdir) / "leads.json").read_bytes()
        bad_leads = json.dumps({"leads": "not-a-list"}).encode("utf-8")  # валидный JSON, неверная форма
        zip_bytes = _make_zip({"data/leads.json": bad_leads})

        with self.assertRaises(content_store.BackupValidationError) as ctx:
            await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(ctx.exception.filename, "leads.json")
        self.assertIn("list", ctx.exception.reason)
        self.assertEqual((Path(self.tmpdir) / "leads.json").read_bytes(), original_leads)

    async def test_import_missing_backup_file_does_not_break_others(self):
        zip_bytes = await content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entries = {n: zf.read(n) for n in zf.namelist() if n != "data/about.json"}
        trimmed_zip = _make_zip(entries)
        await content_store.update_portfolio_type_related_service(self.actor, "landing", "SITE")  # мутируем после бэкапа
        original_about = (Path(self.tmpdir) / "about.json").read_bytes()

        result = await content_store.import_backup_bytes(self.actor, trimmed_zip)

        self.assertIn("about.json", result.missing_json)
        self.assertNotIn("about.json", result.restored_json)
        self.assertEqual((Path(self.tmpdir) / "about.json").read_bytes(), original_about)  # не тронут
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "LEND")  # остальное восстановлено

    async def test_import_ignores_unrelated_archive_file(self):
        zip_bytes = await content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        entries["readme.txt"] = b"not a data file"
        entries["data/unknown.json"] = b'{"whatever": true}'
        combined_zip = _make_zip(entries)

        result = await content_store.import_backup_bytes(self.actor, combined_zip)

        self.assertEqual(sorted(result.restored_json), sorted(content_store.DATA_FILENAMES))
        self.assertEqual(result.missing_json, [])

    async def test_write_failure_on_first_json_leaves_no_state_change(self):
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(self.actor, "landing", "SITE")  # мутируем после бэкапа
        pre_restore = {name: (Path(self.tmpdir) / name).read_bytes() for name in content_store.DATA_FILENAMES}

        with patch("bot.content_store._write", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaises(content_store.BackupRestoreFailedError) as ctx:
                await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(ctx.exception.failed_filename, content_store.DATA_FILENAMES[0])  # portfolio.json — первый
        self.assertEqual(ctx.exception.rolled_back, [])  # нечего было откатывать — ничего не успело записаться
        self.assertEqual(ctx.exception.rollback_failed, [])
        for name in content_store.DATA_FILENAMES:
            self.assertEqual((Path(self.tmpdir) / name).read_bytes(), pre_restore[name])  # 0 изменений

    async def test_write_failure_on_nth_json_rolls_back_previous_byte_for_byte(self):
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(self.actor, "landing", "SITE")  # мутируем после бэкапа
        # Сравниваем по распарсенному значению, а не сырым байтам: rollback
        # идёт через _write() -> json.dump(indent=2), который канонически
        # переформатирует JSON (переносы строк, отступы) независимо от
        # исходного форматирования файла на диске — это не потеря данных
        # (round-trip через json.loads/dumps уже происходит при КАЖДОЙ
        # обычной записи в этом коде, не только при откате), поэтому
        # "byte-for-byte" здесь означает содержимое, а не байты файла.
        pre_restore = {name: json.loads((Path(self.tmpdir) / name).read_bytes()) for name in content_store.DATA_FILENAMES}

        fail_on = "about.json"  # 4-й файл в DATA_FILENAMES — падение НЕ на первом
        original_write = content_store._write

        async def fake_write(filename, data):
            if filename == fail_on:
                raise RuntimeError("simulated write failure")
            return await original_write(filename, data)

        with patch("bot.content_store._write", side_effect=fake_write):
            with self.assertRaises(content_store.BackupRestoreFailedError) as ctx:
                await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(ctx.exception.failed_filename, fail_on)
        self.assertEqual(sorted(ctx.exception.rolled_back), ["faq.json", "portfolio.json", "pricing.json"])
        self.assertEqual(ctx.exception.rollback_failed, [])

        # ВСЕ 6 файлов — содержимое как ДО попытки restore: три успешно
        # откатились, остальные (включая упавший) и не трогались
        for name in content_store.DATA_FILENAMES:
            self.assertEqual(json.loads((Path(self.tmpdir) / name).read_bytes()), pre_restore[name])

    async def test_rollback_failure_is_loud(self):
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(self.actor, "landing", "SITE")  # мутируем после бэкапа

        fail_forward_on = "about.json"     # 4-й — здесь падает сама запись
        fail_rollback_on = "pricing.json"  # 2-й — при откате его запись тоже падает
        original_write = content_store._write
        calls: dict[str, int] = {}

        async def fake_write(filename, data):
            calls[filename] = calls.get(filename, 0) + 1
            if filename == fail_forward_on:
                raise RuntimeError("simulated forward write failure")
            if filename == fail_rollback_on and calls[filename] == 2:
                raise RuntimeError("simulated rollback write failure")
            return await original_write(filename, data)

        with patch("bot.content_store._write", side_effect=fake_write):
            with self.assertLogs("bot.content_store", level="ERROR") as log_ctx:
                with self.assertRaises(content_store.BackupRestoreFailedError) as ctx:
                    await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(ctx.exception.failed_filename, fail_forward_on)
        self.assertEqual(sorted(ctx.exception.rolled_back), ["faq.json", "portfolio.json"])
        self.assertEqual(ctx.exception.rollback_failed, [fail_rollback_on])
        self.assertTrue(any("ROLLBACK" in msg.upper() for msg in log_ctx.output))  # громкий лог о неудавшемся откате

    async def test_binary_write_happens_only_after_json_phase_succeeds(self):
        zip_bytes = await content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        entries["img/portfolio/new_case.jpg"] = b"new-fake-image-bytes"
        combined_zip = _make_zip(entries)

        with patch("bot.content_store._write", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaises(content_store.BackupRestoreFailedError):
                await content_store.import_backup_bytes(self.actor, combined_zip)

        self.assertFalse((content_store.IMG_PORTFOLIO_DIR / "new_case.jpg").exists())  # Phase 3 не запускался


class BackupUpstashSafetyTests(unittest.IsolatedAsyncioTestCase):
    """P1-1 (production-hardening аудит), пересечение с P0-1: export должен
    полностью отказывать (не выдавать частичный ZIP), если хотя бы один
    DATA_FILENAMES-ключ пропал (UpstashKeyMissingError) — никогда не
    отдавать бэкап без leads.json под видом полноценного. Ни export, ни
    import никогда не должны трогать MARKER_KEY."""

    async def asyncSetUp(self):
        self.fake = FakeUpstash()
        self._orig_url = content_store.config.UPSTASH_REDIS_REST_URL
        self._orig_token = content_store.config.UPSTASH_REDIS_REST_TOKEN
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.config.UPSTASH_REDIS_REST_URL = "https://fake-upstash.example/"
        content_store.config.UPSTASH_REDIS_REST_TOKEN = "fake-token"
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"
        self._patch = patch("bot.content_store.urllib.request.urlopen", side_effect=self.fake.urlopen)
        self._patch.start()
        await content_store.ensure_storage_initialized()  # "продакшн" Upstash с реальными данными + marker

    def tearDown(self):
        self._patch.stop()
        content_store.config.UPSTASH_REDIS_REST_URL = self._orig_url
        content_store.config.UPSTASH_REDIS_REST_TOKEN = self._orig_token
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer

    async def test_export_with_all_keys_present_succeeds(self):
        zip_bytes = await content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        self.assertIn("data/leads.json", names)

    async def test_export_with_missing_key_fails_completely(self):
        del self.fake.store["leads.json"]  # ключ "пропал" уже ПОСЛЕ инициализации (P0-1 сценарий)
        with self.assertRaises(content_store.BackupExportError) as ctx:
            await content_store.export_backup_bytes()
        self.assertEqual(ctx.exception.missing_filenames, ["leads.json"])

    async def test_export_with_multiple_missing_keys_lists_all(self):
        del self.fake.store["leads.json"]
        del self.fake.store["faq.json"]
        with self.assertRaises(content_store.BackupExportError) as ctx:
            await content_store.export_backup_bytes()
        self.assertEqual(sorted(ctx.exception.missing_filenames), ["faq.json", "leads.json"])

    async def test_import_and_export_never_touch_marker(self):
        calls_before = len(self.fake.calls)
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.import_backup_bytes(self.actor, zip_bytes)
        new_calls = self.fake.calls[calls_before:]
        self.assertFalse(any(c[1] == content_store.MARKER_KEY for c in new_calls))


class StorageConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """P1-1/P2-6/P2-7 (production-hardening аудит, второй design review) —
    per-key asyncio.Lock model: event loop не блокируется медленным Upstash
    (asyncio.to_thread), single-key read-modify-write атомарен относительно
    других writer'ов того же ключа, РАЗНЫЕ ключи не блокируют друг друга
    (это то, что отличает per-key lock от отклонённого design'а с единым
    global lock), multi-key операции (delete_service, backup) берут locks
    строго в каноническом порядке DATA_FILENAMES и не создают deadlock,
    backup restore атомарен относительно других content_store readers/
    writers внутри процесса.

    Задержки — искусственные (time.sleep внутри fake urlopen, исполняется в
    worker-треде asyncio.to_thread) или asyncio.sleep — НЕ реальная сетевая
    задержка (см. явное требование ТЗ)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in content_store.DATA_FILENAMES:
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        self._orig_url = content_store.config.UPSTASH_REDIS_REST_URL
        self._orig_token = content_store.config.UPSTASH_REDIS_REST_TOKEN
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        content_store.config.UPSTASH_REDIS_REST_URL = self._orig_url
        content_store.config.UPSTASH_REDIS_REST_TOKEN = self._orig_token
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- 1. Event loop responsiveness (P1-1) ----

    async def test_slow_upstash_call_does_not_block_event_loop_heartbeat(self):
        # Считать тики heartbeat НЕ подходит как метрика: heartbeat всё
        # равно рано или поздно сделает все N итераций независимо от того,
        # блокирован loop или нет (asyncio.gather просто отложит его старт)
        # — единственный надёжный дискриминирующий сигнал: суммарное
        # wall-clock время. Если _read блокирует loop — heartbeat стартует
        # только ПОСЛЕ него (время суммируется: ~slow + ~heartbeat). Если не
        # блокирует (to_thread) — оба идут параллельно (время ~= max(slow,
        # heartbeat)). Проверено вручную против sync-эквивалента _read
        # (прямой вызов _upstash_command без to_thread): ~0.8s vs ~0.47s
        # для этих же длительностей — разница явная и устойчивая.
        content_store.config.UPSTASH_REDIS_REST_URL = "https://fake-upstash.example/"
        content_store.config.UPSTASH_REDIS_REST_TOKEN = "fake-token"

        def slow_urlopen(req, timeout=10):
            time.sleep(0.4)  # блокирует worker-тред to_thread, НЕ event loop и НЕ реальная сеть
            body = json.dumps({"result": json.dumps({"menu": {"portfolio": True}})}).encode("utf-8")
            return _FakeUpstashResponse(body)

        async def heartbeat():
            for _ in range(20):
                await asyncio.sleep(0.02)  # ~0.4s суммарно

        with patch("bot.content_store.urllib.request.urlopen", side_effect=slow_urlopen):
            started = time.monotonic()
            await asyncio.gather(content_store._read("ui_config.json"), heartbeat())
            elapsed = time.monotonic() - started

        # Последовательно (заблокированный loop) заняло бы ~0.4+0.4=0.8s+
        # (эмпирически измерено ~1.05s против sync-эквивалента _read);
        # параллельно (event loop свободен во время to_thread) — ~max(0.4,
        # 0.4)=0.4s (эмпирически ~0.65-0.67s с накладными расходами
        # планировщика). Порог 0.85s — с запасом ниже последовательного
        # сценария (~1.05s) и с запасом выше параллельного (~0.67s),
        # устойчиво к таймингам под нагрузкой CI.
        self.assertLess(elapsed, 0.85)

    # ---- 2. Lock ordering (P2-7) ----

    def test_lock_acquires_in_canonical_order_regardless_of_call_site_order(self):
        lock = content_store._lock("leads.json", "portfolio.json", "pricing.json")
        self.assertEqual(lock._filenames, ("portfolio.json", "pricing.json", "leads.json"))

    def test_no_code_bypasses_lock_helper_to_touch_locks_dict_directly(self):
        # Структурная гарантия против "reverse-order acquisition where-либо":
        # единственное место в модуле, где _locks[...] используется —
        # определение самого _StorageLock (__aenter__/__aexit__). Любой
        # будущий multi-key call site ОБЯЗАН идти через _lock(...), не может
        # случайно взять локи в произвольном порядке напрямую.
        import inspect
        src = inspect.getsource(content_store)
        lines_with_locks_dict = [
            i for i, line in enumerate(src.splitlines(), 1)
            if "_locks[" in line and not line.strip().startswith("#")
        ]
        class_start = next(i for i, line in enumerate(src.splitlines(), 1) if line.startswith("class _StorageLock"))
        class_end = next(i for i, line in enumerate(src.splitlines(), 1) if line.startswith("def _lock("))
        for lineno in lines_with_locks_dict:
            self.assertTrue(
                class_start <= lineno <= class_end,
                f"_locks[...] используется вне _StorageLock на строке {lineno} — потенциальный обход канонического порядка",
            )

    # ---- 3. Same-key writes serialize; different-key writes don't (P1-1 review: reject global lock) ----

    async def test_concurrent_operations_on_same_key_are_serialized(self):
        active = {"n": 0, "max": 0}

        async def locked_op():
            async with content_store._lock("faq.json"):
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                await asyncio.sleep(0.03)
                active["n"] -= 1

        await asyncio.gather(*(locked_op() for _ in range(5)))
        self.assertEqual(active["max"], 1)

    async def test_concurrent_operations_on_different_keys_proceed_concurrently(self):
        active = {"n": 0, "max": 0}

        async def locked_op(filename):
            async with content_store._lock(filename):
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                await asyncio.sleep(0.05)
                active["n"] -= 1

        await asyncio.gather(*(locked_op(name) for name in content_store.DATA_FILENAMES))
        # Все 6 разных ключей — НЕ должны сериализоваться друг за другом
        # (это ровно то отличие per-key lock от global lock, которое второй
        # design review потребовал сохранить).
        self.assertEqual(active["max"], len(content_store.DATA_FILENAMES))

    # ---- 4. Lead concurrency: Tier 1, same key (leads.json) ----

    async def test_concurrent_add_lead_calls_produce_two_distinct_leads_no_duplicate_or_lost_id(self):
        content_store.config.UPSTASH_REDIS_REST_URL = "https://fake-upstash.example/"
        content_store.config.UPSTASH_REDIS_REST_TOKEN = "fake-token"
        fake = FakeUpstash()

        def delayed_urlopen(req, timeout=10):
            time.sleep(0.02)  # искусственное окно гонки внутри worker-треда
            return fake.urlopen(req, timeout=timeout)

        with patch("bot.content_store.urllib.request.urlopen", side_effect=delayed_urlopen):
            lead_a, lead_b = await asyncio.gather(
                content_store.add_lead({"service_name": "A"}, {"user_id": 111, "username": "a"}),
                content_store.add_lead({"service_name": "B"}, {"user_id": 222, "username": "b"}),
            )

            self.assertNotEqual(lead_a["id"], lead_b["id"])  # ни дубликата, ни потери — оба id различны
            leads = (await content_store._read("leads.json"))["leads"]
        self.assertEqual(len(leads), 2)
        self.assertEqual({lead_a["id"], lead_b["id"]}, {l["id"] for l in leads})

    # ---- 5. Multi-key correctness: delete_service (Tier 2) ----

    async def test_delete_service_holds_both_locks_and_cleans_up_referential_integrity(self):
        await content_store.add_service(
            self.actor, service_id="TMP_SVC", name="Temp", base_price=1000, term_min=1, term_max=2, includes="x",
        )
        await content_store.update_case(self.actor, "case_1", related_service="TMP_SVC")

        self.assertTrue(await content_store.delete_service(self.actor, "TMP_SVC"))

        services = await content_store.list_services()
        self.assertNotIn("TMP_SVC", [s["id"] for s in services])
        case = next(c for c in await content_store.list_cases() if c["id"] == "case_1")
        self.assertIsNone(case["related_service"])  # portfolio.json тоже вычищен, а не оставлен висящей ссылкой

    # ---- 6. Deadlock avoidance: concurrent multi-key operations complete ----

    async def test_concurrent_multikey_operations_do_not_deadlock(self):
        await content_store.add_service(
            self.actor, service_id="TMP_SVC2", name="Temp2", base_price=500, term_min=1, term_max=2, includes="x",
        )
        # delete_service (portfolio+pricing) и export_backup_bytes (все 6) —
        # оба берут locks в каноническом порядке; конкурентный запуск не
        # должен зависнуть (asyncio.wait_for с коротким timeout доказывает
        # завершение, а не просто "тест не упал").
        results = await asyncio.wait_for(
            asyncio.gather(
                content_store.delete_service(self.actor, "TMP_SVC2"),
                content_store.export_backup_bytes(),
            ),
            timeout=5,
        )
        self.assertTrue(results[0])
        self.assertIsInstance(results[1], bytes)

    # ---- 7-9. Backup restore atomicity relative to other readers (P2-7) ----

    async def test_concurrent_reader_during_restore_never_sees_torn_cross_file_state(self):
        content_store.config.UPSTASH_REDIS_REST_URL = "https://fake-upstash.example/"
        content_store.config.UPSTASH_REDIS_REST_TOKEN = "fake-token"
        fake = FakeUpstash()

        def delayed_urlopen(req, timeout=10):
            time.sleep(0.01)
            return fake.urlopen(req, timeout=timeout)

        with patch("bot.content_store.urllib.request.urlopen", side_effect=delayed_urlopen):
            await content_store.ensure_storage_initialized()  # засевает fake Upstash реальными "old"-данными

            # "old" summary — известный baseline из реальных data/*.json.
            old_summary = await content_store.content_readiness_summary()

            # Готовим backup с ЗАВЕДОМО другой, легко отличимой сводкой —
            # 1 placeholder-кейс, 1 незаполненное поле about, 1 неотвеченный FAQ.
            portfolio = json.loads(fake.store["portfolio.json"])
            portfolio["cases"][0]["cover"] = "img/portfolio/placeholder.svg"
            about = json.loads(fake.store["about.json"])
            about["needs_review_fields"] = ["tagline"]
            faq = json.loads(fake.store["faq.json"])
            faq["faq"][0]["needs_review"] = True
            new_pricing = json.loads(fake.store["pricing.json"])
            new_ui_config = json.loads(fake.store["ui_config.json"])
            new_leads = json.loads(fake.store["leads.json"])
            zip_bytes = _make_zip({
                "data/portfolio.json": json.dumps(portfolio).encode("utf-8"),
                "data/pricing.json": json.dumps(new_pricing).encode("utf-8"),
                "data/faq.json": json.dumps(faq).encode("utf-8"),
                "data/about.json": json.dumps(about).encode("utf-8"),
                "data/ui_config.json": json.dumps(new_ui_config).encode("utf-8"),
                "data/leads.json": json.dumps(new_leads).encode("utf-8"),
            })
            new_summary_expected = {"placeholder_cases": 1, "about_pending_fields": 1, "faq_pending": 1}

            readers_results = []

            async def reader():
                for _ in range(8):
                    readers_results.append(await content_store.content_readiness_summary())
                    await asyncio.sleep(0.005)

            await asyncio.gather(
                content_store.import_backup_bytes(self.actor, zip_bytes),
                reader(),
            )
            # Финальное состояние после restore — гарантированно новое
            # (проверяем ещё внутри patch — Upstash "выключается" за
            # пределами этого блока).
            final_summary = await content_store.content_readiness_summary()

        # Каждое прочитанное значение — либо ЦЕЛИКОМ старое, либо ЦЕЛИКОМ
        # новое; ничего среднего (частично применённого restore) увидено не было.
        for result in readers_results:
            self.assertIn(result, (old_summary, new_summary_expected), f"torn cross-file read: {result}")
        self.assertEqual(final_summary, new_summary_expected)

    # ---- 10. Backup snapshot failure performs zero writes (P2-6) ----

    async def test_snapshot_failure_raises_before_any_write_and_leaves_data_untouched(self):
        zip_bytes = await content_store.export_backup_bytes()
        original = {name: (Path(self.tmpdir) / name).read_bytes() for name in content_store.DATA_FILENAMES}

        with patch("bot.content_store._read", side_effect=RuntimeError("simulated snapshot failure")):
            with self.assertRaises(content_store.BackupSnapshotError) as ctx:
                await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertEqual(ctx.exception.filename, content_store.DATA_FILENAMES[0])
        for name in content_store.DATA_FILENAMES:
            self.assertEqual((Path(self.tmpdir) / name).read_bytes(), original[name])

    # ---- 11. Missing key during snapshot preserves existing _MISSING semantics (P2-6, unchanged) ----

    async def test_missing_key_during_snapshot_is_not_a_backup_snapshot_error(self):
        content_store.config.UPSTASH_REDIS_REST_URL = "https://fake-upstash.example/"
        content_store.config.UPSTASH_REDIS_REST_TOKEN = "fake-token"
        fake = FakeUpstash()
        with patch("bot.content_store.urllib.request.urlopen", side_effect=fake.urlopen):
            await content_store.ensure_storage_initialized()
            zip_bytes = await content_store.export_backup_bytes()
            del fake.store["leads.json"]  # ключ "пропал" уже после экспорта (P0-1 сценарий)

            # UpstashKeyMissingError на снапшоте — НЕ BackupSnapshotError,
            # обрабатывается как _MISSING (нечего откатывать), restore
            # остальных файлов продолжается нормально.
            result = await content_store.import_backup_bytes(self.actor, zip_bytes)

        self.assertIn("leads.json", result.restored_json)  # leads.json всё равно восстановлен (SET, не откат)


class ReferentialIntegrityTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_custom_category_can_get_a_related_service(self):
        new_type = await content_store.add_portfolio_type(self.actor, type_id="cat_custom", label="Тестовая категория")
        self.assertIsNone(new_type["related_service"])  # раньше здесь навсегда осталось бы "нет связи"

        await content_store.update_portfolio_type_related_service(self.actor, "cat_custom", "SMM")
        self.assertEqual(await content_store.default_related_service_for_type("cat_custom"), "SMM")

    async def test_deleting_service_clears_it_from_categories_and_cases(self):
        await content_store.update_portfolio_type_related_service(self.actor, "landing", "LEND")
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "LEND")
        cases_before = [c["id"] for c in await content_store.list_cases() if c.get("related_service") == "LEND"]
        self.assertTrue(cases_before, "фикстура должна содержать хотя бы один кейс с related_service=LEND")

        await content_store.delete_service(self.actor, "LEND")

        self.assertIsNone(await content_store.default_related_service_for_type("landing"))
        dangling = [c["id"] for c in await content_store.list_cases() if c.get("related_service") == "LEND"]
        self.assertEqual(dangling, [], "не должно остаться related_service, указывающих на удалённую услугу")

    async def test_category_without_related_service_is_a_valid_steady_state(self):
        # "graphics" в фикстуре осознанно без related_service — объединяет
        # 3 услуги, однозначно выбрать нельзя (см. data/pricing.json -> groups).
        self.assertIsNone(await content_store.default_related_service_for_type("graphics"))
        case = await content_store.add_case(
            self.actor, case_id="case_test_graphics", title="Тест", type_id="graphics",
            cover="img/portfolio/x.svg", task="t",
            related_service=await content_store.default_related_service_for_type("graphics"),
        )
        self.assertIsNone(case["related_service"])

    async def test_category_without_cases_can_be_deleted_category_with_cases_cannot(self):
        empty_type = await content_store.add_portfolio_type(self.actor, type_id="cat_empty", label="Пустая категория")
        self.assertEqual(await content_store.count_cases_with_type(empty_type["id"]), 0)
        self.assertTrue(await content_store.delete_portfolio_type(self.actor, empty_type["id"]))

        self.assertGreater(await content_store.count_cases_with_type("landing"), 0)
        self.assertFalse(await content_store.delete_portfolio_type(self.actor, "landing"))

    async def test_deleting_a_service_with_no_cases_referencing_it_is_a_clean_noop_on_portfolio(self):
        service_id = await content_store.next_service_id()
        await content_store.add_service(
            self.actor, service_id=service_id, name="Тестовая услуга",
            base_price=1000, term_min=1, term_max=2, includes="—",
        )
        portfolio_before = await content_store._read("portfolio.json")

        self.assertTrue(await content_store.delete_service(self.actor, service_id))

        portfolio_after = await content_store._read("portfolio.json")
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


class ContentReadinessSummaryTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_summary_reflects_current_fixture_state(self):
        # demo-контент заполнен (см. content fill pass): реальные обложки,
        # все FAQ отвечены; About.education/links намеренно оставлены
        # пустыми — банер всё ещё должен предупреждать именно про них.
        summary = await content_store.content_readiness_summary()
        self.assertEqual(summary["placeholder_cases"], 0)
        self.assertEqual(summary["faq_pending"], 0)
        self.assertGreater(summary["about_pending_fields"], 0)
        text = await admin._admin_root_text()
        self.assertIn("⚠️", text)
        self.assertIn("Обо мне", text)

    async def test_summary_drops_to_zero_once_content_is_filled_in(self):
        for c in await content_store.list_cases():
            await content_store.update_case(self.actor, c["id"], cover="img/portfolio/real_photo.jpg")
        about = await content_store.get_about()
        for field in list(about.get("needs_review_fields", [])):
            await content_store.update_about_field(self.actor, field, "заполнено")
        for item in await content_store.list_faq():
            if item.get("needs_review"):
                await content_store.update_faq(self.actor, item["id"], answer="Готовый ответ")

        summary = await content_store.content_readiness_summary()
        self.assertEqual(summary, {"placeholder_cases": 0, "about_pending_fields": 0, "faq_pending": 0})
        self.assertNotIn("⚠️", await admin._admin_root_text())


class CaseCategoryChangeTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_related_service_follows_new_category_default_when_not_customized(self):
        case = await content_store.add_case(
            self.actor, case_id="case_cat_test1", title="Т", type_id="landing",
            cover="img/portfolio/x.svg", task="t", related_service="LEND",
        )
        self.assertEqual(case["related_service"], "LEND")  # совпадает с дефолтом старой категории

        await content_store.update_case_category(self.actor, "case_cat_test1", "site")

        updated = next(c for c in await content_store.list_cases() if c["id"] == "case_cat_test1")
        self.assertEqual(updated["type"], "site")
        self.assertEqual(updated["related_service"], "SITE", "не тронут вручную -> подставляем новый дефолт")

    async def test_manually_customized_related_service_survives_category_change(self):
        case = await content_store.add_case(
            self.actor, case_id="case_cat_test2", title="Т", type_id="landing",
            cover="img/portfolio/x.svg", task="t", related_service="UXUI",
        )
        self.assertEqual(case["related_service"], "UXUI")  # отличается от дефолта "landing" (LEND) -> выбран вручную

        await content_store.update_case_category(self.actor, "case_cat_test2", "site")

        updated = next(c for c in await content_store.list_cases() if c["id"] == "case_cat_test2")
        self.assertEqual(updated["type"], "site")
        self.assertEqual(updated["related_service"], "UXUI", "осознанный выбор не должен стираться сменой категории")

    async def test_update_case_category_returns_false_for_unknown_case(self):
        self.assertFalse(await content_store.update_case_category(self.actor, "case_does_not_exist", "site"))


class CaseImageManagementTests(unittest.IsolatedAsyncioTestCase):
    """Part 1 ТЗ: независимое управление галереей — добавить/удалить/
    переставить/назначить обложку, без пустых состояний и без потери
    обложки при удалении текущей."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"
        await content_store.add_case(
            self.actor, case_id="case_img_test", title="Т", type_id="landing",
            cover="img/portfolio/a.svg", task="t", related_service=None,
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _case(self):
        return next(c for c in await content_store.list_cases() if c["id"] == "case_img_test")

    async def test_add_image_does_not_override_existing_cover_unless_forced(self):
        await content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        case = await self._case()
        self.assertEqual(case["images"], ["img/portfolio/a.svg", "img/portfolio/b.svg"])
        self.assertEqual(case["cover"], "img/portfolio/a.svg")

        await content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/c.svg", set_as_cover=True)
        self.assertEqual((await self._case())["cover"], "img/portfolio/c.svg")

    async def test_remove_cover_image_reassigns_to_first_remaining(self):
        await content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        await content_store.remove_case_image(self.actor, "case_img_test", "img/portfolio/a.svg")
        case = await self._case()
        self.assertEqual(case["images"], ["img/portfolio/b.svg"])
        self.assertEqual(case["cover"], "img/portfolio/b.svg")

    async def test_remove_last_image_leaves_cover_none_not_broken(self):
        await content_store.remove_case_image(self.actor, "case_img_test", "img/portfolio/a.svg")
        case = await self._case()
        self.assertEqual(case["images"], [])
        self.assertIsNone(case["cover"])

    async def test_reorder_image_swaps_and_rejects_out_of_bounds(self):
        await content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        self.assertTrue(await content_store.reorder_case_image(self.actor, "case_img_test", "img/portfolio/b.svg", "up"))
        self.assertEqual((await self._case())["images"], ["img/portfolio/b.svg", "img/portfolio/a.svg"])
        self.assertFalse(await content_store.reorder_case_image(self.actor, "case_img_test", "img/portfolio/b.svg", "up"))

    async def test_set_cover_requires_image_to_already_be_in_gallery(self):
        self.assertFalse(await content_store.set_case_cover(self.actor, "case_img_test", "img/portfolio/not-there.svg"))
        self.assertEqual((await self._case())["cover"], "img/portfolio/a.svg")
        await content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        self.assertTrue(await content_store.set_case_cover(self.actor, "case_img_test", "img/portfolio/b.svg"))
        self.assertEqual((await self._case())["cover"], "img/portfolio/b.svg")


class CaseSectionManagementTests(unittest.IsolatedAsyncioTestCase):
    """Part 1 ТЗ: гибкие sections вместо жёстких task/solution/result."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"
        self.actor = "999"
        await content_store.add_case(
            self.actor, case_id="case_sec_test", title="Т", type_id="landing",
            cover="img/portfolio/a.svg", task="t", related_service=None,
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _case(self):
        return next(c for c in await content_store.list_cases() if c["id"] == "case_sec_test")

    async def test_add_gallery_section_stores_images_not_content(self):
        await content_store.add_case_section(
            self.actor, "case_sec_test", section_type="gallery", title="Скриншоты",
            images=["img/portfolio/x.svg"],
        )
        section = (await self._case())["sections"][0]
        self.assertEqual(section["images"], ["img/portfolio/x.svg"])
        self.assertNotIn("content", section)

    async def test_add_text_section_stores_content(self):
        await content_store.add_case_section(self.actor, "case_sec_test", section_type="text", title="Задача", content="Описание")
        section = (await self._case())["sections"][0]
        self.assertEqual(section["content"], "Описание")

    async def test_update_delete_reorder_sections(self):
        await content_store.add_case_section(self.actor, "case_sec_test", section_type="text", title="Первая", content="A")
        await content_store.add_case_section(self.actor, "case_sec_test", section_type="text", title="Вторая", content="B")

        await content_store.update_case_section(self.actor, "case_sec_test", 0, title="Первая (изменено)")
        self.assertEqual((await self._case())["sections"][0]["title"], "Первая (изменено)")

        self.assertTrue(await content_store.reorder_case_section(self.actor, "case_sec_test", 0, "down"))
        self.assertEqual([s["title"] for s in (await self._case())["sections"]], ["Вторая", "Первая (изменено)"])
        self.assertFalse(await content_store.reorder_case_section(self.actor, "case_sec_test", 0, "up"))

        self.assertTrue(await content_store.delete_case_section(self.actor, "case_sec_test", 0))
        self.assertEqual(len((await self._case())["sections"]), 1)
        self.assertFalse(await content_store.delete_case_section(self.actor, "case_sec_test", 5))


class LeadStoreTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_add_lead_without_draft_id_always_creates_a_new_lead(self):
        lead1 = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        lead2 = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram)
        self.assertNotEqual(lead1["id"], lead2["id"])
        self.assertEqual(len(await content_store.list_leads()), 2)

    async def test_add_lead_with_matching_draft_id_updates_instead_of_duplicating(self):
        first = await content_store.add_lead({"service_name": "Лендинг"}, self.telegram, draft_id="draft-abc")
        self.assertIsNone(first["updated_at"])

        second = await content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "доп. инфо"}, self.telegram, draft_id="draft-abc",
        )

        self.assertEqual(second["id"], first["id"], "\"Дополнить информацию\" не должно создавать вторую заявку")
        self.assertIsNotNone(second["updated_at"])
        self.assertEqual(len(await content_store.list_leads()), 1)
        self.assertEqual(second["payload"]["task_description"], "доп. инфо")

    async def test_add_lead_with_different_draft_id_creates_separate_lead(self):
        await content_store.add_lead({"service_name": "Лендинг"}, self.telegram, draft_id="draft-1")
        await content_store.add_lead({"service_name": "Сайт"}, self.telegram, draft_id="draft-2")
        self.assertEqual(len(await content_store.list_leads()), 2)

    async def test_list_leads_filters_by_status_and_sorts_newest_first(self):
        lead1 = await content_store.add_lead({"service_name": "A"}, self.telegram)
        lead2 = await content_store.add_lead({"service_name": "B"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead2["id"], "IN_PROGRESS")

        all_leads = await content_store.list_leads()
        self.assertEqual([l["id"] for l in all_leads], [lead2["id"], lead1["id"]])

        new_only = await content_store.list_leads("NEW")
        self.assertEqual([l["id"] for l in new_only], [lead1["id"]])

    async def test_list_leads_updated_lead_rises_above_newer_untouched_lead(self):
        # UX-аудит "Заявки как рабочая очередь": заявка, которую только что
        # обновили, должна подниматься наверх, даже если у неё меньший id,
        # чем у другой, нетронутой после создания заявки.
        old_lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        new_lead = await content_store.add_lead({"service_name": "B"}, self.telegram)
        await content_store.update_lead_status(self.actor, old_lead["id"], "IN_PROGRESS")

        # Форсируем заведомо более позднюю метку — как и в аналогичном
        # тесте для list_leads_by_user() (MyLeadsFilteringTests), два
        # datetime.now(timezone.utc) подряд иногда совпадают по разрешению
        # системных часов, тест иначе был бы flaky.
        leads = await content_store._read_leads()
        for l in leads:
            if l["id"] == old_lead["id"]:
                l["updated_at"] = "2030-01-01T00:00:00+00:00"
        await content_store._write_leads(leads)

        result = await content_store.list_leads()
        self.assertEqual([l["id"] for l in result], [old_lead["id"], new_lead["id"]])

    async def test_list_leads_same_updated_at_tiebreaks_on_higher_id(self):
        first = await content_store.add_lead({"service_name": "A"}, self.telegram)
        second = await content_store.add_lead({"service_name": "B"}, self.telegram)
        leads = await content_store._read_leads()
        same_ts = "2026-01-01T00:00:00+00:00"
        for l in leads:
            if l["id"] in (first["id"], second["id"]):
                l["updated_at"] = same_ts
        await content_store._write_leads(leads)

        result = await content_store.list_leads()
        self.assertEqual([l["id"] for l in result], [second["id"], first["id"]])

    async def test_list_leads_active_filter_excludes_done_and_cancelled(self):
        leads_by_status = {}
        for status in content_store.LEAD_STATUSES:
            lead = await content_store.add_lead({"service_name": status}, self.telegram)
            if status != "NEW":
                await content_store.update_lead_status(self.actor, lead["id"], status)
            leads_by_status[status] = lead["id"]

        active_ids = {l["id"] for l in await content_store.list_leads("ACTIVE")}
        self.assertEqual(
            active_ids,
            {leads_by_status[s] for s in ("NEW", "VIEWED", "IN_PROGRESS", "WAITING_CLIENT")},
        )
        self.assertNotIn(leads_by_status["DONE"], active_ids)
        self.assertNotIn(leads_by_status["CANCELLED"], active_ids)

    async def test_list_leads_explicit_status_filters_still_work(self):
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "DONE")

        self.assertEqual([l["id"] for l in await content_store.list_leads("DONE")], [lead["id"]])
        self.assertEqual(await content_store.list_leads("CANCELLED"), [])
        self.assertEqual([l["id"] for l in await content_store.list_leads("ALL")], [lead["id"]])

    async def test_active_is_not_a_real_lead_status(self):
        # "ACTIVE" — техническое значение только для list_leads(), не
        # персистентный business-статус (см. content_store.ACTIVE_LEAD_STATUSES).
        self.assertNotIn("ACTIVE", content_store.LEAD_STATUSES)
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        self.assertFalse(await content_store.update_lead_status(self.actor, lead["id"], "ACTIVE"))
        self.assertEqual((await content_store.get_lead(lead["id"]))["status"], "NEW")  # не изменился

    async def test_update_lead_status_rejects_unknown_status_and_requires_designer(self):
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        self.assertFalse(await content_store.update_lead_status(self.actor, lead["id"], "BOGUS"))
        with self.assertRaises(content_store.NotDesignerError):
            await content_store.update_lead_status("not-the-designer", lead["id"], "DONE")

    async def test_get_lead_returns_none_for_unknown_id(self):
        self.assertIsNone(await content_store.get_lead(999999))


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


class MyLeadsFilteringTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_user_a_does_not_see_user_b_leads(self):
        lead_a = await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 111, "username": "a"})
        lead_b = await content_store.add_lead({"service_name": "Сайт"}, {"user_id": 222, "username": "b"})

        leads_for_a = await content_store.list_leads_by_user(111)
        leads_for_b = await content_store.list_leads_by_user(222)

        self.assertEqual([l["id"] for l in leads_for_a], [lead_a["id"]])
        self.assertEqual([l["id"] for l in leads_for_b], [lead_b["id"]])
        self.assertNotIn(lead_b["id"], [l["id"] for l in leads_for_a])

    async def test_unknown_user_gets_empty_list_not_error(self):
        await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 111, "username": "a"})
        self.assertEqual(await content_store.list_leads_by_user(999999), [])

    async def test_leads_sorted_newest_first(self):
        # Обе заявки ни разу не обновлялись (updated_at=None у обеих) —
        # сортировка падает на created_at, порядок по факту создания.
        first = await content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        second = await content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        leads = await content_store.list_leads_by_user(111)
        self.assertEqual([l["id"] for l in leads], [second["id"], first["id"]])

    async def test_updated_old_lead_rises_above_newer_untouched_lead(self):
        # См. UX-аудит "Мои заявки" — недавняя активность (статус/supplement/
        # owner_message/материал) должна поднимать заявку наверх, даже если
        # она создана раньше другой, нетронутой заявки.
        old_lead = await content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        new_lead = await content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        await content_store.update_lead_status(self.actor, old_lead["id"], "IN_PROGRESS")

        # update_lead_status() уже реально отработал (сам факт простановки
        # updated_at проверяется отдельно, в других тестах) — но два
        # datetime.now(timezone.utc) вызова подряд в одном тесте иногда
        # совпадают до используемого разрешения системных часов, из-за чего
        # updated_at старой заявки может оказаться РАВЕН created_at новой —
        # тогда тай-брейк по id (осознанно реализованный) отдаёт победу
        # новой заявке, и тест стал бы flaky. Форсируем заведомо более
        # позднюю метку, чтобы порядок проверялся детерминированно.
        leads = await content_store._read_leads()
        for l in leads:
            if l["id"] == old_lead["id"]:
                l["updated_at"] = "2030-01-01T00:00:00+00:00"
        await content_store._write_leads(leads)

        leads = await content_store.list_leads_by_user(111)
        self.assertEqual([l["id"] for l in leads], [old_lead["id"], new_lead["id"]])

    async def test_same_updated_at_tiebreaks_on_higher_id(self):
        first = await content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        second = await content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        # Форсируем одинаковый updated_at у обеих — на быстром хранилище
        # (Upstash) реальное совпадение до секунды вполне возможно, это не
        # искусственный случай.
        leads = await content_store._read_leads()
        same_ts = "2026-01-01T00:00:00+00:00"
        for l in leads:
            if l["id"] in (first["id"], second["id"]):
                l["updated_at"] = same_ts
        await content_store._write_leads(leads)

        result = await content_store.list_leads_by_user(111)
        self.assertEqual([l["id"] for l in result], [second["id"], first["id"]])

    async def test_sorting_does_not_change_lead_fields(self):
        lead = await content_store.add_lead({"service_name": "Лендинг", "task_description": "Тест"}, {"user_id": 111})
        await content_store.update_lead_status(self.actor, lead["id"], "DONE")

        result = await content_store.list_leads_by_user(111)
        self.assertEqual(result[0]["payload"]["service_name"], "Лендинг")
        self.assertEqual(result[0]["payload"]["task_description"], "Тест")
        self.assertEqual(result[0]["status"], "DONE")

    async def test_admin_list_leads_now_also_sorts_by_updated_at(self):
        # Ранее list_leads() (для /admin) был отсортирован строго по id —
        # с UX-аудита "Заявки как рабочая очередь" (P2 продуктовый блок)
        # он использует тот же принцип, что и list_leads_by_user() ниже:
        # недавняя активность поднимает заявку наверх, даже если она
        # создана раньше другой, нетронутой заявки.
        old_lead = await content_store.add_lead({"service_name": "A"}, {"user_id": 111})
        new_lead = await content_store.add_lead({"service_name": "B"}, {"user_id": 111})
        await content_store.update_lead_status(self.actor, old_lead["id"], "IN_PROGRESS")

        admin_leads = await content_store.list_leads()
        self.assertEqual([l["id"] for l in admin_leads], [old_lead["id"], new_lead["id"]])


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

        await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 555, "username": "me"})
        await content_store.add_lead({"service_name": "Сайт"}, {"user_id": 666, "username": "other"})

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

        await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 42, "username": "me"})
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

        await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 42, "username": "me"})
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

        leads = await content_store.list_leads_by_user(42)
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
        self.assertEqual(await content_store.list_leads(), [])

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
        self.assertEqual(await content_store.list_leads(), [])

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

        self.assertEqual(await content_store.list_leads_by_user(999999), [])
        leads_for_real_user = await content_store.list_leads_by_user(42)
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
        leads = await content_store.list_leads_by_user(42)
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

        lead = (await content_store.list_leads_by_user(42))[0]
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

        lead = await content_store.find_lead_awaiting_file(42)
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
        self.assertEqual(len(await content_store.list_leads_by_user(42)), 1)
        fake_bot.send_message.assert_awaited_once()

    async def test_different_user_same_draft_id_is_rejected(self):
        # См. production-аудит, P1-2: draft_id — обычная строка из
        # localStorage клиента, ничем не подписана. Совпадение (или подбор)
        # чужого draft_id не должно перезаписывать чужую заявку.
        from aiohttp.test_utils import TestClient, TestServer

        fake_bot = AsyncMock()
        app = webserver.create_app(fake_bot)
        async with TestClient(TestServer(app)) as client:
            first = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=42), "Content-Type": "application/json"},
                json={"service_name": "Лендинг", "task_description": "заявка user 42", "draft_id": "collide-1"},
            )
            self.assertEqual(first.status, 200)
            lead_id = (await first.json())["lead_id"]

            second = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=999), "Content-Type": "application/json"},
                json={"service_name": "Брендинг", "task_description": "попытка user 999", "draft_id": "collide-1"},
            )
            self.assertEqual(second.status, 403)
            self.assertEqual(await second.json(), {"error": "forbidden"})

        # Исходная заявка не изменилась ни в payload, ни в identity.
        lead = await content_store.get_lead(lead_id)
        self.assertEqual(lead["payload"]["task_description"], "заявка user 42")
        self.assertEqual(lead["telegram"]["user_id"], 42)
        # user 999 не получил чужую заявку в своём списке, и новой заявки
        # под тем же draft_id не появилось.
        self.assertEqual(await content_store.list_leads_by_user(999), [])
        self.assertEqual(len(await content_store.list_leads_by_user(42)), 1)
        # Уведомление владельцу ушло РОВНО один раз (за первую, настоящую
        # заявку) — отклонённая коллизия не притворилась новой заявкой.
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

    async def test_supplement_to_someone_elses_lead_is_404(self):
        # См. production-аудит, P2-11: чужой lead_id больше не отличим
        # снаружи от несуществующего (раньше был 403 vs 404 у
        # несуществующего — перебором lead_id можно было узнать, какие ID
        # вообще существуют у других клиентов). Внутри content_store
        # NotLeadOwnerError/LeadNotFoundError остались раздельными типами —
        # унифицирован только HTTP-ответ, см. webserver.py::_handle_lead_supplement.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client, user_id=42)
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=999), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "чужое дополнение"}},
            )
            self.assertEqual(resp.status, 404)
            body = await resp.json()
            self.assertEqual(body, {"error": "not_found"})

        lead = await content_store.get_lead(lead_id)
        self.assertEqual(lead.get("supplements", []), [])  # чужая попытка ничего не добавила

    async def test_wrong_owner_and_nonexistent_lead_responses_are_identical(self):
        # Прямое доказательство неразличимости "не мой lead" vs "нет такого
        # lead" — не просто "оба дают 404 по отдельности", а буквально
        # идентичный status+body для обоих случаев.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client, user_id=42)

            wrong_owner_resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=999), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "x"}},
            )
            nonexistent_resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(user_id=999), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": 999999, "fields": {"comment": "x"}},
            )

            self.assertEqual(wrong_owner_resp.status, nonexistent_resp.status)
            self.assertEqual(await wrong_owner_resp.json(), await nonexistent_resp.json())

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

        lead = await content_store.get_lead(lead_id)
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

        lead = await content_store.get_lead(lead_id)
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


class PublicDataRouteTests(unittest.IsolatedAsyncioTestCase):
    """См. production-аудит, P0-1: /data/ раньше отдавал ВСЮ директорию
    (add_static), включая leads.json — реальные контакты клиентов при
    выключенном Upstash. Теперь /data/{filename} — explicit whitelist
    (PUBLIC_DATA_FILES), не blacklist: файл публичен, только если явно в
    списке; всё остальное — обычный 404, как будто файла не существует."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        # leads.json — намеренно с реалистичным непустым содержимым, чтобы
        # тест реально доказывал "не отдаётся", а не "файла просто нет".
        (Path(self.tmpdir) / "leads.json").write_text(
            json.dumps({"leads": [{"id": 1, "telegram": {"user_id": 42}, "payload": {"contact": "+7 900 000-00-00"}}]}),
            encoding="utf-8",
        )
        # webserver.py больше не хранит собственный DATA_DIR — handle_public_data
        # читает через content_store.read_async, единственный source of truth
        # для пути к данным теперь только content_store.DATA_DIR.
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_leads_json_is_not_publicly_reachable(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/data/leads.json")
            self.assertEqual(resp.status, 404)
            body = await resp.text()
            self.assertNotIn("+7 900", body)  # содержимое реально не утекло в тело ответа

    async def test_arbitrary_filename_under_data_is_404_not_500(self):
        # Whitelist, не только исключение leads.json — любое незнакомое имя
        # (в т.ч. гипотетический будущий файл) тоже не публикуется.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            for name in ("faq.json", "secrets.json", "..%2fconfig.py", "leads.json.bak"):
                resp = await client.get(f"/data/{name}")
                self.assertEqual(resp.status, 404, f"{name} should not be publicly served")

    async def test_public_config_files_still_load(self):
        # Mini App init() делает fetch() ровно этих четырёх файлов
        # (webapp/js/app.js) — они обязаны продолжать работать без изменений.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            for name in ("pricing.json", "portfolio.json", "about.json", "ui_config.json"):
                resp = await client.get(f"/data/{name}")
                self.assertEqual(resp.status, 200, f"{name} should still be publicly served")
                body = await resp.json()
                self.assertIsInstance(body, dict)

    async def test_cache_control_is_no_store_for_public_data(self):
        # _no_cache middleware (webserver.py) применяется по префиксу пути
        # "/data/" безусловно, независимо от того, что именно отдаёт хендлер
        # — эта проверка не менялась в этой фазе, но фиксирует текущую
        # модель явно (см. отчёт, раздел G — freshness bug был НЕ про
        # браузерный кэш, Cache-Control тут уже был максимально строгим).
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/data/pricing.json")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store, no-cache, must-revalidate")


class PublicDataRouteUpstashTests(unittest.IsolatedAsyncioTestCase):
    """См. production-freshness-аудит: /data/{filename} раньше отдавал файл
    напрямую с локального диска (web.FileResponse), а /admin в Upstash-
    режиме пишет ТОЛЬКО в Redis (см. content_store._write) — Mini App
    показывал устаревший deploy-time снапшот после любой правки в /admin.
    handle_public_data теперь читает через content_store.read_async — тот
    же backend, что и /admin. См. PublicDataRouteTests выше для
    local-режима (не изменился этой фазой)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        content_store.DATA_DIR = Path(self.tmpdir)

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
        content_store.DATA_DIR = self._orig_data_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_route_returns_upstash_version_not_stale_local_seed(self):
        # Локальный диск содержит СТАРУЮ версию (реальный сид-файл из data/),
        # Upstash — НОВУЮ (как будто дизайнер только что отредактировал
        # через /admin). До фикса маршрут отдал бы локальную (старую) версию
        # — именно это и было production freshness bug.
        self.fake.store["pricing.json"] = json.dumps(
            {"services": [], "options": [], "coefficients": {}, "rounding": {}, "_marker": "NEW_FROM_ADMIN"}
        )
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/data/pricing.json")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertEqual(body.get("_marker"), "NEW_FROM_ADMIN")

        # локальный файл не тронут и по-прежнему БЕЗ маркера — доказывает,
        # что источником ответа был именно Upstash, а не локальный диск.
        with open(Path(self.tmpdir) / "pricing.json", encoding="utf-8") as f:
            local_content = json.load(f)
        self.assertNotIn("_marker", local_content)

    async def test_leads_json_still_404_in_upstash_mode(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/data/leads.json")
            self.assertEqual(resp.status, 404)

    async def test_all_four_public_files_served_from_upstash(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            for name in ("pricing.json", "portfolio.json", "about.json", "ui_config.json"):
                resp = await client.get(f"/data/{name}")
                self.assertEqual(resp.status, 200, f"{name} should be served (seeded from local on first read)")
                body = await resp.json()
                self.assertIsInstance(body, dict)
                # первое чтение засеивает Upstash из локального файла (см.
                # content_store._read) — второй запрос должен вернуть то же
                # самое без повторного локального чтения.
                self.assertIn(name, self.fake.store)


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
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]

        message.document = make_fake_document(file_id="doc-1", file_unique_id="uniq-1")
        await webapp.handle_tz_file(message)

        lead = await content_store.get_lead(lead_id)
        self.assertEqual(len(lead["materials"]), 1)
        self.assertEqual(lead["materials"][0]["file_id"], "doc-1")
        self.assertEqual(lead["materials"][0]["file_unique_id"], "uniq-1")
        self.assertEqual(lead["materials"][0]["kind"], "document")
        self.assertEqual(lead["materials"][0]["source"], "new")

    async def test_awaiting_state_cleared_after_material_received(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m2"})
        self.assertIsNotNone(await content_store.find_lead_awaiting_file(message.from_user.id))

        message.document = make_fake_document()
        await webapp.handle_tz_file(message)
        self.assertIsNone(await content_store.find_lead_awaiting_file(message.from_user.id))

    async def test_multiple_leads_same_user_do_not_cause_misattribution(self):
        # Два лида одного клиента, оба помечены attach_tz=True — второй
        # (более новый) должен снять ожидание с первого (см.
        # content_store._clear_other_awaiting/аудит), иначе find_lead_awaiting_file
        # была бы неоднозначной и файл мог уйти не в ту заявку.
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m3-a"})
        first_lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]

        await webapp._handle_brief_submission(message, {"service_name": "Логотип", "attach_tz": True, "draft_id": "m3-b"})
        second_lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]

        self.assertNotEqual(first_lead_id, second_lead_id)
        first_lead = await content_store.get_lead(first_lead_id)
        self.assertFalse(first_lead["awaiting_tz_file"])  # снято вторым запросом

        message.document = make_fake_document()
        await webapp.handle_tz_file(message)

        # Файл должен уйти именно во вторую (единственно ожидающую) заявку.
        self.assertEqual(len((await content_store.get_lead(second_lead_id))["materials"]), 1)
        self.assertEqual((await content_store.get_lead(first_lead_id)).get("materials", []), [])

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

        lead = await content_store.find_lead_awaiting_file(555)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["id"], lead_id)
        self.assertEqual(lead["awaiting_tz_file_source"], "supplement")


def make_reply_message(chat_id: int, text: str, send_message: AsyncMock) -> SimpleNamespace:
    """Достаточно для lead_reply_send: message.text, message.chat.id (actor
    для _require_designer), message.bot.send_message (контролируемый —
    успех/исключение задаёт сам тест), message.answer. delete/
    bot.delete_message/bot.edit_message_text (P1-3, Batch 3) — success-ветка
    теперь вызывает flow.step_from_text; без seeded-anchor в этих тестах
    она падает на message.answer() тем же способом, что и раньше."""
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=chat_id),
        delete=AsyncMock(),
        bot=SimpleNamespace(send_message=send_message, delete_message=AsyncMock(), edit_message_text=AsyncMock()),
        answer=AsyncMock(),
    )


class OwnerMessageTests(unittest.IsolatedAsyncioTestCase):
    """owner_messages[] — append-only ответы дизайнера клиенту, тот же
    паттерн, что и supplements[]/materials[] (см. аудит): отдельный поток,
    не трогает payload, не теряется при неудачной Telegram-доставке."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "888"
        self.actor = 888
        self.lead = await content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Исходная задача"},
            {"user_id": 55555, "username": "client", "first_name": "Клиент"},
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---- content_store.add_owner_message ----

    async def test_new_owner_message_is_saved(self):
        lead = await content_store.add_owner_message(self.actor, self.lead["id"], "Первый ответ", "sent")
        self.assertEqual(len(lead["owner_messages"]), 1)
        self.assertEqual(lead["owner_messages"][0]["text"], "Первый ответ")
        self.assertEqual(lead["owner_messages"][0]["delivery_status"], "sent")
        self.assertEqual(lead["owner_messages"][0]["id"], 1)

    async def test_two_messages_are_both_kept_append_only(self):
        await content_store.add_owner_message(self.actor, self.lead["id"], "Первый", "sent")
        lead = await content_store.add_owner_message(self.actor, self.lead["id"], "Второй", "sent")
        self.assertEqual(len(lead["owner_messages"]), 2)
        self.assertEqual(lead["owner_messages"][0]["text"], "Первый")
        self.assertEqual(lead["owner_messages"][1]["text"], "Второй")
        self.assertEqual(lead["owner_messages"][0]["id"], 1)
        self.assertEqual(lead["owner_messages"][1]["id"], 2)

    async def test_owner_message_does_not_change_payload(self):
        lead = await content_store.add_owner_message(self.actor, self.lead["id"], "Ответ", "sent")
        self.assertEqual(lead["payload"]["task_description"], "Исходная задача")

    async def test_owner_message_updates_updated_at(self):
        self.assertIsNone(self.lead["updated_at"])
        lead = await content_store.add_owner_message(self.actor, self.lead["id"], "Ответ", "sent")
        self.assertIsNotNone(lead["updated_at"])

    async def test_owner_message_requires_designer(self):
        with self.assertRaises(content_store.NotDesignerError):
            await content_store.add_owner_message("not-the-designer", self.lead["id"], "Ответ", "sent")

    async def test_owner_message_for_unknown_lead_returns_none(self):
        self.assertIsNone(await content_store.add_owner_message(self.actor, 999999, "Ответ", "sent"))

    # ---- bot/handlers/admin.py::lead_reply_send (реальный хендлер) ----

    async def test_lead_reply_send_success_sets_delivery_status_sent(self):
        state = make_state(self.actor)
        await state.update_data(lead_id=self.lead["id"])
        await state.set_state(AdminStates.lead_reply_text)

        message = make_reply_message(self.actor, "Всё уточнили, приступаем", AsyncMock())
        await admin.lead_reply_send(message, state)

        lead = await content_store.get_lead(self.lead["id"])
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

        lead = await content_store.get_lead(self.lead["id"])
        self.assertEqual(len(lead["owner_messages"]), 1)  # не потерялось
        self.assertEqual(lead["owner_messages"][0]["delivery_status"], "failed")
        self.assertEqual(lead["owner_messages"][0]["text"], "Ответ, который не дойдёт")
        # Владельцу — понятная ошибка, не тихий сбой.
        admin_text = message.answer.await_args.args[0]
        self.assertIn("Не получилось отправить", admin_text)

    async def test_admin_detail_shows_owner_messages(self):
        await content_store.add_owner_message(self.actor, self.lead["id"], "Первый ответ", "sent")
        await content_store.add_owner_message(self.actor, self.lead["id"], "Второй, не дошёл", "failed")
        lead = await content_store.get_lead(self.lead["id"])

        text = lead_format.format_lead_admin_detail(lead)
        self.assertIn("Ответы дизайнера", text)
        self.assertIn("Первый ответ", text)
        self.assertIn("Второй, не дошёл", text)
        self.assertIn("не доставлено", text)

    # ---- /api/my-leads (HTTP) ----

    async def test_my_leads_returns_owner_messages(self):
        from aiohttp.test_utils import TestClient, TestServer

        await content_store.add_owner_message(self.actor, self.lead["id"], "Виден клиенту", "sent")
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

    async def _make_lead(self, user_id=55555):
        return await content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Тест"},
            {"user_id": user_id, "username": "client", "first_name": "Клиент"},
        )

    async def test_status_change_sends_one_notification(self):
        lead = await self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        callback.bot.send_message.assert_awaited_once()
        self.assertEqual(callback.bot.send_message.await_args.kwargs["chat_id"], 55555)
        self.assertEqual((await content_store.get_lead(lead["id"]))["status"], "IN_PROGRESS")

    async def test_status_change_notification_has_service_name_and_label(self):
        # lead.id — глобальный сквозной счётчик по ВСЕМ заявкам от ВСЕХ
        # клиентов (см. content_store.add_lead), клиенту его не показываем
        # (см. UX-аудит) — вместо номера используется service_name.
        lead = await self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:WAITING_CLIENT", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Ваша заявка обновлена", text)
        self.assertIn("Лендинг", text)  # service_name из _make_lead()
        self.assertIn("Нужно ваше действие", text)

    async def test_status_change_notification_does_not_contain_lead_id(self):
        lead = await self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:DONE", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertNotIn(f"#{lead['id']}", text)
        self.assertNotIn(str(lead["id"]), text)

    async def test_status_change_notification_falls_back_when_service_name_missing(self):
        lead = await content_store.add_lead(
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
        lead = await self._make_lead()  # уже "NEW" по умолчанию
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:NEW", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        callback.bot.send_message.assert_not_awaited()
        self.assertEqual((await content_store.get_lead(lead["id"]))["status"], "NEW")

    async def test_missing_user_id_changes_status_but_sends_zero_notifications(self):
        lead = await content_store.add_lead(
            {"service_name": "Лендинг"}, {"user_id": None, "username": None, "first_name": None},
        )
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        callback.bot.send_message.assert_not_awaited()
        self.assertEqual((await content_store.get_lead(lead["id"]))["status"], "IN_PROGRESS")

    async def test_send_message_exception_leaves_status_changed(self):
        lead = await self._make_lead()
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        failing_bot = AsyncMock()
        failing_bot.send_message.side_effect = TelegramAPIError(method=None, message="bot was blocked")
        callback = make_callback("adminleadstatus:DONE", chat_id=self.actor, bot=failing_bot)

        await admin.lead_change_status(callback, state)  # не должно бросить исключение наружу

        self.assertEqual((await content_store.get_lead(lead["id"]))["status"], "DONE")

    async def test_status_change_does_not_touch_owner_messages(self):
        lead = await self._make_lead()
        await content_store.add_owner_message(self.actor, lead["id"], "Ранее написанный ответ", "sent")
        state = make_state(self.actor)
        await state.update_data(lead_id=lead["id"])
        callback = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor)

        await admin.lead_change_status(callback, state)

        lead_after = await content_store.get_lead(lead["id"])
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
