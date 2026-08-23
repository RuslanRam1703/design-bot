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
import hashlib
import hmac
import io
import json
import shutil
import tempfile
import time
import unittest
import urllib.error
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
import bot.behance as behance
import bot.r2_storage as r2_storage
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


def make_message(
    document=None, photo=None, video=None, animation=None, voice=None, video_note=None, sticker=None, chat_id=1,
) -> SimpleNamespace:
    """Достаточно для _handle_brief_submission/handle_tz_file: message.from_user,
    message.bot (async send_message), message.answer (async) — сам объект не
    обязан быть настоящим aiogram Message, потому что типовые аннотации в
    рантайме не проверяются. first_name/last_name присутствуют, т.к. реальный
    aiogram User их всегда отдаёт (first_name обязателен по Telegram Bot API,
    last_name — опционален, но атрибут есть всегда, просто может быть None).
    document/photo/video/animation/voice/video_note/sticker по умолчанию None —
    как у настоящего aiogram Message, когда в сообщении нет соответствующего
    вложения (Stage B: video/animation добавлены тем же паттерном; Stage C
    Batch 1: voice/video_note/sticker — тем же). chat.id == from_user.id по
    умолчанию — тот же принцип, что и в make_flow_message (в приватном чате
    с ботом они всегда совпадают)."""
    return SimpleNamespace(
        from_user=SimpleNamespace(id=1, username="client", first_name="Клиент", last_name=None),
        chat=SimpleNamespace(id=chat_id),
        text=None,
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
        forward=AsyncMock(),
        document=document,
        photo=photo,
        video=video,
        animation=animation,
        voice=voice,
        video_note=video_note,
        sticker=sticker,
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

    async def test_cmd_id_reports_correct_chat_id(self):
        # Batch 14: /faq/portfolio/about/brief уже покрыты выше (в т.ч.
        # cmd_brief — через переменную handler в цикле, не буквальным
        # вызовом cmd_brief(...), из-за чего был ошибочно посчитан
        # непокрытым при аудите), но /id не был покрыт нигде.
        msg = make_flow_message(chat_id=424242, text="/id")
        await start.cmd_id(msg)
        msg.answer.assert_awaited_once_with(texts.MY_ID_TEMPLATE.format(chat_id=424242), parse_mode="Markdown")

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
            # Текст промпта расширен вместе с Behance-интеграцией: на этом же
            # шаге теперь принимается и ссылка на проект Behance (см.
            # admin.cases_add_photo_behance). Проверяемое здесь поведение —
            # RULE 3 / anchor, а не сама формулировка.
            "Пришлите фото кейса (как фото) — или ссылку на проект Behance:",
            chat_id=self.actor, message_id=500, reply_markup=kb.cancel_keyboard()
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
        # content_store.add_case вызывается только на последнем шаге
        # (cases_add_description) — отмена ДО него структурно не может
        # создать частично записанный кейс (P1-3, Batch 5, item 15).
        self.assertFalse(any(c["title"] == "Новый лендинг" for c in await content_store.list_cases()))

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

    # ---- backup/import validation: backup_import_receive + backup_restore_do
    # (BadZipFile), backup_import_wrong ----
    # P1-3, Batch 10: restore теперь требует подтверждения (см.
    # backup_restore_do) — receive только сохраняет байты и показывает
    # confirm; BadZipFile обнаруживается уже на "Да", тем же self-healing
    # raw edit_text (RULE 3, тот же message_id, без reset между шагами).
    async def test_backup_import_bad_zip_retry_deletes_current_prompt_on_cancel(self):
        state = await self._state_with_anchor(anchor_id=1200, cancel_to="backup")
        bad = make_flow_message_factory(chat_id=self.actor, start_id=8500)()
        bad.document = SimpleNamespace(file_id="fake_zip_id")
        bad.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="documents/backup.zip"))
        bad.bot.download_file = AsyncMock(return_value=io.BytesIO(b"not a zip"))

        await admin.backup_import_receive(bad, state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_confirm.state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 1200)

        confirm_cb = make_callback("adminbackuprestoreconfirm:yes", chat_id=self.actor, message_id=1200)
        await admin.backup_restore_do(confirm_cb, state)
        confirm_cb.message.edit_text.assert_awaited_once_with(
            "Файл повреждён или не .zip — пришлите другой файл.", reply_markup=kb.cancel_keyboard()
        )
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_wait_file.state)

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

    # P1-3, Batch 10: backup_import_receive больше не вызывает
    # import_backup_bytes напрямую — теперь только сохраняет байты и
    # показывает confirm-экран (см. backup_restore_do). Каждый тест ниже
    # сначала проходит через backup_import_receive (проверяя anchor на
    # confirm-шаге), затем эмулирует "Да" через backup_restore_do — тот же
    # message_id, тот же self-healing raw edit_text (RULE 3, без
    # промежуточного reset), что и у остальных confirm_do в этом файле.
    async def _confirm_backup_restore(self, anchor_id: int, state: FSMContext) -> SimpleNamespace:
        cb = make_callback("adminbackuprestoreconfirm:yes", chat_id=self.actor, message_id=anchor_id)
        await admin.backup_restore_do(cb, state)
        return cb

    async def test_backup_import_validation_error_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=680, cancel_to="backup")
        msg = self._backup_message(680)
        await admin.backup_import_receive(msg, state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_confirm.state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 680)

        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupValidationError("pricing.json", "not valid JSON", ["pricing.json"])),
        ):
            cb = await self._confirm_backup_restore(680, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_backup_import_snapshot_error_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=690, cancel_to="backup")
        msg = self._backup_message(690)
        await admin.backup_import_receive(msg, state)

        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupSnapshotError("pricing.json")),
        ):
            cb = await self._confirm_backup_restore(690, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_backup_import_restore_failed_rollback_failed_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=700, cancel_to="backup")
        msg = self._backup_message(700)
        await admin.backup_import_receive(msg, state)

        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupRestoreFailedError("pricing.json", ["faq.json"], ["pricing.json"])),
        ):
            cb = await self._confirm_backup_restore(700, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertIn("КРИТИЧНО", cb.message.edit_text.await_args.args[0])
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_backup_import_restore_failed_rollback_ok_keeps_anchor_synced(self):
        state = await self._state_with_anchor(anchor_id=710, cancel_to="backup")
        msg = self._backup_message(710)
        await admin.backup_import_receive(msg, state)

        with patch(
            "bot.content_store.import_backup_bytes",
            new=AsyncMock(side_effect=content_store.BackupRestoreFailedError("pricing.json", ["faq.json"], [])),
        ):
            cb = await self._confirm_backup_restore(710, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertIn("Исходное состояние данных восстановлено", cb.message.edit_text.await_args.args[0])
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_backup_import_success_deletes_current_prompt_on_cancel(self):
        state = await self._state_with_anchor(anchor_id=720, cancel_to="backup")
        msg = self._backup_message(720)
        await admin.backup_import_receive(msg, state)

        fake_result = SimpleNamespace(restored_json=["pricing.json"], missing_json=[], restored_images=[], failed_images=[])
        with patch("bot.content_store.import_backup_bytes", new=AsyncMock(return_value=fake_result)):
            cb = await self._confirm_backup_restore(720, state)
        cb.message.edit_text.assert_awaited_once()
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


class AdminNavigationMenuWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 9: full functional audit of Admin -> Меню и навигация
    (menu_nav/nav_toggle). Unlike every other admin domain audited in
    Batches 5-8, this one is a single flat, stateless toggle screen -- no
    wizard, no free-text step, no FSM sub-states, no destructive action
    (a toggle is trivially reversible, the same reason case-image
    reorder/set-cover never needed a Batch 6-style confirm step either).

    Found no code bug: nav_toggle already defaults to True via
    ui_config["menu"].get(key, True) and content_store.set_menu_item_enabled
    is already bool-safe/no-raise on an unknown key, so a stale/malformed
    callback_data is already a safe no-op, not a crash. menu_nav is
    already covered by AdminSectionNavigationAnchorTests (one of the 9
    adminmenu:* entry points). The gap was regression coverage: nav_toggle
    itself -- the only meaningful ACTION in this screen -- had zero test
    coverage anywhere in the suite.

    Deliberately NOT fixed here (documented, not silently ignored): the
    "faq" menu-visibility flag has no enforcement point anywhere in the
    client-facing product. webapp/js/app.js's tab bar (TAB_SCREENS) never
    included "faq" to begin with (FAQ is a separate, non-Mini-App inline-
    keyboard flow -- see EntryPointArchitectureTests), and bot/handlers/
    start.py's /faq command and reply-keyboard button never check
    ui_config at all. Toggling "faq" off in this admin screen is
    therefore silently inert. This is a real cross-domain gap, but fixing
    it requires deciding what a disabled /faq command should actually do
    for a non-Mini-App entry point (silently ignore? show a stub
    message?) -- a product decision, not something safely inferable here,
    and FAQ's command/keyboard code is a separately-audited, protected
    domain (Batch 2). Left for a future batch. "calculator" has a
    narrower but real effect (the Mini App's own /calculator deep-link
    fallback only, since no bot command/button exposes it since it was
    folded into the brief flow) -- not a bug, matches the product's own
    documented evolution."""

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

    async def _menu(self):
        return (await content_store.get_ui_config())["menu"]

    async def test_nav_toggle_disables_enabled_item(self):
        self.assertTrue((await self._menu())["portfolio"])  # seed default
        cb = make_callback("adminnavtoggle:portfolio", chat_id=self.actor, message_id=500)
        await admin.nav_toggle(cb, state=make_state(self.actor))

        self.assertFalse((await self._menu())["portfolio"])
        shown_markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
        texts = [btn.text for row in shown_markup.inline_keyboard for btn in row]
        self.assertTrue(any(t.startswith("⬜") and "Портфолио" in t for t in texts))

    async def test_nav_toggle_reenables_disabled_item(self):
        await content_store.set_menu_item_enabled(self.actor, "about", False)
        cb = make_callback("adminnavtoggle:about", chat_id=self.actor, message_id=510)
        await admin.nav_toggle(cb, state=make_state(self.actor))

        self.assertTrue((await self._menu())["about"])
        shown_markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
        texts = [btn.text for row in shown_markup.inline_keyboard for btn in row]
        self.assertTrue(any(t.startswith("✅") and "Обо мне" in t for t in texts))

    async def test_nav_toggle_is_idempotent_round_trip(self):
        state = make_state(self.actor)
        before = dict(await self._menu())
        await admin.nav_toggle(make_callback("adminnavtoggle:calculator", chat_id=self.actor, message_id=520), state)
        await admin.nav_toggle(make_callback("adminnavtoggle:calculator", chat_id=self.actor, message_id=520), state)
        self.assertEqual(await self._menu(), before)  # ровно вернулись к исходному состоянию

    async def test_nav_toggle_unknown_key_does_not_crash(self):
        cb = make_callback("adminnavtoggle:not_a_real_menu_item", chat_id=self.actor, message_id=530)
        before = dict(await self._menu())

        await admin.nav_toggle(cb, state=make_state(self.actor))  # не должно бросить исключение

        self.assertEqual(await self._menu(), before)  # реальные пункты меню не задеты
        cb.message.edit_text.assert_awaited_once()  # экран всё равно корректно перерисован
        cb.answer.assert_awaited_once()

    async def test_nav_toggle_persists_across_reentry(self):
        state = make_state(self.actor)
        await admin.nav_toggle(make_callback("adminnavtoggle:brief", chat_id=self.actor, message_id=540), state)

        # выходим и заново заходим в раздел -- как отдельная сессия/повторный клик
        reentry_state = await self._state_with_nav()
        cb = make_callback("adminmenu:nav", chat_id=self.actor, message_id=550)
        await admin.menu_nav(cb, reentry_state)

        shown_markup = cb.message.edit_text.await_args.kwargs["reply_markup"]
        texts = [btn.text for row in shown_markup.inline_keyboard for btn in row]
        self.assertTrue(any(t.startswith("⬜") and "Заявка" in t for t in texts))

    async def _state_with_nav(self, nav_msg_id: int = 111) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id, flow._NAV_ANCHOR_CHAT_KEY: self.actor,
        })
        return state


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

    # ---- Stage C Batch 1, Finding 5: filter "Назад" returns to Leads, not Admin root ----

    def test_leads_filter_keyboard_back_button_targets_leads_not_root(self):
        markup = kb.leads_filter_keyboard()
        back_buttons = [btn for row in markup.inline_keyboard for btn in row if btn.text == "◀️ Назад"]
        self.assertEqual(len(back_buttons), 1)
        self.assertEqual(back_buttons[0].callback_data, "adminmenu:leads")

    async def test_leads_filter_back_returns_to_leads_list_not_root(self):
        # Симулируем реальный тап "Назад": callback_data теперь "adminmenu:leads",
        # что означает вызов ровно menu_leads (тот же обработчик, что и вход
        # в "Заявки" из корня) — сохранённое поведение по умолчанию (ACTIVE),
        # не сохранение прежнего фильтра (см. implementation plan).
        await content_store.add_lead({"service_name": "Активная"}, self.telegram)
        state = make_state(self.actor)
        cb = make_callback("adminleadaction:filter", chat_id=self.actor)
        await admin.leads_filter_start(cb, state)  # открыли фильтр

        back_cb = make_callback("adminmenu:leads", chat_id=self.actor)
        await admin.menu_leads(back_cb, state)  # тап "Назад"

        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)
        shown_text = back_cb.message.edit_text.await_args.args[0]
        self.assertIn("Заявки", shown_text)
        self.assertEqual((await state.get_data())["lead_filter"], "ACTIVE")

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


class AdminLeadWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 7: Admin -> Leads / Client Interaction Workflow.

    Full audit found the anchor-lifecycle class (Batch 4) already closed
    here -- menu_leads is the only reset_state_keep_nav() site in this
    block and was already migrated to flow.step_from_callback; every
    other handler is a self-healing raw edit_text with no intervening
    reset. content_store's lead functions (get_lead/update_lead_status/
    delete_lead/add_owner_message) already follow the established bool-
    return/no-raise pattern. The admin detail card (bot/lead.py::
    format_lead_admin_detail) already surfaces everything the owner needs
    to understand a request (Telegram identity, service, source, brief,
    calc summary, supplements, materials, owner replies, timestamps) --
    no data gaps found.

    Two real, reproduced bugs were found and fixed:

    1. lead_change_status read the freshly-updated lead unconditionally --
       if the lead was deleted between opening its card and clicking a
       status button (concurrent session, stale/replayed callback),
       format_lead_admin_detail(None)/lead_detail_keyboard(None) crashed
       with TypeError. Reproduced directly before fixing. Fixed with a
       single "if lead is None" guard, falling back to the leads list --
       the same graceful-missing-entity pattern used everywhere else in
       admin.py.
    2. lead_reply_start set cancel_to="root" -- unlike every other free-
       text step in admin.py (which preserves its specific context via
       _resolve_cancel's "options"/"sections"/"images" branches), cancelling
       out of "Текст сообщения клиенту:" lost the lead entirely and sent
       the owner to the bare admin root menu instead of back to the lead
       card they were replying to. Fixed by adding a "leads" branch to
       _resolve_cancel, mirroring the existing sections/images pattern.

    Lead deletion (start/confirm/cancel/missing) had zero prior handler-
    level test coverage -- covered here; found no bug (content_store.
    delete_lead is already bool-safe, and lead_delete_do re-renders the
    list rather than a specific lead, so it was never at crash risk)."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "333"
        self.actor = 333
        self.lead = await content_store.add_lead(
            {"service_name": "Лендинг", "task_description": "Тест"},
            {"user_id": 66666, "username": "client", "first_name": "Клиент"},
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_in_lead_detail(self, lead_id: int, msg_id: int = 500) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor,
            "lead_id": lead_id,
        })
        await state.set_state(AdminStates.lead_detail)
        return state

    # ---- 8/9: missing/stale lead does not crash ----
    async def test_lead_open_detail_missing_lead_shows_alert_without_crash(self):
        state = make_state(self.actor)
        cb = make_callback("adminleadpick:999999", chat_id=self.actor)
        await admin.lead_open_detail(cb, state)  # не должно бросить исключение
        cb.answer.assert_awaited_once_with("Заявка не найдена", show_alert=True)
        self.assertIsNone(await state.get_state())  # ничего не открыто

    async def test_lead_change_status_missing_lead_does_not_crash(self):
        state = await self._state_in_lead_detail(999999, msg_id=600)  # такой заявки нет
        cb = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor, message_id=600)

        await admin.lead_change_status(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once()
        self.assertIn("не найдена", cb.message.edit_text.await_args.args[0])
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)

    async def test_lead_change_status_same_status_is_a_no_op_not_a_crash(self):
        # E2E MVP audit, Batch 4: re-tapping the ALREADY-active status used
        # to call update_lead_status (unconditionally bumping updated_at)
        # and then edit_text() with byte-identical text+keyboard — Telegram
        # rejects that with an unhandled TelegramBadRequest("message is not
        # modified"). Now short-circuits before either happens.
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=605)
        cb = make_callback("adminleadstatus:NEW", chat_id=self.actor, message_id=605)  # NEW — уже текущий статус

        await admin.lead_change_status(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_not_awaited()  # ничего не перерисовано — нечего перерисовывать
        cb.answer.assert_awaited_once_with("Статус уже установлен")
        lead = await content_store.get_lead(self.lead["id"])
        self.assertEqual(lead["status"], "NEW")
        self.assertIsNone(lead["updated_at"])  # update_lead_status не вызывался — updated_at не тронут

    # ---- 10/13: reply start guards a missing/incomplete recipient ----
    async def test_lead_reply_start_missing_telegram_id_shows_alert_without_crash(self):
        lead = await content_store.add_lead({"service_name": "Без Telegram ID"}, {})
        state = await self._state_in_lead_detail(lead["id"], msg_id=610)
        cb = make_callback("adminleadaction:reply", chat_id=self.actor, message_id=610)

        await admin.lead_reply_start(cb, state)  # не должно бросить исключение

        cb.answer.assert_awaited_once_with("Нет Telegram ID клиента — ответить через бота нельзя", show_alert=True)
        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)  # остались на месте

    async def test_lead_reply_start_missing_lead_shows_alert_without_crash(self):
        state = await self._state_in_lead_detail(999999, msg_id=615)
        cb = make_callback("adminleadaction:reply", chat_id=self.actor, message_id=615)

        await admin.lead_reply_start(cb, state)  # не должно бросить исключение

        cb.answer.assert_awaited_once_with("Нет Telegram ID клиента — ответить через бота нельзя", show_alert=True)

    # ---- 7/11: reply cancellation returns to the lead card, not admin root ----
    async def test_lead_reply_start_sets_cancel_to_leads(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=620)
        await admin.lead_reply_start(make_callback("adminleadaction:reply", chat_id=self.actor, message_id=620), state)
        self.assertEqual((await state.get_data()).get("cancel_to"), "leads")
        self.assertEqual(await state.get_state(), AdminStates.lead_reply_text.state)

    async def test_lead_reply_inline_cancel_returns_to_lead_detail_not_root(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=630)
        await admin.lead_reply_start(make_callback("adminleadaction:reply", chat_id=self.actor, message_id=630), state)

        cb = make_callback("admincancel", chat_id=self.actor, message_id=630)
        await admin.admin_cancel(cb, state)

        cb.message.edit_text.assert_awaited_once()
        shown_text = cb.message.edit_text.await_args.args[0]
        self.assertIn(f"Заявка #{self.lead['id']}", shown_text)  # карточка заявки, не общий Админ-меню
        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)
        self.assertEqual((await state.get_data()).get("lead_id"), self.lead["id"])

    async def test_lead_reply_cancel_command_returns_to_lead_detail_not_root(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=640)
        await admin.lead_reply_start(make_callback("adminleadaction:reply", chat_id=self.actor, message_id=640), state)

        cancel_msg = make_reply_message(self.actor, "/cancel", AsyncMock())
        await admin.admin_cancel_command(cancel_msg, state)

        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)
        self.assertEqual((await state.get_data()).get("lead_id"), self.lead["id"])
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=640)  # старый prompt убран
        sent_text = cancel_msg.answer.await_args.args[0]
        self.assertIn(f"Заявка #{self.lead['id']}", sent_text)

    async def test_lead_reply_cancel_missing_lead_falls_back_to_leads_list(self):
        state = await self._state_in_lead_detail(999999, msg_id=650)
        await state.update_data(cancel_to="leads")
        await state.set_state(AdminStates.lead_reply_text)

        cb = make_callback("admincancel", chat_id=self.actor, message_id=650)
        await admin.admin_cancel(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once()
        self.assertIn("не найдена", cb.message.edit_text.await_args.args[0])
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)

    # ---- 6: Main Menu from lead detail cleans up correctly ----
    async def test_lead_detail_then_main_menu_cleans_current_screen(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=660)

        trigger = make_reply_message(self.actor, texts.MAIN_MENU_BUTTON, AsyncMock())
        await admin.admin_main_menu_button(trigger, state)
        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)  # активное состояние -> сперва подтверждение

        confirm_cb = SimpleNamespace(data="mainmenu:confirm", message=make_flow_message(chat_id=self.actor), answer=AsyncMock())
        await start.main_menu_confirm(confirm_cb, state)
        confirm_cb.message.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=660)
        confirm_cb.message.answer.assert_not_awaited()
        self.assertIsNone(await state.get_state())

    # ---- 15: delete/archive -- destructive, must be confirmed ----
    async def test_lead_delete_shows_confirm_and_does_not_remove_until_confirmed(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=670)
        cb = make_callback("adminleadaction:delete", chat_id=self.actor, message_id=670)
        await admin.lead_delete_start(cb, state)

        cb.message.edit_text.assert_awaited_once_with(
            f"Удалить заявку #{self.lead['id']}? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelleadconfirm")
        )
        self.assertEqual(await state.get_state(), AdminStates.lead_delete_confirm.state)
        self.assertIsNotNone(await content_store.get_lead(self.lead["id"]))  # ещё не удалена

    async def test_lead_delete_cancel_preserves_lead(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=680)
        await state.set_state(AdminStates.lead_delete_confirm)
        cb = make_callback("admindelleadconfirm:no", chat_id=self.actor, message_id=680)
        await admin.lead_delete_do(cb, state)

        self.assertIsNotNone(await content_store.get_lead(self.lead["id"]))
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)
        self.assertIn("Отменено", cb.message.edit_text.await_args.args[0])

    async def test_lead_delete_confirm_yes_removes_lead(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=690)
        await state.set_state(AdminStates.lead_delete_confirm)
        cb = make_callback("admindelleadconfirm:yes", chat_id=self.actor, message_id=690)
        await admin.lead_delete_do(cb, state)

        self.assertIsNone(await content_store.get_lead(self.lead["id"]))
        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)
        self.assertIn("удалена", cb.message.edit_text.await_args.args[0])

    async def test_lead_delete_missing_lead_does_not_crash(self):
        state = await self._state_in_lead_detail(999999, msg_id=700)
        await state.set_state(AdminStates.lead_delete_confirm)
        cb = make_callback("admindelleadconfirm:yes", chat_id=self.actor, message_id=700)

        await admin.lead_delete_do(cb, state)  # не должно бросить исключение

        self.assertEqual(await state.get_state(), AdminStates.leads_list.state)
        self.assertIsNotNone(await content_store.get_lead(self.lead["id"]))  # реальная заявка не задета

    # ---- 14: repeated open does not orphan messages (same message_id reused) ----
    async def test_repeated_lead_reopen_does_not_orphan_messages(self):
        state = make_state(self.actor)
        msg_id = 710
        await state.update_data(**{flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: self.actor})
        await admin.menu_leads(make_callback("adminmenu:leads", chat_id=self.actor, message_id=msg_id), state)

        cb1 = make_callback(f"adminleadpick:{self.lead['id']}", chat_id=self.actor, message_id=msg_id)
        await admin.lead_open_detail(cb1, state)
        cb2 = make_callback("adminleadaction:back", chat_id=self.actor, message_id=msg_id)
        await admin.lead_back_to_list(cb2, state)
        cb3 = make_callback(f"adminleadpick:{self.lead['id']}", chat_id=self.actor, message_id=msg_id)
        await admin.lead_open_detail(cb3, state)

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)  # всё ещё то же сообщение, ничего нового не создано
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    # ---- 16: ensure_nav_anchor after lead operations does not duplicate NAV ----
    async def test_ensure_nav_anchor_after_lead_change_status_does_not_recreate(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=720)
        await admin.lead_change_status(make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor, message_id=720), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=20600)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)

    async def test_ensure_nav_anchor_after_lead_delete_do_does_not_recreate(self):
        state = await self._state_in_lead_detail(self.lead["id"], msg_id=730)
        await state.set_state(AdminStates.lead_delete_confirm)
        await admin.lead_delete_do(make_callback("admindelleadconfirm:yes", chat_id=self.actor, message_id=730), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=20700)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)


class LeadMaterialsAdminTests(unittest.IsolatedAsyncioTestCase):
    """Stage B: карточка заявки -> "📎 Материалы" -> список -> "▶️ Отправить"
    повторно шлёт сохранённый file_id дизайнеру напрямую через Bot API
    (send_document/send_photo/send_video/send_animation) — файл никогда не
    скачивается на Render (см. content_store.record_lead_material,
    неизменный)."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "333"
        self.actor = 333
        self.lead = await content_store.add_lead(
            {"service_name": "Лендинг"},
            {"user_id": 66666, "username": "client", "first_name": "Клиент"},
        )
        # Индекс в materials[] — 0=document, 1=photo, 2=video, 3=animation.
        for kind, file_id in (("document", "doc-fid"), ("photo", "photo-fid"), ("video", "video-fid"), ("animation", "gif-fid")):
            await content_store.record_lead_material(self.lead["id"], file_id, f"{file_id}-uniq", kind, "new")

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _state_in_lead_detail(self) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(lead_id=self.lead["id"])
        await state.set_state(AdminStates.lead_detail)
        return state

    async def _state_in_materials_list(self) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(lead_id=self.lead["id"])
        await state.set_state(AdminStates.lead_materials_list)
        return state

    async def test_lead_detail_keyboard_shows_materials_button_with_count(self):
        lead = await content_store.get_lead(self.lead["id"])
        markup = kb.lead_detail_keyboard(lead)
        button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertIn("📎 Материалы (4)", button_texts)

    async def test_lead_detail_keyboard_hides_materials_button_when_none(self):
        empty_lead = await content_store.add_lead({"service_name": "Логотип"}, {"user_id": 999, "username": None})
        markup = kb.lead_detail_keyboard(empty_lead)
        button_texts = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertFalse(any("Материалы" in t for t in button_texts))

    async def test_materials_open_shows_list_and_sets_state(self):
        state = await self._state_in_lead_detail()
        cb = make_callback("adminleadaction:materials", chat_id=self.actor)
        await admin.lead_materials_open(cb, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertIn("Материалы заявки", cb.message.edit_text.await_args.args[0])
        self.assertEqual(await state.get_state(), AdminStates.lead_materials_list.state)

    async def test_materials_back_returns_to_lead_detail(self):
        state = await self._state_in_materials_list()
        cb = make_callback("adminleadaction:materialsback", chat_id=self.actor)
        await admin.lead_materials_back(cb, state)
        cb.message.edit_text.assert_awaited_once()
        self.assertIn(f"Заявка #{self.lead['id']}", cb.message.edit_text.await_args.args[0])
        self.assertEqual(await state.get_state(), AdminStates.lead_detail.state)

    async def test_resend_document_dispatches_send_document(self):
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:0", chat_id=self.actor)
        await admin.lead_material_resend(cb, state)
        cb.bot.send_document.assert_awaited_once_with(chat_id="333", document="doc-fid")
        cb.answer.assert_awaited_once_with("Отправлено ✅")

    async def test_resend_photo_dispatches_send_photo(self):
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:1", chat_id=self.actor)
        await admin.lead_material_resend(cb, state)
        cb.bot.send_photo.assert_awaited_once_with(chat_id="333", photo="photo-fid")

    async def test_resend_video_dispatches_send_video(self):
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:2", chat_id=self.actor)
        await admin.lead_material_resend(cb, state)
        cb.bot.send_video.assert_awaited_once_with(chat_id="333", video="video-fid")

    async def test_resend_animation_dispatches_send_animation(self):
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:3", chat_id=self.actor)
        await admin.lead_material_resend(cb, state)
        cb.bot.send_animation.assert_awaited_once_with(chat_id="333", animation="gif-fid")

    async def test_invalid_file_id_shows_friendly_error_not_crash(self):
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:0", chat_id=self.actor)
        cb.bot.send_document = AsyncMock(side_effect=TelegramAPIError(method=None, message="wrong file_id"))
        await admin.lead_material_resend(cb, state)  # не должно бросить исключение
        cb.answer.assert_awaited_once_with("Не удалось получить файл — возможно, он больше недоступен.", show_alert=True)

    async def test_failed_resend_does_not_mutate_lead(self):
        before = await content_store.get_lead(self.lead["id"])
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:0", chat_id=self.actor)
        cb.bot.send_document = AsyncMock(side_effect=TelegramAPIError(method=None, message="wrong file_id"))
        await admin.lead_material_resend(cb, state)
        after = await content_store.get_lead(self.lead["id"])
        self.assertEqual(before, after)

    async def test_successful_resend_does_not_create_duplicate_material(self):
        before = await content_store.get_lead(self.lead["id"])
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:0", chat_id=self.actor)
        await admin.lead_material_resend(cb, state)
        after = await content_store.get_lead(self.lead["id"])
        self.assertEqual(len(after["materials"]), len(before["materials"]))
        self.assertEqual(after["materials"], before["materials"])

    async def test_resend_unknown_index_shows_alert_not_crash(self):
        state = await self._state_in_materials_list()
        cb = make_callback("adminmaterialsend:99", chat_id=self.actor)
        await admin.lead_material_resend(cb, state)
        cb.answer.assert_awaited_once_with("Материал не найден", show_alert=True)


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


class WorkMessageSurvivesMainMenuTests(unittest.IsolatedAsyncioTestCase):
    """Stage B, item 8: подтверждаем архитектурную находку Stage A-аудита —
    "рабочие" сообщения (уведомления о заявке, материалы, ответы дизайнера,
    статус-уведомления) никогда не регистрируются как TRANSIENT
    (_ANCHOR_MSG_KEY) и поэтому не могут быть удалены RULE 1/2/3 или
    "⌂ Главное меню". Не переписываем flow.py — только доказываем текущее
    поведение реальным хендлером (lead_change_status), а не синтетическим
    сценарием."""

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
            {"service_name": "Лендинг"}, {"user_id": 55555, "username": "client", "first_name": "Клиент"},
        )

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_status_notification_does_not_register_as_transient_screen(self):
        # Реальный "рабочий" send: lead_change_status шлёт уведомление в чат
        # КЛИЕНТА (lead["telegram"]["user_id"]), не в чат дизайнера, и
        # никогда не трогает state.data — но даже независимо от chat_id,
        # ключевая проверка здесь именно в том, что _ANCHOR_MSG_KEY
        # (единственное, что удаляет "Главное меню"/RULE 1/2/3) остаётся
        # ровно тем же, что уже отслеживалось ДО этого вызова.
        TRACKED_SCREEN_ID = 42
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            flow._ANCHOR_MSG_KEY: TRACKED_SCREEN_ID, flow._ANCHOR_CHAT_KEY: self.actor,
            "lead_id": self.lead["id"],
        })
        cb = make_callback("adminleadstatus:IN_PROGRESS", chat_id=self.actor, message_id=TRACKED_SCREEN_ID)

        await admin.lead_change_status(cb, state)

        # Уведомление реально отправлено клиенту (это и есть "рабочее сообщение").
        cb.bot.send_message.assert_awaited_once()
        self.assertEqual(cb.bot.send_message.await_args.kwargs["chat_id"], 55555)
        # ...но _ANCHOR_MSG_KEY не изменился — уведомление НЕ стало
        # отслеживаемым TRANSIENT-экраном.
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), TRACKED_SCREEN_ID)

        # Поэтому "Главное меню" удалит РОВНО отслеживаемый экран admin.py
        # (карточку заявки), а не что-то, связанное с самим уведомлением —
        # тот же механизм, что уже доказан для чистой навигации в
        # MainMenuConfirmationTests.test_no_active_state_cleans_up_without_touching_nav_anchor.
        cleanup_msg = make_flow_message(chat_id=self.actor, text=texts.MAIN_MENU_BUTTON)
        await start.main_menu_button(cleanup_msg, state)
        cleanup_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=TRACKED_SCREEN_ID)

    async def test_material_forward_does_not_register_as_transient_screen(self):
        # Тот же принцип для handle_tz_file: он вообще не принимает state
        # (см. Stage A аудит — структурно не может тронуть _ANCHOR_MSG_KEY),
        # но здесь доказываем именно последствие — ПОСЛЕ прихода материала
        # уже существующий tracked TRANSIENT-экран admin.py остаётся тем же
        # и корректно удаляется "Главным меню", материал ему не мешает.
        TRACKED_SCREEN_ID = 77
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            flow._ANCHOR_MSG_KEY: TRACKED_SCREEN_ID, flow._ANCHOR_CHAT_KEY: self.actor,
        })

        message = make_message()
        await webapp._handle_brief_submission(
            message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "wm1"},
        )
        message.document = make_fake_document()
        await webapp.handle_tz_file(message)  # реальный "рабочий" forward клиентского материала

        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), TRACKED_SCREEN_ID)  # не тронут

        cleanup_msg = make_flow_message(chat_id=self.actor, text=texts.MAIN_MENU_BUTTON)
        await start.main_menu_button(cleanup_msg, state)
        cleanup_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=TRACKED_SCREEN_ID)


class ClientTextRelayTests(unittest.IsolatedAsyncioTestCase):
    """Stage B, item 5: свободный текст клиента -> DESIGNER_CHAT_ID вместо
    старого generic fallback_text (см. Stage A аудит — раньше сообщение
    просто терялось, дизайнер о нём не узнавал)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "999"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_single_active_lead_relay_is_tagged_with_lead_id(self):
        # telegram-словарь заявки намеренно совпадает с make_message()'s
        # дефолтным from_user (id=1, username="client", first_name="Клиент") —
        # это тот же самый клиент, что и создавал заявку, и теперь пишет
        # свободным текстом.
        lead = await content_store.add_lead(
            {"service_name": "Лендинг"}, {"user_id": 1, "username": "client", "first_name": "Клиент"},
        )
        message = make_message(chat_id=1)
        message.text = "Когда будет готово?"

        await start.relay_client_text_to_designer(message, make_state(1))

        message.bot.send_message.assert_awaited_once()
        kwargs = message.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], "999")
        self.assertIn(f"по заявке #{lead['id']}", kwargs["text"])
        self.assertIn("Клиент", kwargs["text"])
        self.assertIn("@client", kwargs["text"])
        self.assertIn("Когда будет готово?", kwargs["text"])

    # ---- Stage C Batch 1, Finding 4: relay stays under Telegram's 4096-char limit ----

    async def test_normal_relay_message_is_unchanged(self):
        # Regression guard: short client text must survive byte-for-byte,
        # not just "present as a substring" — proves the new clamp helper
        # is a true no-op below the threshold.
        await content_store.add_lead(
            {"service_name": "Лендинг"}, {"user_id": 1, "username": "client", "first_name": "Клиент"},
        )
        message = make_message(chat_id=1)
        message.text = "Обычное короткое сообщение."

        await start.relay_client_text_to_designer(message, make_state(1))

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertTrue(text.endswith("Текст клиента:\nОбычное короткое сообщение."))
        self.assertNotIn("…", text)

    async def test_oversized_client_text_is_truncated_and_stays_under_telegram_limit(self):
        await content_store.add_lead(
            {"service_name": "Лендинг"}, {"user_id": 1, "username": "client", "first_name": "Клиент"},
        )
        message = make_message(chat_id=1)
        message.text = "x" * 4500  # заведомо длиннее, чем Telegram вообще пропустил бы на входе клиенту

        await start.relay_client_text_to_designer(message, make_state(1))

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertLessEqual(len(text), 4096)  # реальный Telegram-лимит на исходящее сообщение
        self.assertIn("…", text)  # явный маркер обрезки
        # Заголовок/идентичность/лид-контекст сохранены целиком, не обрезаны.
        self.assertIn("Сообщение по заявке #", text)
        self.assertIn("Клиент: Клиент (@client)", text)
        self.assertIn("Текст клиента:", text)

    async def test_oversized_client_text_still_gets_normal_acknowledgement(self):
        # Обрезка происходит ДО send_message — значит "успешная" отправка
        # (никакого TelegramBadRequest на длину) и клиент получает обычное
        # подтверждение, а не текст из except-ветки ("Не получилось...").
        message = make_message(chat_id=1)
        message.text = "y" * 4500

        await start.relay_client_text_to_designer(message, make_state(1))

        message.answer.assert_awaited_once_with("Сообщение отправлено дизайнеру ✅")

    # ---- Stage C Batch 1, Finding 4 fix (after read-only review): pathological
    # header no longer breaks the "guaranteed under Telegram limit" contract ----

    def test_clamp_returns_empty_string_when_no_room_for_client_text(self):
        # Regression guard for the exact confirmed bug: раньше, когда для
        # текста клиента не оставалось места (available == 0), функция всё
        # равно возвращала маркер "…" — непустую строку без единого символа
        # текста клиента, что и приводило к превышению бюджета
        # (header_len + маркер). header в 3999 символов -> header_len=4000 ->
        # available=0 (см. _MAX_RELAY_LENGTH=4000).
        header_lines = ["x" * 3999]
        clamped = start._clamp_relay_client_text(header_lines, "нужно ли тут что-то поместить")
        self.assertEqual(clamped, "")

    async def test_pathological_large_header_still_stays_under_telegram_hard_limit(self):
        # Прямое воспроизведение сценария из read-only review: у одного
        # клиента патологически много активных заявок -> строка "Активные
        # заявки: ..." раздувает сам header далеко за пределы бюджета.
        # list_leads_by_user замокан синтетическим списком (900 записей) —
        # через content_store.add_lead это заняло бы ~24с на один тест
        # (реальная файловая запись на каждый lead), что неприемлемо для
        # regression-suite; семантика list_leads_by_user (list[dict] с
        # "status"/"id") воспроизведена точно.
        synthetic_leads = [{"id": i, "status": "NEW"} for i in range(1, 900)]
        message = make_message(chat_id=1)
        message.text = "обычный текст клиента"

        with patch.object(content_store, "list_leads_by_user", AsyncMock(return_value=synthetic_leads)):
            await start.relay_client_text_to_designer(message, make_state(1))

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertLessEqual(len(text), 4096)  # реальный Telegram-лимит — теперь гарантированно соблюдён
        message.answer.assert_awaited_once_with("Сообщение отправлено дизайнеру ✅")  # не ушло в except-ветку

    async def test_zero_active_leads_is_general_inquiry(self):
        message = make_message(chat_id=1)
        message.text = "Привет, а сколько стоит логотип?"

        await start.relay_client_text_to_designer(message, make_state(1))

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Общее обращение", text)
        self.assertNotIn("по заявке #", text)
        self.assertIn("Привет, а сколько стоит логотип?", text)

    async def test_multiple_active_leads_is_general_inquiry_with_ids(self):
        lead1 = await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 1, "username": "ivan"})
        lead2 = await content_store.add_lead({"service_name": "Логотип"}, {"user_id": 1, "username": "ivan"}, draft_id="d2")
        message = make_message(chat_id=1)
        message.text = "У меня два заказа, вопрос по обоим"

        await start.relay_client_text_to_designer(message, make_state(1))

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Общее обращение", text)
        self.assertIn(f"#{lead1['id']}", text)
        self.assertIn(f"#{lead2['id']}", text)

    async def test_done_and_cancelled_leads_are_not_treated_as_active(self):
        lead = await content_store.add_lead({"service_name": "Лендинг"}, {"user_id": 1, "username": "ivan"})
        await content_store.update_lead_status("999", lead["id"], "DONE")
        message = make_message(chat_id=1)
        message.text = "Спасибо, было отлично!"

        await start.relay_client_text_to_designer(message, make_state(1))

        text = message.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Общее обращение", text)  # НЕ привязано к done-заявке
        self.assertNotIn(f"#{lead['id']}", text)

    async def test_client_receives_acknowledgement(self):
        message = make_message(chat_id=1)
        message.text = "Привет"

        await start.relay_client_text_to_designer(message, make_state(1))

        message.answer.assert_awaited_once()
        self.assertIn("отправлено", message.answer.await_args.args[0].lower())

    async def test_relay_failure_gives_client_a_clear_error_not_generic_hint(self):
        message = make_message(chat_id=1)
        message.text = "Привет"
        message.bot.send_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="boom"))

        await start.relay_client_text_to_designer(message, make_state(1))  # не должно бросить исключение

        message.answer.assert_awaited_once()
        sent = message.answer.await_args.args[0]
        self.assertNotIn("Воспользуйтесь кнопками", sent)
        self.assertIn("Не получилось", sent)

    async def test_designer_own_text_is_not_relayed(self):
        message = make_message(chat_id=999)  # == DESIGNER_CHAT_ID
        message.from_user = SimpleNamespace(id=999, username="owner", first_name="Дизайнер", last_name=None)
        message.text = "заметка себе"
        state = make_state(999)

        with patch("bot.handlers.start.flow.open_root", new=AsyncMock()) as mocked_open_root:
            await start.relay_client_text_to_designer(message, state)

        message.bot.send_message.assert_not_awaited()  # НЕ relay'ится дизайнеру же
        mocked_open_root.assert_awaited_once()  # упало в прежний fallback_text


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


def make_non_image_document_message(chat_id: int, mime_type: str = "application/pdf") -> SimpleNamespace:
    """Batch 3 (finding B3-4) — тот же шаблон, что и make_photo_message, но
    document с не-image mime_type: воспроизводит "designer прислал файл
    document с mime_type=None (Telegram не всегда его отдаёт) — не должен
    провалиться как not (message.photo or message.document); это отдельная,
    более узкая проверка на mime_type, см. admin._is_valid_image_upload."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        photo=None,
        document=SimpleNamespace(file_id="fake_file_id", mime_type=mime_type),
        text=None,
        delete=AsyncMock(),
        bot=SimpleNamespace(
            get_file=AsyncMock(return_value=SimpleNamespace(file_path="documents/fake.pdf")),
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
        # P1-3, Batch 6: удаление изображения теперь требует confirm-шага
        # (было одним кликом) — сначала показывает "Удалить...?", реально
        # удаляет только admindelcaseimgconfirm:yes.
        await admin.case_image_action(make_callback("admincaseimgact:delete", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.case_image_pick_delete.state)
        self.assertEqual(len((await self._case())["images"]), 2)  # ещё не удалено
        await admin.case_image_delete_do(make_callback("admindelcaseimgconfirm:yes", chat_id=self.actor), state)
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


class AdminCaseManagementCompletenessTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 5: полный re-audit управления кейсами (add/edit/delete/
    images/sections) не нашёл ни одного нового anchor-lifecycle бага — все
    3 сайта reset_state_keep_nav()/finish_flow() в этом блоке
    (cases_add_description, cases_edit_field "done", cases_delete_do) уже
    были исправлены в Batch 4; все raw edit_text() — self-healing (тот же
    физический message_id, без промежуточного reset/set_data). Реальный
    пробел был в regression-покрытии, а не в коде: редактирование/удаление
    несуществующего кейса (content_store.update_case/delete_case тихо
    возвращают False, никогда не бросают — тот же принятый
    пред-существующий паттерн, что и у FAQ/pricing, см.
    test_faq_delete_missing_item_does_not_corrupt_navigation; не
    исправляется в рамках этого batch) и delete-половина инварианта
    ensure_nav_anchor (edit-done половина уже покрыта
    AdminRemainingAnchorGapsTests)."""

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

    async def _state_with_nav(self, nav_msg_id: int = 111) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
        })
        return state

    # ---- 9/15: редактирование метаданных несуществующего кейса не роняет
    # хендлер и не портит навигацию/данные ----
    async def test_cases_edit_value_missing_case_does_not_corrupt_navigation(self):
        state = await self._state_with_nav()
        msg_id = 13000
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor},
                                 case_id="case_does_not_exist", field="title", cancel_to="cases")
        await state.set_state(AdminStates.edit_case_value)
        msg = make_flow_message_factory(chat_id=self.actor, start_id=13100)(text="Новое название")

        await admin.cases_edit_value(msg, state)  # не должно бросить исключение

        msg.bot.edit_message_text.assert_awaited_once_with(
            "Обновлено ✅\n\nЧто ещё изменить?", chat_id=self.actor, message_id=msg_id, reply_markup=kb.case_field_keyboard()
        )
        self.assertEqual(await state.get_state(), AdminStates.edit_case_field_pick.state)
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        # никакой кейс не появился и не испортился
        self.assertFalse(any(c["id"] == "case_does_not_exist" for c in await content_store.list_cases()))

    # ---- 8/15: удаление несуществующего кейса — тот же принятый паттерн,
    # что и у test_faq_delete_missing_item_does_not_corrupt_navigation ----
    async def test_cases_delete_missing_case_does_not_corrupt_navigation(self):
        state = await self._state_with_nav()
        msg_id = 13200
        await state.update_data(case_id="case_does_not_exist", cancel_to="cases")
        await state.set_state(AdminStates.delete_case_confirm)
        cb = make_callback("admindelcaseconfirm:yes", chat_id=self.actor, message_id=msg_id)

        await admin.cases_delete_do(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once_with("Кейс удалён ✅", reply_markup=kb.admin_cases_menu_keyboard())
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), msg_id)
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)
        self.assertIsNone(await state.get_state())

    # ---- 13: ensure_nav_anchor после cases_delete_do не дублирует WELCOME
    # (edit-done половина этого инварианта уже покрыта
    # AdminRemainingAnchorGapsTests.test_ensure_nav_anchor_after_cases_edit_done_does_not_recreate) ----
    async def test_ensure_nav_anchor_after_cases_delete_do_does_not_recreate(self):
        case = await content_store.add_case(
            str(self.actor), case_id="case_ensure_del", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        state = await self._state_with_nav()
        await state.update_data(case_id=case["id"], cancel_to="cases")
        await state.set_state(AdminStates.delete_case_confirm)
        await admin.cases_delete_do(make_callback("admindelcaseconfirm:yes", chat_id=self.actor, message_id=13300), state)

        probe = make_flow_message_factory(chat_id=self.actor, start_id=13400)()
        created = await flow.ensure_nav_anchor(probe, state)
        self.assertFalse(created)
        probe.answer.assert_not_awaited()
        data = await state.get_data()
        self.assertEqual(data.get(flow._NAV_ANCHOR_MSG_KEY), 111)


class AdminCaseContentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 6: Case Images / Sections / Content Workflow.

    Full audit found the anchor-lifecycle class (Batch 4's territory)
    already closed here too -- every handler in this block either uses
    flow.step_from_text on its text/photo steps (already migrated in
    Batches 1-3) or is a self-healing raw edit_text with no intervening
    reset_state_keep_nav()/finish_flow(). No NEW reset-class bug existed
    to fix.

    What the audit DID find, backed by direct reproduction (see delivery
    report):

    1. Section delete and case-level gallery image delete were single-
       click, irreversible, with no confirm/cancel step -- unlike
       case-level delete, which already has one (delete_case_confirm).
       The batch's own required test list ("Delete section -> cancel",
       "Delete image -> cancel") presupposes this step exists. Added,
       mirroring cases_delete_confirm/cases_delete_do exactly, reusing
       the case_section_pick_delete/case_image_pick_delete states that
       were already declared in bot/states.py but never wired to any
       handler.
    2. case_section_edit_value, case_section_remove_image and the
       "removeimg" branch of case_section_action all read
       case["sections"][index] directly -- if the section (or the whole
       case) had been removed by a concurrent session, or the case
       simply never had a "sections" key yet (add_case never sets one;
       it's lazily created by the first add_case_section call), this
       raised KeyError/IndexError/TypeError instead of the established
       graceful "not found" pattern used everywhere else in this file.
       Reproduced directly before fixing (see report). Fixed with a
       single _current_section(case_id, index) -> dict | None helper.
    3. The exact UX nuance flagged at the end of Batch 5 ("Remove image
       -> Back skips past the section-detail screen straight to the
       sections list") was re-examined against this batch's explicit
       criteria: anchor stayed correct, FSM state stayed correct, no
       orphan, the section was always reopenable -- but it DID return to
       the wrong level (2 screens back instead of 1), contradicting the
       batch's own "correctly return one level back" requirement, and
       was trivially fixable within the existing architecture (its own
       callback_data instead of reusing the outer "back to sections"
       one). Fixed."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "444"
        self.actor = 444
        await content_store.add_case(
            str(self.actor), case_id="case_cw", title="Тест", type_id="landing",
            cover="img/portfolio/seed.svg", task="t", related_service=None,
        )
        await content_store.add_case_section(str(self.actor), "case_cw", section_type="text", title="Задача", content="старый текст")

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _case(self):
        return next(c for c in await content_store.list_cases() if c["id"] == "case_cw")

    async def _state_with_nav(self, nav_msg_id: int = 111) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: nav_msg_id,
            flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            flow._ANCHOR_MSG_KEY: nav_msg_id,
            flow._ANCHOR_CHAT_KEY: self.actor,
        })
        return state

    async def _state_in_section_detail(self, section_index: int = 0, msg_id: int = 500) -> FSMContext:
        state = await self._state_with_nav()
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor},
                                 case_id="case_cw", section_index=section_index, cancel_to="sections")
        await state.set_state(AdminStates.case_section_edit_field_pick)
        return state

    async def _state_in_images_menu(self, image_path: str = "img/portfolio/seed.svg", msg_id: int = 500) -> FSMContext:
        state = await self._state_with_nav()
        await state.update_data(**{flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor},
                                 case_id="case_cw", image_path=image_path, cancel_to="images")
        await state.set_state(AdminStates.case_images_menu)
        return state

    # ---- 3/25: Add section (gallery type) -> complete in one step ----
    async def test_add_section_gallery_type_completes_in_one_step(self):
        state = await self._state_with_nav()
        await state.update_data(case_id="case_cw", cancel_to="sections")
        await state.set_state(AdminStates.case_sections_menu)
        await admin.case_section_add_start(make_callback("admincasesecaction:add", chat_id=self.actor, message_id=111), state)
        await admin.case_section_add_type(make_callback("admincasesectype:gallery", chat_id=self.actor), state)
        title_msg = make_flow_message_factory(chat_id=self.actor, start_id=20000)(text="Скриншоты")
        await admin.case_section_add_title(title_msg, state)

        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)
        case = await self._case()
        self.assertEqual(len(case["sections"]), 2)
        self.assertEqual(case["sections"][1], {"type": "gallery", "title": "Скриншоты", "images": []})

    # ---- 7/8: Delete section -> cancel / confirm (new confirm step) ----
    async def test_section_delete_shows_confirm_and_does_not_remove_until_confirmed(self):
        state = await self._state_in_section_detail(section_index=0, msg_id=600)
        cb = make_callback("admincasesecact:delete", chat_id=self.actor, message_id=600)
        await admin.case_section_action(cb, state)

        cb.message.edit_text.assert_awaited_once_with(
            "Удалить раздел «Задача»? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelsecconfirm")
        )
        self.assertEqual(await state.get_state(), AdminStates.case_section_pick_delete.state)
        self.assertEqual(len((await self._case())["sections"]), 1)  # ещё не удалён
        data = await state.get_data()
        self.assertEqual(data.get(flow._ANCHOR_MSG_KEY), 600)  # self-healing, тот же message_id

    async def test_section_delete_cancel_preserves_section(self):
        state = await self._state_in_section_detail(section_index=0, msg_id=610)
        await state.set_state(AdminStates.case_section_pick_delete)
        cb = make_callback("admindelsecconfirm:no", chat_id=self.actor, message_id=610)
        await admin.case_section_delete_do(cb, state)

        cb.message.edit_text.assert_awaited_once()
        self.assertEqual(cb.message.edit_text.await_args.args[0], "Разделы кейса:")
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)
        self.assertEqual(len((await self._case())["sections"]), 1)

    async def test_section_delete_confirm_yes_removes_section(self):
        state = await self._state_in_section_detail(section_index=0, msg_id=620)
        await state.set_state(AdminStates.case_section_pick_delete)
        cb = make_callback("admindelsecconfirm:yes", chat_id=self.actor, message_id=620)
        await admin.case_section_delete_do(cb, state)

        cb.message.edit_text.assert_awaited_once()
        self.assertEqual(cb.message.edit_text.await_args.args[0], "Раздел удалён ✅\n\nРазделы кейса:")
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)
        self.assertEqual(len((await self._case())["sections"]), 0)

    # ---- 9/24: Delete missing section -- graceful, not a crash ----
    async def test_section_delete_missing_section_shows_not_found_without_crash(self):
        state = await self._state_in_section_detail(section_index=99, msg_id=630)  # индекс не существует
        cb = make_callback("admincasesecact:delete", chat_id=self.actor, message_id=630)
        await admin.case_section_action(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once_with(
            "Раздел не найден.\n\nРазделы кейса:", reply_markup=kb.case_sections_menu_keyboard((await self._case())["sections"])
        )
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)

    # ---- 24: Edit / remove-image on a missing section -- graceful, not a crash
    # (reproduced as a real KeyError/IndexError/TypeError before this batch's fix) ----
    async def test_case_section_edit_value_missing_section_does_not_crash(self):
        state = await self._state_in_section_detail(section_index=99, msg_id=640)
        await state.update_data(section_field="title")
        await state.set_state(AdminStates.case_section_edit_value)
        msg = make_flow_message_factory(chat_id=self.actor, start_id=20100)(text="Новое имя")

        await admin.case_section_edit_value(msg, state)  # не должно бросить исключение

        msg.bot.edit_message_text.assert_awaited_once_with(
            "Раздел не найден.\n\nРазделы кейса:", chat_id=self.actor, message_id=640,
            reply_markup=kb.case_sections_menu_keyboard((await self._case())["sections"]),
        )
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)

    async def test_case_section_remove_image_missing_section_does_not_crash(self):
        state = await self._state_in_section_detail(section_index=99, msg_id=650)
        cb = make_callback("admincasesecimgpick:0", chat_id=self.actor, message_id=650)

        await admin.case_section_remove_image(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once_with(
            "Раздел не найден.\n\nРазделы кейса:", reply_markup=kb.case_sections_menu_keyboard((await self._case())["sections"])
        )
        self.assertEqual(await state.get_state(), AdminStates.case_sections_menu.state)

    async def test_case_section_action_removeimg_missing_section_does_not_crash(self):
        state = await self._state_in_section_detail(section_index=99, msg_id=655)
        cb = make_callback("admincasesecact:removeimg", chat_id=self.actor, message_id=655)

        await admin.case_section_action(cb, state)  # не должно бросить исключение

        cb.answer.assert_awaited_once_with("Раздел не найден", show_alert=True)

    # ---- 13/14: Delete image (case-level gallery) -> cancel / confirm ----
    async def test_image_delete_shows_confirm_and_does_not_remove_until_confirmed(self):
        state = await self._state_in_images_menu(image_path="img/portfolio/seed.svg", msg_id=700)
        cb = make_callback("admincaseimgact:delete", chat_id=self.actor, message_id=700)
        await admin.case_image_action(cb, state)

        cb.message.edit_text.assert_awaited_once_with(
            "Удалить seed.svg? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelcaseimgconfirm")
        )
        self.assertEqual(await state.get_state(), AdminStates.case_image_pick_delete.state)
        self.assertIn("img/portfolio/seed.svg", (await self._case())["images"])

    async def test_image_delete_cancel_preserves_image(self):
        state = await self._state_in_images_menu(image_path="img/portfolio/seed.svg", msg_id=710)
        await state.set_state(AdminStates.case_image_pick_delete)
        cb = make_callback("admindelcaseimgconfirm:no", chat_id=self.actor, message_id=710)
        await admin.case_image_delete_do(cb, state)

        self.assertEqual(await state.get_state(), AdminStates.case_images_menu.state)
        self.assertIn("img/portfolio/seed.svg", (await self._case())["images"])

    # ---- 15/24: Delete missing image -- graceful, not a crash ----
    async def test_image_delete_missing_image_does_not_crash(self):
        state = await self._state_in_images_menu(image_path="img/portfolio/does_not_exist.jpg", msg_id=720)
        await state.set_state(AdminStates.case_image_pick_delete)
        cb = make_callback("admindelcaseimgconfirm:yes", chat_id=self.actor, message_id=720)

        await admin.case_image_delete_do(cb, state)  # не должно бросить исключение

        self.assertEqual(await state.get_state(), AdminStates.case_images_menu.state)
        # реальные изображения кейса не пострадали
        self.assertIn("img/portfolio/seed.svg", (await self._case())["images"])

    # ---- 16: Remove image (in-section) -> Back returns to section detail,
    # not the sections list (P1-3, Batch 6 fix; re-examined Batch 5 finding) ----
    async def test_remove_image_back_returns_to_section_detail_not_sections_list(self):
        await content_store.add_case_section(str(self.actor), "case_cw", section_type="gallery", title="Галерея", images=["img/a.jpg"])
        state = await self._state_in_section_detail(section_index=1, msg_id=800)  # индекс 1 -- галерея
        cb = make_callback("admincasesecact:backimg", chat_id=self.actor, message_id=800)

        await admin.case_section_action(cb, state)

        cb.message.edit_text.assert_awaited_once_with("«Галерея»:", reply_markup=kb.case_section_action_keyboard("gallery"))
        self.assertEqual(await state.get_state(), AdminStates.case_section_edit_field_pick.state)  # НЕ case_sections_menu
        # тот же раздел можно открыть повторно (не потерян контекст)
        data = await state.get_data()
        self.assertEqual(data.get("section_index"), 1)

    # ---- 17: Remove image (in-section picker) -> Main Menu cleans the screen ----
    async def test_main_menu_from_remove_image_picker_cleans_screen(self):
        await content_store.add_case_section(str(self.actor), "case_cw", section_type="gallery", title="Галерея", images=["img/a.jpg"])
        state = await self._state_in_section_detail(section_index=1, msg_id=810)
        await admin.case_section_action(make_callback("admincasesecact:removeimg", chat_id=self.actor, message_id=810), state)
        self.assertEqual(await state.get_state(), AdminStates.case_section_edit_field_pick.state)  # без изменений

        # состояние активно -> "⌂ Главное меню" сперва спрашивает
        # подтверждение (см. MainMenuConfirmationTests), а не удаляет сразу.
        trigger = make_flow_message_factory(chat_id=self.actor, start_id=20200)(text=texts.MAIN_MENU_BUTTON)
        await admin.admin_main_menu_button(trigger, state)
        self.assertEqual(await state.get_state(), AdminStates.case_section_edit_field_pick.state)  # не сброшено самим показом

        confirm_cb = SimpleNamespace(data="mainmenu:confirm", message=make_flow_message(chat_id=self.actor), answer=AsyncMock())
        await start.main_menu_confirm(confirm_cb, state)
        confirm_cb.message.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=810)
        confirm_cb.message.answer.assert_not_awaited()  # ни одного нового WELCOME
        self.assertIsNone(await state.get_state())

    # ---- 25: Cancel mid add-section (before content step) leaves no
    # partially-created section ----
    async def test_add_section_cancel_before_content_step_creates_no_partial_section(self):
        state = await self._state_with_nav()
        await state.update_data(case_id="case_cw", cancel_to="sections")
        await state.set_state(AdminStates.case_sections_menu)
        await admin.case_section_add_start(make_callback("admincasesecaction:add", chat_id=self.actor, message_id=900), state)
        await admin.case_section_add_type(make_callback("admincasesectype:text", chat_id=self.actor), state)
        title_msg = make_flow_message_factory(chat_id=self.actor, start_id=20300)(text="Незаконченный раздел")
        await admin.case_section_add_title(title_msg, state)  # только текст, до content не дошли

        cancel_msg = make_flow_message_factory(chat_id=self.actor, start_id=20400)(text="/cancel")
        await admin.admin_cancel_command(cancel_msg, state)

        self.assertFalse(any(s["title"] == "Незаконченный раздел" for s in (await self._case())["sections"]))

    # ---- 18/19: re-open case and section after edits -- data really persisted ----
    async def test_reopen_case_after_section_and_image_edits_persists_data(self):
        state = await self._state_in_section_detail(section_index=0, msg_id=1000)
        msg = make_flow_message_factory(chat_id=self.actor, start_id=20500)(text="Новая задача проекта")
        await state.update_data(section_field="content")
        await state.set_state(AdminStates.case_section_edit_value)
        await admin.case_section_edit_value(msg, state)

        img_state = await self._state_in_images_menu(image_path="img/portfolio/seed.svg", msg_id=1100)
        await admin.case_image_action(make_callback("admincaseimgact:cover", chat_id=self.actor, message_id=1100), img_state)

        # "переоткрываем" кейс с нуля -- те же данные, свежий вызов list_cases()
        reopened = next(c for c in await content_store.list_cases() if c["id"] == "case_cw")
        self.assertEqual(reopened["sections"][0]["content"], "Новая задача проекта")
        self.assertEqual(reopened["cover"], "img/portfolio/seed.svg")

        # тот же раздел снова открывается корректно (не потерян)
        reopen_cb = make_callback("admincasesecpick:0", chat_id=self.actor, message_id=1200)
        reopen_state = await self._state_with_nav()
        await reopen_state.update_data(case_id="case_cw", cancel_to="sections")
        await reopen_state.set_state(AdminStates.case_sections_menu)
        await admin.case_section_picked(reopen_cb, reopen_state)
        reopen_cb.message.edit_text.assert_awaited_once_with("«Задача»:", reply_markup=kb.case_section_action_keyboard("text"))


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

    # ---- Product Readiness audit, 2026-08-22: бэкап-меню отражает реальный
    # storage backend (content_store.is_redis_backed), а не всегда
    # предполагает эфемерную локальную ФС. Патчим is_redis_backed напрямую
    # (не UPSTASH_REDIS_REST_URL/TOKEN) — иначе export/import реально пойдёт
    # в _upstash_command и попытается сделать настоящий сетевой запрос на
    # fake-хост; здесь проверяется только текст, а не сама Redis-ветка
    # storage-слоя (та уже покрыта FakeUpstash-тестами в другом месте файла).

    async def test_backup_menu_text_for_local_storage_says_restore_after_each_deploy(self):
        state = make_state(self.actor)
        cb = make_callback("adminmenu:backup", chat_id=self.actor)
        with patch("bot.handlers.admin.content_store.is_redis_backed", return_value=False):
            await admin.menu_backup(cb, state)
        text = cb.message.edit_text.await_args.args[0]
        self.assertIn("переживает деплой, только если вы его восстановите", text)

    async def test_backup_menu_text_for_redis_storage_says_survives_automatically(self):
        state = make_state(self.actor)
        cb = make_callback("adminmenu:backup", chat_id=self.actor)
        with patch("bot.handlers.admin.content_store.is_redis_backed", return_value=True):
            await admin.menu_backup(cb, state)
        text = cb.message.edit_text.await_args.args[0]
        self.assertIn("переживают деплой автоматически", text)
        self.assertNotIn("восстановите после каждого обновления", text)

    async def test_export_caption_for_local_storage_says_restore_after_deploy(self):
        state = make_state(self.actor)
        cb = self._make_export_callback()
        with patch("bot.handlers.admin.content_store.is_redis_backed", return_value=False):
            await admin.backup_export(cb, state)
        caption = cb.message.answer_document.await_args.kwargs["caption"]
        self.assertIn("восстанавливайте после деплоя", caption)

    async def test_export_caption_for_redis_storage_says_additional_safety_copy(self):
        state = make_state(self.actor)
        cb = self._make_export_callback()
        with patch("bot.handlers.admin.content_store.is_redis_backed", return_value=True):
            await admin.backup_export(cb, state)
        caption = cb.message.answer_document.await_args.kwargs["caption"]
        self.assertIn("дополнительная копия", caption)
        self.assertNotIn("восстанавливайте после деплоя", caption)

    # P1-3, Batch 10: restore теперь требует явного подтверждения (см.
    # backup_restore_do) — единственное destructive-действие в admin.py,
    # которое раньше срабатывало немедленно по факту загрузки файла, без
    # confirm-шага (аудит нашёл это единственным подобным пробелом во всём
    # файле). backup_import_receive теперь только скачивает и сохраняет
    # байты; сам import_backup_bytes вызывается только из backup_restore_do
    # после "Да" — три теста ниже обновлены на двухшаговый проход через
    # реальные хендлеры (не мок) той же реальной цепочкой, что и раньше.
    async def _confirm_real_restore(self, state: FSMContext) -> SimpleNamespace:
        cb = SimpleNamespace(
            data="adminbackuprestoreconfirm:yes",
            message=SimpleNamespace(chat=SimpleNamespace(id=self.actor), edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await admin.backup_restore_do(cb, state)
        return cb

    async def test_import_via_real_handler_restores_changed_data(self):
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(str(self.actor), "landing", "SITE")
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "SITE")

        state = make_state(self.actor)
        await admin.backup_import_start(make_callback("adminbackupaction:import", chat_id=self.actor), state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_wait_file.state)

        await admin.backup_import_receive(self._make_zip_document_message(zip_bytes), state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_confirm.state)
        # подтверждение ещё не нажато -- данные ещё НЕ восстановлены
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "SITE")

        await self._confirm_real_restore(state)

        self.assertEqual(await content_store.default_related_service_for_type("landing"), "LEND")
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_import_restore_cancel_leaves_data_unchanged(self):
        zip_bytes = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(str(self.actor), "landing", "SITE")

        state = make_state(self.actor)
        await admin.backup_import_receive(self._make_zip_document_message(zip_bytes), state)
        cb = SimpleNamespace(
            data="adminbackuprestoreconfirm:no",
            message=SimpleNamespace(chat=SimpleNamespace(id=self.actor), edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await admin.backup_restore_do(cb, state)

        # "Нет" -- ничего не восстановлено, текущие (изменённые) данные целы
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "SITE")
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)
        cb.message.edit_text.assert_awaited_once_with("Отменено. Бэкап:", reply_markup=kb.backup_menu_keyboard())

    async def test_import_bad_zip_via_real_handler_does_not_crash(self):
        state = make_state(self.actor)
        msg = self._make_zip_document_message(b"not a zip")
        await admin.backup_import_receive(msg, state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_confirm.state)

        cb = await self._confirm_real_restore(state)
        cb.message.edit_text.assert_awaited_once()
        self.assertIn("повреждён", cb.message.edit_text.await_args.args[0])
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_wait_file.state)

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
        await admin.backup_import_receive(msg, state)

        with patch("bot.content_store._read", side_effect=RuntimeError("simulated snapshot read failure")):
            cb = await self._confirm_real_restore(state)

        cb.message.edit_text.assert_awaited_once()
        reply_text = cb.message.edit_text.await_args.args[0]
        self.assertIn("отменено", reply_text.lower())
        self.assertNotIn("simulated snapshot read failure", reply_text)  # деталь исходного исключения не утекает
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)
        self.assertEqual((Path(self.tmpdir) / "leads.json").read_bytes(), original_before)  # ничего не записано

    # ---- 6/8: stale/missing upload, repeated import, Main Menu from confirm ----
    async def test_backup_restore_stale_missing_upload_does_not_crash(self):
        # Устаревший/повторный callback без предварительного
        # backup_import_receive в ЭТОЙ сессии (state.data никогда не
        # содержал pending_backup_bytes) — см. аудит "stale/concurrent
        # operations". Не должно случиться при нормальной навигации, но
        # graceful fallback вместо KeyError/AttributeError.
        state = make_state(self.actor)
        cb = await self._confirm_real_restore(state)
        cb.message.edit_text.assert_awaited_once_with("Файл не найден — пришлите его заново.\n\nБэкап:", reply_markup=kb.backup_menu_keyboard())
        self.assertEqual(await state.get_state(), AdminStates.backup_menu.state)

    async def test_backup_restore_repeated_upload_uses_latest_not_stale_bytes(self):
        # Загрузили файл A, передумали и загрузили B ДО подтверждения —
        # "Да" должен восстановить B, а не осевшие в state.data байты A.
        zip_a = await content_store.export_backup_bytes()
        await content_store.update_portfolio_type_related_service(str(self.actor), "landing", "SITE")
        zip_b = await content_store.export_backup_bytes()  # содержит SITE
        await content_store.update_portfolio_type_related_service(str(self.actor), "landing", "LEND")  # текущее -> LEND (не в A, не в B)

        state = make_state(self.actor)
        await admin.backup_import_receive(self._make_zip_document_message(zip_a), state)
        await admin.backup_import_receive(self._make_zip_document_message(zip_b), state)
        await self._confirm_real_restore(state)

        self.assertEqual(await content_store.default_related_service_for_type("landing"), "SITE")  # из B, не из A

    async def test_backup_menu_main_menu_cleans_up_from_confirm_screen(self):
        zip_bytes = await content_store.export_backup_bytes()  # содержит текущее (LEND)
        await content_store.update_portfolio_type_related_service(str(self.actor), "landing", "SITE")
        state = make_state(self.actor)
        # anchor задан заранее (как реально бывает — экран уже отслеживался
        # ДО загрузки файла): без этого flow.step_from_text ушёл бы в
        # answer()-fallback, а _make_zip_document_message даёт для него
        # неконфигурированный AsyncMock без реальных chat/message_id.
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            flow._ANCHOR_MSG_KEY: 900, flow._ANCHOR_CHAT_KEY: self.actor,
        })
        msg = self._make_zip_document_message(zip_bytes)
        await admin.backup_import_receive(msg, state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_confirm.state)
        anchor_id = (await state.get_data()).get(flow._ANCHOR_MSG_KEY)
        self.assertEqual(anchor_id, 900)

        trigger = make_reply_message(self.actor, texts.MAIN_MENU_BUTTON, AsyncMock())
        await admin.admin_main_menu_button(trigger, state)
        self.assertEqual(await state.get_state(), AdminStates.backup_restore_confirm.state)  # сперва confirmation

        confirm_cb = SimpleNamespace(data="mainmenu:confirm", message=make_flow_message(chat_id=self.actor), answer=AsyncMock())
        await start.main_menu_confirm(confirm_cb, state)
        confirm_cb.message.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=anchor_id)
        self.assertIsNone(await state.get_state())
        # ушли через Главное меню, НЕ подтвердив -- restore не произошёл,
        # текущее (изменённое уже после экспорта) значение осталось как есть
        self.assertEqual(await content_store.default_related_service_for_type("landing"), "SITE")


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

    # ---- Batch 3: R2 cleanup on avatar replacement ----

    async def test_update_avatar_deletes_old_value_from_r2(self):
        old_avatar = (await content_store.get_about())["avatar"]
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            await content_store.update_about_field(self.actor, "avatar", "https://pub-test.r2.dev/about/avatar_new.jpg")
        mock_delete.assert_awaited_once_with(old_avatar)

    async def test_update_avatar_to_same_value_does_not_call_delete(self):
        old_avatar = (await content_store.get_about())["avatar"]
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            await content_store.update_about_field(self.actor, "avatar", old_avatar)
        mock_delete.assert_not_awaited()

    async def test_update_unrelated_field_does_not_call_delete(self):
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            await content_store.update_about_field(self.actor, "location", "Питер")
        mock_delete.assert_not_awaited()

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


class AdminAboutExperienceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 8: audit of the remaining admin content domains not yet
    deep-audited beyond the mechanical anchor-lifecycle sweep (Pricing/
    Services/Options, FAQ, Categories) found them already clean --
    destructive actions already confirmed, cancel_to targets already
    correct, no crash risk on missing entities. About -> Experience was
    the one remaining gap, mirroring exactly what Batch 6 found for Case
    sections/images and Batch 7 found for Leads:

    1. Deleting an experience entry was a single click with no
       confirmation, unlike every other destructive action in admin.py
       (case/section/image/service/option/FAQ/category/lead delete all
       already confirm). AdminStates.about_experience_pick_delete was
       already declared in bot/states.py but never wired to a handler --
       the same unused-state signature Batch 6 found for
       case_section_pick_delete. Fixed by wiring it up, mirroring
       option_delete_confirm/option_delete_do exactly.
    2. Both entry points into the "Опыт работы" sub-flow (about_edit_field's
       "experience" branch, and about_experience_add_start) set
       cancel_to="root" -- cancelling out of adding an entry, or out of
       the list itself, skipped past the experience list straight to the
       bare admin root, the same class of bug Batch 7 fixed for
       lead_reply_start. Fixed by adding an "experience" branch to
       _resolve_cancel, mirroring the existing sections/images/leads
       pattern."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        real_data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("pricing.json", "portfolio.json", "faq.json", "about.json", "ui_config.json"):
            shutil.copy(real_data_dir / name, Path(self.tmpdir) / name)
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "222"
        self.actor = 222
        # реальный about.json уже содержит seed-записи опыта — сбрасываем в
        # пустой список для изолированного, детерминированного индекса.
        await content_store.update_about_field(str(self.actor), "experience", [])
        await content_store.add_about_experience(str(self.actor), role="Дизайнер", company="Acme", period="2020-2023", description="")

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _entries(self):
        return (await content_store.get_about()).get("experience", [])

    async def _state_with_entry_open(self, index: int = 0, msg_id: int = 500) -> FSMContext:
        state = make_state(self.actor)
        await state.update_data(**{
            flow._NAV_ANCHOR_MSG_KEY: 111, flow._NAV_ANCHOR_CHAT_KEY: self.actor,
            flow._ANCHOR_MSG_KEY: msg_id, flow._ANCHOR_CHAT_KEY: self.actor,
            "exp_index": index,
        })
        await state.set_state(AdminStates.about_experience_menu)
        return state

    # ---- delete now requires confirmation (was single-click) ----
    async def test_experience_delete_shows_confirm_and_does_not_remove_until_confirmed(self):
        state = await self._state_with_entry_open(0, msg_id=600)
        cb = make_callback("adminaboutexpentry:delete", chat_id=self.actor, message_id=600)
        await admin.about_experience_entry_action(cb, state)

        cb.message.edit_text.assert_awaited_once_with(
            "Удалить запись «Дизайнер — Acme»? Это необратимо.", reply_markup=kb.confirm_keyboard("admindelexpconfirm")
        )
        self.assertEqual(await state.get_state(), AdminStates.about_experience_pick_delete.state)
        self.assertEqual(len(await self._entries()), 1)  # ещё не удалена

    async def test_experience_delete_cancel_preserves_entry(self):
        state = await self._state_with_entry_open(0, msg_id=610)
        await state.set_state(AdminStates.about_experience_pick_delete)
        cb = make_callback("admindelexpconfirm:no", chat_id=self.actor, message_id=610)
        await admin.about_experience_delete_do(cb, state)

        self.assertEqual(len(await self._entries()), 1)
        self.assertEqual(await state.get_state(), AdminStates.about_experience_menu.state)
        self.assertEqual(cb.message.edit_text.await_args.args[0], "Опыт работы:")

    async def test_experience_delete_confirm_yes_removes_entry(self):
        state = await self._state_with_entry_open(0, msg_id=620)
        await state.set_state(AdminStates.about_experience_pick_delete)
        cb = make_callback("admindelexpconfirm:yes", chat_id=self.actor, message_id=620)
        await admin.about_experience_delete_do(cb, state)

        self.assertEqual(len(await self._entries()), 0)
        self.assertEqual(await state.get_state(), AdminStates.about_experience_menu.state)
        self.assertEqual(cb.message.edit_text.await_args.args[0], "Запись удалена ✅\n\nОпыт работы:")

    async def test_experience_delete_missing_entry_does_not_crash(self):
        state = await self._state_with_entry_open(99, msg_id=630)  # такого индекса нет
        cb = make_callback("adminaboutexpentry:delete", chat_id=self.actor, message_id=630)

        await admin.about_experience_entry_action(cb, state)  # не должно бросить исключение

        cb.message.edit_text.assert_awaited_once_with(
            "Запись не найдена.\n\nОпыт работы:", reply_markup=kb.about_experience_menu_keyboard(await self._entries())
        )
        self.assertEqual(await state.get_state(), AdminStates.about_experience_menu.state)
        self.assertEqual(len(await self._entries()), 1)  # реальная запись не задета

    # ---- cancel_to="experience", not "root" (was losing the list context) ----
    async def test_experience_menu_sets_cancel_to_experience(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.edit_about_field_pick)
        await admin.about_edit_field(make_callback("admineditabout:experience", chat_id=self.actor), state)
        self.assertEqual((await state.get_data()).get("cancel_to"), "experience")
        self.assertEqual(await state.get_state(), AdminStates.about_experience_menu.state)

    async def test_experience_add_start_sets_cancel_to_experience(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.about_experience_menu)
        await admin.about_experience_add_start(make_callback("adminaboutexpaction:add", chat_id=self.actor), state)
        self.assertEqual((await state.get_data()).get("cancel_to"), "experience")

    async def test_experience_add_cancel_returns_to_experience_list_not_root(self):
        state = await self._state_with_entry_open(0, msg_id=640)
        await state.set_state(AdminStates.about_experience_menu)
        await admin.about_experience_add_start(make_callback("adminaboutexpaction:add", chat_id=self.actor, message_id=640), state)

        cancel_msg = make_reply_message(self.actor, "/cancel", AsyncMock())
        await admin.admin_cancel_command(cancel_msg, state)

        self.assertEqual(await state.get_state(), AdminStates.about_experience_menu.state)
        sent_text = cancel_msg.answer.await_args.args[0]
        self.assertEqual(sent_text, "Отменено. Опыт работы:")  # не "Отменено. Админ-меню:"
        cancel_msg.bot.delete_message.assert_awaited_once_with(chat_id=self.actor, message_id=640)


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

    async def test_import_zip_slip_image_entry_is_confined_to_portfolio_dir(self):
        # Security regression (Product Readiness audit, F2): import_backup_bytes
        # extracts img/portfolio/* entries via IMG_PORTFOLIO_DIR / Path(name).name
        # (basename only, see content_store.py) — the mechanism was already
        # judged correct by code inspection, this proves it holds against an
        # actual crafted traversal filename, not just by reading the code.
        # Does NOT change import_backup_bytes — regression test only.
        zip_bytes = await content_store.export_backup_bytes()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            entries = {n: zf.read(n) for n in zf.namelist()}
        entries["img/portfolio/../../../evil_traversal.txt"] = b"malicious content"
        combined_zip = _make_zip(entries)

        # "../../../" от img/portfolio/ целились бы сюда — за пределы и
        # IMG_PORTFOLIO_DIR, и всего self.img_tmpdir, будь basename-защита
        # обойдена.
        outside_target = Path(self.img_tmpdir).parent / "evil_traversal.txt"

        result = await content_store.import_backup_bytes(self.actor, combined_zip)

        self.assertFalse(outside_target.exists())  # traversal-путь не создан
        # Не "отклонено", а "безопасно нейтрализовано" — так и задокументировано
        # в import_backup_bytes: basename кладёт запись ВНУТРИ IMG_PORTFOLIO_DIR.
        self.assertTrue((content_store.IMG_PORTFOLIO_DIR / "evil_traversal.txt").exists())
        self.assertEqual((content_store.IMG_PORTFOLIO_DIR / "evil_traversal.txt").read_bytes(), b"malicious content")
        self.assertIn("img/portfolio/../../../evil_traversal.txt", result.restored_images)

        # Остальное состояние (легитимный case_1.jpg, все data/*.json) не
        # пострадало — вредоносная запись не мешает нормальному restore.
        self.assertEqual((content_store.IMG_PORTFOLIO_DIR / "case_1.jpg").read_bytes(), b"fake-jpeg-bytes")
        self.assertEqual(sorted(result.restored_json), sorted(content_store.DATA_FILENAMES))

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


class BackupRestoreEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Backup/Restore E2E audit, 2026-08-22: единый сквозной прогон
    export -> ZIP -> mutate -> restore через РЕАЛЬНЫЙ production-путь
    (content_store.export_backup_bytes/import_backup_bytes — те же функции,
    что вызывает bot/handlers/admin.py, внутренняя логика не копируется).

    Отличие от BackupExportImportTests выше: та проверяет отдельные аспекты
    (по одному изменённому полю за тест), здесь — один представительный
    датасет, покрывающий ВСЕ категории (leads/cases/services/about/faq/
    ui_config/image) сразу, с заведомо уникальными "ORIGINAL_"-маркерами,
    которые нельзя спутать со случайным совпадением, плюс cross-file
    referential integrity (case.type/related_service -> реальные portfolio
    types/pricing services) после restore.

    Полностью изолировано от сети и Upstash: UPSTASH_REDIS_REST_URL/TOKEN
    не заданы (дефолт тестового окружения) => content_store._upstash_enabled()
    ложь => _read/_write идут через _read_local/_write_local в tmpdir (см.
    content_store.py) — тот же изоляционный механизм, что и во всех
    остальных Backup*Tests выше, ничего нового не введено.

    FSM/confirm-шаг (upload -> "Да" -> backup_restore_do) сюда намеренно не
    входит — он уже покрыт отдельно и полно в AdminBackupHandlersTests
    (test_import_via_real_handler_restores_changed_data и соседние), которые
    гоняют реальные bot/handlers/admin.py хендлеры. Здесь фокус на глубине
    датасета и cross-file integrity, не на переигрывании FSM-переходов —
    см. Limitations в итоговом отчёте."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.img_tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = content_store.DATA_DIR
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        self._orig_img_portfolio = content_store.IMG_PORTFOLIO_DIR
        self._orig_img_about = content_store.IMG_ABOUT_DIR
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "777"
        content_store.IMG_PORTFOLIO_DIR = Path(self.img_tmpdir) / "portfolio"
        content_store.IMG_ABOUT_DIR = Path(self.img_tmpdir) / "about"
        content_store.IMG_PORTFOLIO_DIR.mkdir(parents=True)
        content_store.IMG_ABOUT_DIR.mkdir(parents=True)
        self.actor = "777"

        # Небольшой, но представительный dataset: одно значение на
        # категорию, каждое с "ORIGINAL_"-маркером — после restore можно
        # однозначно отличить "вернулось из бэкапа" от "случайно не менялось".
        portfolio = {
            "cases": [{
                "id": "case_e2e", "title": "ORIGINAL_TITLE", "type": "e2e_type",
                "cover": "img/portfolio/e2e_cover.jpg", "images": ["img/portfolio/e2e_cover.jpg"],
                "task": "ORIGINAL_TASK", "related_service": "E2E_SVC",
            }],
            "types": [{"id": "e2e_type", "label": "ORIGINAL_TYPE_LABEL", "related_service": "E2E_SVC"}],
        }
        pricing = {
            "services": [{
                "id": "E2E_SVC", "name": "ORIGINAL_SERVICE_NAME", "base_price": 10000,
                "term_min": 1, "term_max": 2, "includes": "ORIGINAL_INCLUDES",
            }],
            "options": [{"service_id": "E2E_SVC", "id": "E2E_OPT", "name": "ORIGINAL_OPTION", "price": 500, "days": 1, "multipliable": False}],
            "groups": [],
            "coefficients": {"urgent": {"label": "Срочно", "multiplier": 1.25}},
            "rounding": {"price_from_factor": 0.95, "price_to_factor": 1.05, "round_to": 500},
        }
        about = {
            "needs_review_fields": [], "avatar": "img/about/e2e_avatar.jpg",
            "name": "ORIGINAL_NAME", "tagline": "ORIGINAL_TAGLINE", "location": "",
            "specialization": [], "tools": [], "skills": [], "experience_years": "1",
            "experience_text": "ORIGINAL_EXPERIENCE", "approach": "ORIGINAL_APPROACH",
            "experience": [], "education": {"enabled": False, "items": []}, "links": [],
        }
        faq = {"faq": [{"id": 1, "type": "static", "question": "ORIGINAL_Q", "answer": "ORIGINAL_A", "needs_review": False}]}
        ui_config = {"menu": {"faq": True, "portfolio": True}}
        leads = {"leads": [{
            "id": 1, "draft_id": None, "status": "NEW", "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": None, "payload": {"service_name": "ORIGINAL_LEAD_MARKER"},
            "telegram": {"user_id": 555, "username": "e2e_client"}, "calc_summary": None,
            "awaiting_tz_file": False, "awaiting_tz_file_source": None,
        }]}

        for name, data in (
            ("portfolio.json", portfolio), ("pricing.json", pricing), ("faq.json", faq),
            ("about.json", about), ("ui_config.json", ui_config), ("leads.json", leads),
        ):
            (Path(self.tmpdir) / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        (content_store.IMG_PORTFOLIO_DIR / "e2e_cover.jpg").write_bytes(b"ORIGINAL_IMAGE_BYTES")

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        content_store.IMG_PORTFOLIO_DIR = self._orig_img_portfolio
        content_store.IMG_ABOUT_DIR = self._orig_img_about
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.img_tmpdir, ignore_errors=True)

    # ---- B: export archive integrity + export не мутирует источник ----

    async def test_export_produces_exact_expected_archive_without_mutating_source(self):
        pre_export_bytes = {name: (Path(self.tmpdir) / name).read_bytes() for name in content_store.DATA_FILENAMES}

        zip_bytes = await content_store.export_backup_bytes()

        # читается стандартным ZipFile без ошибок структуры
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            self.assertIsNone(zf.testzip())  # None == нет повреждённых записей
            names = set(zf.namelist())
            expected = {f"data/{n}" for n in content_store.DATA_FILENAMES} | {"img/portfolio/e2e_cover.jpg"}
            self.assertEqual(names, expected)  # ровно ожидаемые файлы, ни одного лишнего
            self.assertEqual(
                json.loads(zf.read("data/pricing.json"))["services"][0]["name"],
                "ORIGINAL_SERVICE_NAME",
            )

        # export только читает — исходные файлы на диске побайтово не тронуты
        for name in content_store.DATA_FILENAMES:
            self.assertEqual((Path(self.tmpdir) / name).read_bytes(), pre_export_bytes[name])
        self.assertEqual((content_store.IMG_PORTFOLIO_DIR / "e2e_cover.jpg").read_bytes(), b"ORIGINAL_IMAGE_BYTES")

    # ---- A-E: полный сквозной цикл по всем категориям + cross-file integrity ----

    async def test_full_export_mutate_restore_cycle_recovers_every_category(self):
        zip_bytes = await content_store.export_backup_bytes()

        # ---- C: намеренная, заметная мутация каждой категории ----
        self.assertTrue(await content_store.update_service(self.actor, "E2E_SVC", name="MUTATED_SERVICE_NAME"))
        self.assertTrue(await content_store.update_case(self.actor, "case_e2e", title="MUTATED_TITLE"))
        self.assertTrue(await content_store.update_about_field(self.actor, "tagline", "MUTATED_TAGLINE"))
        self.assertTrue(await content_store.update_faq(self.actor, 1, answer="MUTATED_ANSWER"))
        self.assertTrue(await content_store.update_lead_status(self.actor, 1, "DONE"))
        self.assertTrue(await content_store.set_menu_item_enabled(self.actor, "faq", False))
        (content_store.IMG_PORTFOLIO_DIR / "e2e_cover.jpg").write_bytes(b"MUTATED_IMAGE_BYTES")

        # sanity: мутации реально применились (иначе restore-проверка ниже
        # ничего бы не доказывала — тест был бы ложно-зелёным)
        services = await content_store.list_services()
        self.assertEqual(next(s for s in services if s["id"] == "E2E_SVC")["name"], "MUTATED_SERVICE_NAME")
        cases = await content_store.list_cases()
        self.assertEqual(next(c for c in cases if c["id"] == "case_e2e")["title"], "MUTATED_TITLE")
        self.assertEqual((await content_store.get_about())["tagline"], "MUTATED_TAGLINE")
        self.assertEqual(next(f for f in await content_store.list_faq() if f["id"] == 1)["answer"], "MUTATED_ANSWER")
        leads_before_restore = (await content_store._read("leads.json"))["leads"]
        self.assertEqual(next(l for l in leads_before_restore if l["id"] == 1)["status"], "DONE")
        self.assertFalse((await content_store.get_ui_config())["menu"]["faq"])
        self.assertEqual((content_store.IMG_PORTFOLIO_DIR / "e2e_cover.jpg").read_bytes(), b"MUTATED_IMAGE_BYTES")

        # ---- D: restore через реальный production-путь ----
        result = await content_store.import_backup_bytes(self.actor, zip_bytes)

        # ---- E: все категории вернулись к ORIGINAL_*, мутации исчезли,
        # ничего не восстановлено частично ----
        self.assertEqual(sorted(result.restored_json), sorted(content_store.DATA_FILENAMES))
        self.assertEqual(result.missing_json, [])
        self.assertEqual(result.failed_images, [])
        self.assertIn("img/portfolio/e2e_cover.jpg", result.restored_images)

        services = await content_store.list_services()
        service = next(s for s in services if s["id"] == "E2E_SVC")
        self.assertEqual(service["name"], "ORIGINAL_SERVICE_NAME")

        cases = await content_store.list_cases()
        case = next(c for c in cases if c["id"] == "case_e2e")
        self.assertEqual(case["title"], "ORIGINAL_TITLE")

        about = await content_store.get_about()
        self.assertEqual(about["tagline"], "ORIGINAL_TAGLINE")

        faq_item = next(f for f in await content_store.list_faq() if f["id"] == 1)
        self.assertEqual(faq_item["answer"], "ORIGINAL_A")

        leads_after_restore = (await content_store._read("leads.json"))["leads"]
        lead = next(l for l in leads_after_restore if l["id"] == 1)
        self.assertEqual(lead["status"], "NEW")
        self.assertEqual(lead["payload"]["service_name"], "ORIGINAL_LEAD_MARKER")

        ui_config = await content_store.get_ui_config()
        self.assertTrue(ui_config["menu"]["faq"])

        self.assertEqual((content_store.IMG_PORTFOLIO_DIR / "e2e_cover.jpg").read_bytes(), b"ORIGINAL_IMAGE_BYTES")

        # ---- cross-file referential integrity после restore: case.type ->
        # реальный portfolio type, case.related_service/type.related_service
        # -> реальный pricing service (то же свойство, что Product Readiness
        # audit проверял вручную для реальных production-данных, здесь —
        # автоматизированная regression-проверка на E2E-датасете) ----
        types = await content_store.list_portfolio_types()
        type_ids = {t["id"] for t in types}
        service_ids = {s["id"] for s in services}
        self.assertIn(case["type"], type_ids)
        self.assertIn(case["related_service"], service_ids)
        portfolio_type = next(t for t in types if t["id"] == case["type"])
        self.assertIn(portfolio_type["related_service"], service_ids)


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
            # Остальные обложки переводим с demo_case_N.svg на "реальные" —
            # иначе после fix demo_case_N-детекции (Product Readiness batch,
            # 2026-08-22) они тоже считались бы placeholder, и сводка не была
            # бы отличима от old_summary с тем же числом.
            portfolio = json.loads(fake.store["portfolio.json"])
            for i, case in enumerate(portfolio["cases"]):
                case["cover"] = "img/portfolio/placeholder.svg" if i == 0 else f"img/portfolio/{case['id']}.jpg"
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

    # ---- P1-3, Batch 12: faq_price_answer — тот же TOCTOU-класс, что уже
    # закрыт в admin.py (Batch 6/7/8/10): пикер услуг мог быть показан,
    # пока в faq.json ещё был пункт type=="service_price", а к моменту
    # клика по конкретной услуге этот пункт уже удалён из /admin -> FAQ. ----
    FAKE_PRICING = {"services": [{"id": "LEND", "name": "Лендинг", "base_price": 25000, "term_min": 7, "term_max": 10}]}
    FAKE_FAQ_WITH_TEMPLATE = {
        "faq": [{"id": 1, "type": "service_price", "answer_template": "Цена {name}: от {base_price} ₽, срок {term_min}-{term_max} дн."}]
    }
    FAKE_FAQ_WITHOUT_TEMPLATE = {"faq": [{"id": 2, "type": "static", "answer": "не относится к делу"}]}

    async def test_faq_price_answer_happy_path_with_existing_template(self):
        callback = make_callback("faqprice:LEND")
        with patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ_WITH_TEMPLATE), \
             patch("bot.handlers.faq.load_pricing", return_value=self.FAKE_PRICING):
            await faq.faq_price_answer(callback)
        callback.message.edit_text.assert_awaited_once_with(
            "Цена Лендинг: от 25000 ₽, срок 7-10 дн.", reply_markup=keyboards.faq_price_answer_keyboard(1)
        )
        callback.answer.assert_awaited_once()

    async def test_faq_price_answer_missing_template_does_not_crash(self):
        callback = make_callback("faqprice:LEND")
        with patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ_WITHOUT_TEMPLATE), \
             patch("bot.handlers.faq.load_pricing", return_value=self.FAKE_PRICING):
            await faq.faq_price_answer(callback)  # не должно бросить исключение (было StopIteration)
        callback.answer.assert_awaited_once_with("Вопрос не найден", show_alert=True)
        callback.message.edit_text.assert_not_awaited()  # ничего не отредактировано, данные не тронуты

    async def test_faq_price_answer_missing_service_still_graceful(self):
        # Существующий, не связанный с этим фиксом путь — убеждаемся, что
        # фикс не задел его: неизвестная услуга уже была graceful ДО Batch 12.
        callback = make_callback("faqprice:UNKNOWN")
        with patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ_WITH_TEMPLATE), \
             patch("bot.handlers.faq.load_pricing", return_value=self.FAKE_PRICING):
            await faq.faq_price_answer(callback)
        callback.answer.assert_awaited_once_with("Услуга не найдена", show_alert=True)
        callback.message.edit_text.assert_not_awaited()

    # ---- P1-3, Batch 13: /admin -> Меню и навигация -> "faq" теперь
    # реально enforced на обеих точках входа (/faq, кнопка "❓ Частые
    # вопросы") — раньше выключение флага не имело эффекта (см. аудит
    # Batch 9/11). load_ui_config патчится явно (тот же синхронный
    # локальный источник, что load_faq/load_pricing уже используют в этом
    # файле, не зависит от реального data/ui_config.json). ----
    UI_CONFIG_FAQ_ENABLED = {"menu": {"faq": True}}
    UI_CONFIG_FAQ_DISABLED = {"menu": {"faq": False}}

    async def test_faq_command_enabled_shows_faq_list(self):
        state = make_state()
        msg = make_flow_message(text="/faq")
        with patch("bot.handlers.faq.load_ui_config", return_value=self.UI_CONFIG_FAQ_ENABLED), \
             patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            await faq.cmd_faq(msg, state)
        shown = msg.answer.await_args_list[-1]
        shown_text = shown.args[0] if shown.args else shown.kwargs.get("text")
        self.assertEqual(shown_text, texts.FAQ_INTRO)

    async def test_faq_command_disabled_does_not_enter_faq_flow(self):
        state = make_state()
        msg = make_flow_message(text="/faq")
        with patch("bot.handlers.faq.load_ui_config", return_value=self.UI_CONFIG_FAQ_DISABLED), \
             patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            await faq.cmd_faq(msg, state)
        shown = msg.answer.await_args_list[-1]
        shown_text = shown.args[0] if shown.args else shown.kwargs.get("text")
        self.assertEqual(shown_text, texts.FAQ_DISABLED)
        shown_markup = shown.kwargs.get("reply_markup") if len(shown.args) < 2 else shown.args[1]
        self.assertIsNone(shown_markup)  # никакого FAQ-списка/клавиатуры не показано

    async def test_faq_reply_button_enabled_shows_faq_list(self):
        state = make_state()
        msg = make_flow_message(text=texts.MENU_FAQ)
        with patch("bot.handlers.faq.load_ui_config", return_value=self.UI_CONFIG_FAQ_ENABLED), \
             patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            await faq.show_faq_list(msg, state)
        shown = msg.answer.await_args_list[-1]
        shown_text = shown.args[0] if shown.args else shown.kwargs.get("text")
        self.assertEqual(shown_text, texts.FAQ_INTRO)

    async def test_faq_reply_button_disabled_does_not_enter_faq_flow(self):
        state = make_state()
        msg = make_flow_message(text=texts.MENU_FAQ)
        with patch("bot.handlers.faq.load_ui_config", return_value=self.UI_CONFIG_FAQ_DISABLED), \
             patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            await faq.show_faq_list(msg, state)
        shown = msg.answer.await_args_list[-1]
        shown_text = shown.args[0] if shown.args else shown.kwargs.get("text")
        self.assertEqual(shown_text, texts.FAQ_DISABLED)

    async def test_faq_reenabled_after_disabled_restores_normal_flow(self):
        state = make_state()

        with patch("bot.handlers.faq.load_ui_config", return_value=self.UI_CONFIG_FAQ_DISABLED), \
             patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            disabled_msg = make_flow_message(text=texts.MENU_FAQ)
            await faq.show_faq_list(disabled_msg, state)
        disabled_shown = disabled_msg.answer.await_args_list[-1]
        disabled_text = disabled_shown.args[0] if disabled_shown.args else disabled_shown.kwargs.get("text")
        self.assertEqual(disabled_text, texts.FAQ_DISABLED)

        with patch("bot.handlers.faq.load_ui_config", return_value=self.UI_CONFIG_FAQ_ENABLED), \
             patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            reenabled_msg = make_flow_message(text=texts.MENU_FAQ)
            await faq.show_faq_list(reenabled_msg, state)
        reenabled_shown = reenabled_msg.answer.await_args_list[-1]
        reenabled_text = reenabled_shown.args[0] if reenabled_shown.args else reenabled_shown.kwargs.get("text")
        self.assertEqual(reenabled_text, texts.FAQ_INTRO)  # обычный FAQ-список снова доступен

    async def test_faq_toggle_missing_key_defaults_to_enabled(self):
        # ui_config.json без ключа "faq" вовсе (напр. очень старый бэкап) —
        # тот же .get(key, True) default, что и в admin.py::nav_toggle
        # (Batch 9) — по умолчанию доступно, а не тихо выключено.
        state = make_state()
        msg = make_flow_message(text="/faq")
        with patch("bot.handlers.faq.load_ui_config", return_value={"menu": {}}), \
             patch("bot.handlers.faq.load_faq", return_value=self.FAKE_FAQ):
            await faq.cmd_faq(msg, state)
        shown = msg.answer.await_args_list[-1]
        shown_text = shown.args[0] if shown.args else shown.kwargs.get("text")
        self.assertEqual(shown_text, texts.FAQ_INTRO)


class R2StorageTests(unittest.IsolatedAsyncioTestCase):
    """Batch 3 (persistent media): bot/r2_storage.py — hand-rolled SigV4
    signing + PUT/DELETE against Cloudflare R2's S3-compatible API.
    Canonical-request and signing-key-derivation tests below are cross-
    checked against AWS's own published SigV4 documentation (canonical
    request/CanonicalHeaders example) and an independent from-scratch
    reimplementation of the key-derivation chain — not just "does the code
    agree with itself"."""

    def setUp(self):
        self._orig = (
            r2_storage.config.R2_ACCOUNT_ID, r2_storage.config.R2_ACCESS_KEY_ID,
            r2_storage.config.R2_SECRET_ACCESS_KEY, r2_storage.config.R2_BUCKET_NAME,
            r2_storage.config.R2_PUBLIC_BASE_URL,
        )
        r2_storage.config.R2_ACCOUNT_ID = "test-account-id"
        r2_storage.config.R2_ACCESS_KEY_ID = "test-access-key-id"
        r2_storage.config.R2_SECRET_ACCESS_KEY = "test-secret-access-key"
        r2_storage.config.R2_BUCKET_NAME = "test-bucket"
        r2_storage.config.R2_PUBLIC_BASE_URL = "https://pub-test.r2.dev"

    def tearDown(self):
        (
            r2_storage.config.R2_ACCOUNT_ID, r2_storage.config.R2_ACCESS_KEY_ID,
            r2_storage.config.R2_SECRET_ACCESS_KEY, r2_storage.config.R2_BUCKET_NAME,
            r2_storage.config.R2_PUBLIC_BASE_URL,
        ) = self._orig

    def test_is_configured_true_when_all_vars_set(self):
        self.assertTrue(r2_storage.is_configured())

    def test_is_configured_false_when_any_var_missing(self):
        for attr in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME", "R2_PUBLIC_BASE_URL"):
            original = getattr(r2_storage.config, attr)
            setattr(r2_storage.config, attr, "")
            self.assertFalse(r2_storage.is_configured(), f"expected not configured with {attr} empty")
            setattr(r2_storage.config, attr, original)

    def test_generate_object_key_format(self):
        key = r2_storage.generate_object_key("portfolio", "case_5", ".jpg")
        self.assertRegex(key, r"^portfolio/case_5_[0-9a-f]{8}\.jpg$")

    def test_generate_object_key_unique_per_call(self):
        keys = {r2_storage.generate_object_key("about", "avatar", ".png") for _ in range(20)}
        self.assertEqual(len(keys), 20)  # ни одного совпадения

    # ---- Cross-checked against AWS's published SigV4 documentation ----

    def test_canonical_request_matches_aws_documented_example(self):
        # Пример CanonicalHeaders дословно из AWS SigV4 docs (Elements of an
        # AWS API request signature) — не придуман, взят как есть.
        headers = {
            "host": "examplebucket.s3.amazonaws.com",
            "x-amz-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "x-amz-date": "20130708T220855Z",
        }
        payload_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        canonical_request, signed_headers = r2_storage.build_canonical_request("GET", "/test.txt", headers, payload_hash)
        expected = (
            "GET\n/test.txt\n\n"
            "host:examplebucket.s3.amazonaws.com\n"
            "x-amz-content-sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n"
            "x-amz-date:20130708T220855Z\n\n"
            "host;x-amz-content-sha256;x-amz-date\n"
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )
        self.assertEqual(canonical_request, expected)
        self.assertEqual(signed_headers, "host;x-amz-content-sha256;x-amz-date")

    def test_canonical_headers_are_sorted_regardless_of_input_order(self):
        headers = {
            "x-amz-date": "20260101T000000Z",
            "host": "example.r2.cloudflarestorage.com",
            "x-amz-content-sha256": "abc",
        }
        _, signed_headers = r2_storage.build_canonical_request("PUT", "/bucket/key", headers, "abc")
        self.assertEqual(signed_headers, "host;x-amz-content-sha256;x-amz-date")  # алфавитный порядок, не порядок dict

    def test_signing_key_derivation_matches_independent_reference_implementation(self):
        # Независимая реализация той же цепочки (НЕ импортирует ничего из
        # r2_storage) — по шагам из AWS docs: DateKey -> DateRegionKey ->
        # DateRegionServiceKey -> SigningKey.
        def reference_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
            def h(key: bytes, msg: str) -> bytes:
                return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
            k_date = h(("AWS4" + secret_key).encode("utf-8"), date_stamp)
            k_region = h(k_date, region)
            k_service = h(k_region, service)
            return h(k_service, "aws4_request")

        expected = reference_signing_key("some-secret-key", "20260115", "auto", "s3")
        actual = r2_storage.derive_signing_key("some-secret-key", "20260115")
        self.assertEqual(actual, expected)

    def test_signing_key_changes_with_date(self):
        key1 = r2_storage.derive_signing_key("secret", "20260101")
        key2 = r2_storage.derive_signing_key("secret", "20260102")
        self.assertNotEqual(key1, key2)

    def test_authorization_header_structure(self):
        url, headers = r2_storage.build_authorization_header("PUT", "portfolio/case_1_abcd1234.jpg", b"fake-bytes", {"content-type": "image/jpeg"})
        self.assertEqual(url, "https://test-account-id.r2.cloudflarestorage.com/test-bucket/portfolio/case_1_abcd1234.jpg")
        auth = headers["Authorization"]
        self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 Credential=test-access-key-id/"))
        self.assertIn("/auto/s3/aws4_request, SignedHeaders=", auth)
        self.assertIn("content-type", auth.split("SignedHeaders=")[1])
        self.assertIn(", Signature=", auth)
        signature = auth.rsplit("Signature=", 1)[1]
        self.assertRegex(signature, r"^[0-9a-f]{64}$")  # hex-encoded HMAC-SHA256
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertIn("x-amz-content-sha256", headers)
        self.assertIn("x-amz-date", headers)

    def test_authorization_header_payload_hash_reflects_actual_content(self):
        _, headers_a = r2_storage.build_authorization_header("PUT", "k", b"content-a")
        _, headers_b = r2_storage.build_authorization_header("PUT", "k", b"content-b")
        self.assertNotEqual(headers_a["x-amz-content-sha256"], headers_b["x-amz-content-sha256"])
        self.assertEqual(headers_a["x-amz-content-sha256"], hashlib.sha256(b"content-a").hexdigest())

    # ---- upload_image / delete_image (mocked transport, no real network) ----

    async def test_upload_image_success_returns_public_url(self):
        with patch.object(r2_storage, "_http_request", return_value=(200, b"")):
            url = await r2_storage.upload_image("portfolio/case_1_abcd1234.jpg", b"bytes", "image/jpeg")
        self.assertEqual(url, "https://pub-test.r2.dev/portfolio/case_1_abcd1234.jpg")

    async def test_upload_image_201_also_treated_as_success(self):
        with patch.object(r2_storage, "_http_request", return_value=(201, b"")):
            url = await r2_storage.upload_image("k", b"bytes", "image/jpeg")
        self.assertEqual(url, "https://pub-test.r2.dev/k")

    async def test_upload_image_non_2xx_raises_r2_upload_error(self):
        with patch.object(r2_storage, "_http_request", return_value=(403, b"Forbidden")):
            with self.assertRaises(r2_storage.R2UploadError):
                await r2_storage.upload_image("k", b"bytes", "image/jpeg")

    async def test_upload_image_network_error_raises_r2_upload_error(self):
        with patch.object(r2_storage, "_http_request", side_effect=urllib.error.URLError("simulated network failure")):
            with self.assertRaises(r2_storage.R2UploadError):
                await r2_storage.upload_image("k", b"bytes", "image/jpeg")

    async def test_delete_image_skips_non_r2_url_without_http_call(self):
        with patch.object(r2_storage, "_http_request") as mock_request:
            await r2_storage.delete_image("img/portfolio/demo_case_1.svg")  # legacy relative path
        mock_request.assert_not_called()

    async def test_delete_image_skips_url_from_different_base(self):
        with patch.object(r2_storage, "_http_request") as mock_request:
            await r2_storage.delete_image("https://someone-elses-bucket.r2.dev/portfolio/x.jpg")
        mock_request.assert_not_called()

    async def test_delete_image_skips_when_not_configured(self):
        r2_storage.config.R2_ACCOUNT_ID = ""
        with patch.object(r2_storage, "_http_request") as mock_request:
            await r2_storage.delete_image("https://pub-test.r2.dev/portfolio/x.jpg")
        mock_request.assert_not_called()

    async def test_delete_image_success_no_error_logged(self):
        with patch.object(r2_storage, "_http_request", return_value=(204, b"")):
            with self.assertNoLogs(r2_storage.logger.name, level="ERROR"):
                await r2_storage.delete_image("https://pub-test.r2.dev/portfolio/x.jpg")

    async def test_delete_image_404_treated_as_success_no_error_logged(self):
        # Объект уже отсутствует в R2 (например, повторная попытка) — не
        # считаем это ошибкой, орфан явно не остался.
        with patch.object(r2_storage, "_http_request", return_value=(404, b"Not Found")):
            with self.assertNoLogs(r2_storage.logger.name, level="ERROR"):
                await r2_storage.delete_image("https://pub-test.r2.dev/portfolio/x.jpg")

    async def test_delete_image_failure_logs_error_does_not_raise(self):
        # Batch 3 product decision: "deletion failures must be handled
        # deliberately and reported, not silently hidden" — логируется как
        # ERROR, но НЕ бросает исключение (не должно блокировать основное
        # действие дизайнера, см. content_store.py::remove_case_image и др.)
        with patch.object(r2_storage, "_http_request", return_value=(500, b"Internal Server Error")):
            with self.assertLogs(r2_storage.logger.name, level="ERROR") as log_ctx:
                await r2_storage.delete_image("https://pub-test.r2.dev/portfolio/x.jpg")
        self.assertTrue(any("orphan" in m.lower() for m in log_ctx.output))

    async def test_delete_image_network_error_logs_error_does_not_raise(self):
        with patch.object(r2_storage, "_http_request", side_effect=urllib.error.URLError("simulated network failure")):
            with self.assertLogs(r2_storage.logger.name, level="ERROR") as log_ctx:
                await r2_storage.delete_image("https://pub-test.r2.dev/portfolio/x.jpg")
        self.assertTrue(any("orphan" in m.lower() for m in log_ctx.output))

    async def test_delete_image_extracts_correct_key_from_url(self):
        captured = {}

        def fake_request(method, url, headers, data):
            captured["url"] = url
            return 204, b""

        with patch.object(r2_storage, "_http_request", side_effect=fake_request):
            await r2_storage.delete_image("https://pub-test.r2.dev/portfolio/case_1_abcd1234.jpg")
        self.assertEqual(captured["url"], "https://test-account-id.r2.cloudflarestorage.com/test-bucket/portfolio/case_1_abcd1234.jpg")


class SaveCasePhotoPathSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Security hardening, Batch 3: save_case_photo()'s case_id приходит из
    admin callback data (см. cases_edit_picked в handlers/admin.py) без
    проверки, что такой кейс вообще существует. Path(case_id).name (см.
    fix в content_store.py) гарантирует basename semantics — тот же паттерн,
    что уже используется для zip-slip защиты в import_backup_bytes.

    Доказываем именно filesystem boundary (куда РЕАЛЬНО легла запись через
    fake bot.download_file), а не просто форму возвращаемой строки — фейковый
    download_file ниже физически пишет байты по переданному destination,
    как это делает настоящий aiogram Bot.download_file."""

    def setUp(self):
        self.img_tmpdir = tempfile.mkdtemp()
        self._orig_img_portfolio = content_store.IMG_PORTFOLIO_DIR
        content_store.IMG_PORTFOLIO_DIR = Path(self.img_tmpdir) / "portfolio"
        content_store.IMG_PORTFOLIO_DIR.mkdir(parents=True)
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.config.DESIGNER_CHAT_ID = "555"
        self.actor = "555"

    def tearDown(self):
        content_store.IMG_PORTFOLIO_DIR = self._orig_img_portfolio
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.img_tmpdir, ignore_errors=True)

    def _fake_bot(self, written: list) -> SimpleNamespace:
        async def get_file(file_id):
            return SimpleNamespace(file_path="photos/fake.jpg")

        async def download_file(file_path, destination=None):
            # Реальный aiogram Bot.download_file пишет байты напрямую по
            # destination без какой-либо санитизации со своей стороны —
            # вся защита должна быть внутри save_case_photo() ДО этого вызова.
            # destination=None (Batch 3, R2-путь) — реальный aiogram в этом
            # случае возвращает io.BytesIO с байтами (см. Bot.download_file),
            # ничего на диск не пишет.
            if destination is None:
                return io.BytesIO(b"fake-image-bytes")
            written.append(Path(destination))
            Path(destination).write_bytes(b"fake-image-bytes")
            return None

        return SimpleNamespace(get_file=get_file, download_file=download_file)

    async def test_absolute_path_case_id_stays_within_portfolio_dir(self):
        written: list = []
        bot = self._fake_bot(written)
        # Абсолютный путь на СОСЕДНЮЮ директорию внутри того же tmpdir, НЕ
        # внутри IMG_PORTFOLIO_DIR — Path(base) / "/absolute/path" в pathlib
        # отбрасывает base целиком, если правый операнд абсолютный, поэтому
        # без fix запись ушла бы именно туда.
        outside_dir = Path(self.img_tmpdir) / "outside_marker"
        malicious_case_id = str(outside_dir / "evil")

        cover = await content_store.save_case_photo(self.actor, bot, "fid", malicious_case_id)

        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].resolve().parent, content_store.IMG_PORTFOLIO_DIR.resolve())
        self.assertFalse(outside_dir.exists())  # ничего не создано за пределами IMG_PORTFOLIO_DIR
        self.assertTrue(cover.startswith("img/portfolio/"))

    async def test_traversal_case_id_stays_within_portfolio_dir(self):
        written: list = []
        bot = self._fake_bot(written)

        cover = await content_store.save_case_photo(self.actor, bot, "fid", "../../../evil")

        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].resolve().parent, content_store.IMG_PORTFOLIO_DIR.resolve())
        # ../../../ выше tmpdir ничего не создал — единственный написанный
        # файл лежит ровно внутри IMG_PORTFOLIO_DIR (проверено строкой выше).
        self.assertTrue(cover.startswith("img/portfolio/"))

    async def test_normal_case_id_unchanged(self):
        written: list = []
        bot = self._fake_bot(written)

        cover = await content_store.save_case_photo(self.actor, bot, "fid", "case_5")

        self.assertEqual(cover, "img/portfolio/case_5.jpg")
        self.assertEqual(written[0].resolve(), (content_store.IMG_PORTFOLIO_DIR / "case_5.jpg").resolve())
        self.assertEqual(written[0].read_bytes(), b"fake-image-bytes")

    async def test_gallery_add_case_id_with_uuid_suffix_unchanged(self):
        # case_image_add_receive/case_section_edit_value передают
        # f"{case_id}_{uuid4().hex[:8]}" (см. handlers/admin.py) — обычный
        # безопасный компонент, .name не должен его менять.
        written: list = []
        bot = self._fake_bot(written)

        cover = await content_store.save_case_photo(self.actor, bot, "fid", "case_5_a1b2c3d4")

        self.assertEqual(cover, "img/portfolio/case_5_a1b2c3d4.jpg")

    # ---- Batch 3: R2-configured path (persistent media) ----

    async def test_save_case_photo_uses_r2_when_configured_not_local_disk(self):
        written: list = []
        bot = self._fake_bot(written)
        with patch.object(content_store.r2_storage, "is_configured", return_value=True), \
             patch.object(
                 content_store.r2_storage, "upload_image",
                 new=AsyncMock(return_value="https://pub-test.r2.dev/portfolio/case_5_deadbeef.jpg"),
             ) as mock_upload:
            cover = await content_store.save_case_photo(self.actor, bot, "fid", "case_5")

        self.assertEqual(cover, "https://pub-test.r2.dev/portfolio/case_5_deadbeef.jpg")
        self.assertEqual(written, [])  # ничего не записано на локальный диск
        mock_upload.assert_awaited_once()
        key_arg, content_arg, content_type_arg = mock_upload.await_args.args
        self.assertTrue(key_arg.startswith("portfolio/case_5_"))
        self.assertTrue(key_arg.endswith(".jpg"))
        self.assertEqual(content_arg, b"fake-image-bytes")
        self.assertEqual(content_type_arg, "image/jpeg")

    async def test_save_case_photo_r2_failure_propagates_no_local_fallback(self):
        # Batch 3 product decision: R2 сконфигурирован, но загрузка не
        # удалась -> R2UploadError долетает как есть, НИКАКОГО тихого
        # fallback на локальный диск (иначе portfolio.json указывал бы на
        # ложно-успешный локальный файл, реально не переживающий redeploy).
        written: list = []
        bot = self._fake_bot(written)
        with patch.object(content_store.r2_storage, "is_configured", return_value=True), \
             patch.object(
                 content_store.r2_storage, "upload_image",
                 new=AsyncMock(side_effect=r2_storage.R2UploadError("simulated R2 failure")),
             ):
            with self.assertRaises(r2_storage.R2UploadError):
                await content_store.save_case_photo(self.actor, bot, "fid", "case_5")

        self.assertEqual(written, [])  # точно не было тихой записи на диск

    async def test_save_about_photo_uses_r2_when_configured(self):
        written: list = []
        bot = self._fake_bot(written)
        with patch.object(content_store.r2_storage, "is_configured", return_value=True), \
             patch.object(
                 content_store.r2_storage, "upload_image",
                 new=AsyncMock(return_value="https://pub-test.r2.dev/about/avatar_deadbeef.png"),
             ) as mock_upload:
            path = await content_store.save_about_photo(self.actor, bot, "fid")

        self.assertEqual(path, "https://pub-test.r2.dev/about/avatar_deadbeef.png")
        self.assertEqual(written, [])
        key_arg = mock_upload.await_args.args[0]
        self.assertTrue(key_arg.startswith("about/avatar_"))

    async def test_save_about_photo_r2_failure_propagates_no_local_fallback(self):
        written: list = []
        bot = self._fake_bot(written)
        with patch.object(content_store.r2_storage, "is_configured", return_value=True), \
             patch.object(
                 content_store.r2_storage, "upload_image",
                 new=AsyncMock(side_effect=r2_storage.R2UploadError("simulated R2 failure")),
             ):
            with self.assertRaises(r2_storage.R2UploadError):
                await content_store.save_about_photo(self.actor, bot, "fid")

        self.assertEqual(written, [])


class AdminImageUploadMimeValidationTests(unittest.IsolatedAsyncioTestCase):
    """Batch 3, finding B3-4 — минимальная content-type проверка на всех
    5 upload entry points (F.photo | F.document): photo всегда настоящее
    изображение (Telegram сам конвертирует в JPEG), document — произвольный
    файл, mime_type может быть не image/*. Прямые unit-тесты самой проверки
    ниже + по одному handler-level тесту на каждый из двух вариантов сайта
    (без предварительной "ничего не прислано" проверки и с ней) — логика
    одна и та же (_is_valid_image_upload) на всех 5, дублировать интеграционный
    тест на каждый сайт отдельно означало бы просто повторно проверять
    copy-paste, не новую логику."""

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

    # ---- _is_valid_image_upload() unit tests ----

    def test_photo_is_always_valid(self):
        msg = make_photo_message(self.actor)
        self.assertTrue(admin._is_valid_image_upload(msg))

    def test_document_with_image_mime_is_valid(self):
        msg = make_non_image_document_message(self.actor, mime_type="image/png")
        self.assertTrue(admin._is_valid_image_upload(msg))

    def test_document_with_pdf_mime_is_invalid(self):
        msg = make_non_image_document_message(self.actor, mime_type="application/pdf")
        self.assertFalse(admin._is_valid_image_upload(msg))

    def test_document_with_no_mime_type_is_invalid(self):
        msg = make_non_image_document_message(self.actor, mime_type=None)
        self.assertFalse(admin._is_valid_image_upload(msg))

    def test_neither_photo_nor_document_is_invalid(self):
        msg = SimpleNamespace(photo=None, document=None)
        self.assertFalse(admin._is_valid_image_upload(msg))

    # ---- handler-level: site WITHOUT a pre-existing "nothing sent" check ----

    async def test_about_edit_photo_rejects_non_image_document(self):
        await content_store.add_case(  # not needed by about, but keeps setUp uniform across the file
            self.actor, case_id="case_unused", title="T", type_id="landing", cover="img/portfolio/a.svg", task="t", related_service=None,
        )
        state = make_state(self.actor)
        await state.set_state(AdminStates.edit_about_photo)
        msg = make_non_image_document_message(self.actor, mime_type="application/pdf")
        with patch.object(content_store, "save_about_photo", new=AsyncMock()) as mock_save:
            await admin.about_edit_photo(msg, state)
        mock_save.assert_not_awaited()
        # step_from_text без предустановленного anchor падает в message.answer
        # (см. bot/flow.py::step_from_text) — не edit_message_text.
        msg.answer.assert_awaited_once()
        self.assertIn("изображение", msg.answer.await_args.args[0].lower())

    # ---- handler-level: site WITH a pre-existing "nothing sent" check ----

    async def test_cases_edit_value_cover_rejects_non_image_document(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.edit_case_value)
        await state.update_data(case_id="case_x", field="cover")
        msg = make_non_image_document_message(self.actor, mime_type="application/pdf")
        with patch.object(content_store, "save_case_photo", new=AsyncMock()) as mock_save:
            await admin.cases_edit_value(msg, state)
        mock_save.assert_not_awaited()
        msg.answer.assert_awaited_once()
        self.assertIn("изображение", msg.answer.await_args.args[0].lower())

    async def test_cases_add_photo_rejects_non_image_document(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.add_case_photo)
        await state.update_data(case_id="case_new")
        msg = make_non_image_document_message(self.actor, mime_type="text/plain")
        with patch.object(content_store, "save_case_photo", new=AsyncMock()) as mock_save:
            await admin.cases_add_photo(msg, state)
        mock_save.assert_not_awaited()


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
        # Текущая реальная data/portfolio.json — все 10 кейсов всё ещё
        # demo (img/portfolio/demo_case_N.svg), FAQ полностью отвечен;
        # About.education/links намеренно оставлены пустыми — банер
        # всё ещё должен предупреждать именно про них (Product Readiness
        # audit, 2026-08-22 — обновлено вместе с fix demo_case_N-детекции).
        summary = await content_store.content_readiness_summary()
        self.assertEqual(summary["placeholder_cases"], 10)
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

    # ---- Product Readiness audit, 2026-08-22: demo_case_N.svg detection ----

    async def test_demo_case_cover_is_counted_as_placeholder(self):
        # img/portfolio/demo_case_N.svg — фактическое имя seed-обложек
        # (data/portfolio.json), не содержит подстроку "placeholder", раньше
        # ускользало от detection полностью (найдено Product Readiness audit).
        cases = await content_store.list_cases()
        case_id = cases[0]["id"]
        await content_store.update_case(self.actor, case_id, cover="img/portfolio/demo_case_1.svg")
        for c in cases[1:]:
            await content_store.update_case(self.actor, c["id"], cover="img/portfolio/real_photo.jpg")

        summary = await content_store.content_readiness_summary()
        self.assertEqual(summary["placeholder_cases"], 1)

    async def test_real_looking_cover_is_not_counted(self):
        # Реальные загруженные обложки идут через save_case_photo() как
        # img/portfolio/{case_id}{ext} (case_id всегда "case_N", см.
        # next_case_id) — этот паттерн не должен ловиться ни старой
        # "placeholder"-подстрокой, ни новым demo_case_N-паттерном.
        for c in await content_store.list_cases():
            await content_store.update_case(self.actor, c["id"], cover=f"img/portfolio/{c['id']}.jpg")

        summary = await content_store.content_readiness_summary()
        self.assertEqual(summary["placeholder_cases"], 0)

    async def test_generic_placeholder_substring_still_detected(self):
        # Существующее поведение (широкая подстрока "placeholder") должно
        # остаться нетронутым — fix только ДОБАВЛЯЕТ detection для
        # demo_case_N, не заменяет старую проверку.
        cases = await content_store.list_cases()
        await content_store.update_case(self.actor, cases[0]["id"], cover="img/portfolio/placeholder.svg")
        for c in cases[1:]:
            await content_store.update_case(self.actor, c["id"], cover="img/portfolio/real_photo.jpg")

        summary = await content_store.content_readiness_summary()
        self.assertEqual(summary["placeholder_cases"], 1)


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

    # ---- Batch 3: R2 cleanup on removal/replacement ----

    async def test_remove_case_image_calls_r2_delete_with_removed_path(self):
        await content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            await content_store.remove_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        mock_delete.assert_awaited_once_with("img/portfolio/b.svg")

    async def test_update_case_cover_replacement_deletes_old_cover(self):
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            await content_store.update_case(self.actor, "case_img_test", cover="https://pub-test.r2.dev/portfolio/new.jpg")
        mock_delete.assert_awaited_once_with("img/portfolio/a.svg")  # старый cover, не новый

    async def test_update_case_setting_same_cover_does_not_call_delete(self):
        # Regression guard: не должно случайно удалять текущий, всё ещё
        # действующий cover, если он просто "переустановлен" тем же значением.
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            await content_store.update_case(self.actor, "case_img_test", cover="img/portfolio/a.svg")
        mock_delete.assert_not_awaited()

    async def test_update_case_unrelated_field_does_not_call_delete(self):
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            await content_store.update_case(self.actor, "case_img_test", title="Новый заголовок")
        mock_delete.assert_not_awaited()

    async def test_delete_case_calls_r2_delete_for_top_level_and_section_images(self):
        await content_store.add_case_image(self.actor, "case_img_test", "img/portfolio/b.svg")
        await content_store.add_case_section(
            self.actor, "case_img_test", section_type="gallery", title="Галерея",
            images=["img/portfolio/gallery1.svg", "img/portfolio/gallery2.svg"],
        )
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            self.assertTrue(await content_store.delete_case(self.actor, "case_img_test"))
        deleted_paths = {call.args[0] for call in mock_delete.await_args_list}
        self.assertEqual(
            deleted_paths,
            {"img/portfolio/a.svg", "img/portfolio/b.svg", "img/portfolio/gallery1.svg", "img/portfolio/gallery2.svg"},
        )


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

    # ---- Batch 3: R2 cleanup for section images ----

    async def test_delete_case_section_calls_r2_delete_for_its_images(self):
        await content_store.add_case_section(
            self.actor, "case_sec_test", section_type="gallery", title="Скриншоты",
            images=["img/portfolio/x.svg", "img/portfolio/y.svg"],
        )
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            self.assertTrue(await content_store.delete_case_section(self.actor, "case_sec_test", 0))
        deleted_paths = {call.args[0] for call in mock_delete.await_args_list}
        self.assertEqual(deleted_paths, {"img/portfolio/x.svg", "img/portfolio/y.svg"})

    async def test_delete_text_section_does_not_call_r2_delete(self):
        await content_store.add_case_section(self.actor, "case_sec_test", section_type="text", title="Т", content="C")
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            self.assertTrue(await content_store.delete_case_section(self.actor, "case_sec_test", 0))
        mock_delete.assert_not_awaited()

    async def test_remove_case_section_image_deletes_from_r2_and_updates_json(self):
        await content_store.add_case_section(
            self.actor, "case_sec_test", section_type="gallery", title="Скриншоты",
            images=["img/portfolio/x.svg", "img/portfolio/y.svg"],
        )
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            self.assertTrue(await content_store.remove_case_section_image(self.actor, "case_sec_test", 0, 0))
        mock_delete.assert_awaited_once_with("img/portfolio/x.svg")
        section = (await self._case())["sections"][0]
        self.assertEqual(section["images"], ["img/portfolio/y.svg"])

    async def test_remove_case_section_image_out_of_bounds_returns_false_no_delete(self):
        await content_store.add_case_section(
            self.actor, "case_sec_test", section_type="gallery", title="Скриншоты", images=["img/portfolio/x.svg"],
        )
        with patch.object(content_store.r2_storage, "delete_image", new=AsyncMock()) as mock_delete:
            self.assertFalse(await content_store.remove_case_section_image(self.actor, "case_sec_test", 0, 5))
        mock_delete.assert_not_awaited()


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

    # ---- Batch 2: Closed lead lifecycle (DONE/CANCELLED hard-block) ----

    async def test_update_lead_status_to_done_clears_awaiting_tz_file(self):
        lead = await content_store.add_lead({"service_name": "A", "attach_tz": True}, self.telegram)
        self.assertTrue((await content_store.get_lead(lead["id"]))["awaiting_tz_file"])

        await content_store.update_lead_status(self.actor, lead["id"], "DONE")

        closed = await content_store.get_lead(lead["id"])
        self.assertFalse(closed["awaiting_tz_file"])
        self.assertIsNone(closed["awaiting_tz_file_source"])

    async def test_update_lead_status_to_cancelled_clears_awaiting_tz_file(self):
        lead = await content_store.add_lead({"service_name": "A", "attach_tz": True}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "CANCELLED")

        closed = await content_store.get_lead(lead["id"])
        self.assertFalse(closed["awaiting_tz_file"])
        self.assertIsNone(closed["awaiting_tz_file_source"])

    async def test_update_lead_status_to_non_terminal_does_not_touch_awaiting_tz_file(self):
        # Regression guard: только DONE/CANCELLED должны сбрасывать флаг —
        # обычный переход между активными статусами не должен его трогать.
        lead = await content_store.add_lead({"service_name": "A", "attach_tz": True}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "IN_PROGRESS")

        still_active = await content_store.get_lead(lead["id"])
        self.assertTrue(still_active["awaiting_tz_file"])
        self.assertEqual(still_active["awaiting_tz_file_source"], "new")

    async def test_find_lead_awaiting_file_excludes_closed_lead_even_with_stale_flag(self):
        # Симулирует legacy-заявку: закрыта ДО этого исправления, флаг мог
        # остаться true с более раннего момента (см. content_store.
        # find_lead_awaiting_file докстринг). Прямая правка через
        # update_lead_status не подходит — она теперь сама снимает флаг;
        # здесь нужен именно "уже закрыта, но флаг ещё стоит" сценарий.
        lead = await content_store.add_lead({"service_name": "A", "attach_tz": True}, self.telegram)
        async with content_store._lock("leads.json"):
            leads = await content_store._read_leads()
            stored = next(l for l in leads if l["id"] == lead["id"])
            stored["status"] = "DONE"  # закрыта напрямую, минуя update_lead_status
            await content_store._write_leads(leads)

        self.assertIsNone(await content_store.find_lead_awaiting_file(self.telegram["user_id"]))

    async def test_record_lead_material_returns_false_for_closed_lead(self):
        # Прямой вызов (минуя find_lead_awaiting_file) — воспроизводит
        # гонку "лид был активен на момент проверки, закрылся к моменту
        # записи" (см. implementation plan, §7.2/7.3).
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "CANCELLED")

        result = await content_store.record_lead_material(lead["id"], "file-1", "unique-1", "document", "new")

        self.assertFalse(result)
        self.assertEqual((await content_store.get_lead(lead["id"]))["materials"], [])

    async def test_add_lead_supplement_raises_for_done_lead(self):
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "DONE")

        with self.assertRaises(content_store.LeadClosedError):
            await content_store.add_lead_supplement(lead["id"], self.telegram, {"comment": "ещё кое-что"})

        self.assertEqual((await content_store.get_lead(lead["id"]))["supplements"], [])

    async def test_add_lead_supplement_raises_for_cancelled_lead(self):
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "CANCELLED")

        with self.assertRaises(content_store.LeadClosedError):
            await content_store.add_lead_supplement(lead["id"], self.telegram, {"comment": "ещё кое-что"})

    async def test_add_lead_draft_upsert_raises_for_closed_lead(self):
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram, draft_id="d-closed-1")
        await content_store.update_lead_status(self.actor, lead["id"], "DONE")

        with self.assertRaises(content_store.LeadClosedError):
            await content_store.add_lead(
                {"service_name": "A, изменённое"}, self.telegram, draft_id="d-closed-1",
            )

        unchanged = await content_store.get_lead(lead["id"])
        self.assertEqual(unchanged["payload"]["service_name"], "A")  # апсерт не применился

    async def test_close_then_reopen_then_supplement_succeeds(self):
        # Прямое доказательство "reopening restores normal client
        # interaction path" на уровне content_store (см. implementation
        # plan §4) — без auto-reopen, только явная смена статуса.
        lead = await content_store.add_lead({"service_name": "A"}, self.telegram)
        await content_store.update_lead_status(self.actor, lead["id"], "DONE")
        with self.assertRaises(content_store.LeadClosedError):
            await content_store.add_lead_supplement(lead["id"], self.telegram, {"comment": "пока закрыта"})

        await content_store.update_lead_status(self.actor, lead["id"], "IN_PROGRESS")
        reopened_lead, supplement_id = await content_store.add_lead_supplement(
            lead["id"], self.telegram, {"comment": "уже после reopen"},
        )

        self.assertEqual(supplement_id, 1)
        self.assertEqual(reopened_lead["supplements"][0]["fields"]["comment"], "уже после reopen")


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

        # P1-3, Batch 13: тот же принцип, что уже применён в
        # test_updated_old_lead_rises_above_newer_untouched_lead выше (для
        # list_leads_by_user) — два datetime.now(timezone.utc) вызова подряд
        # иногда совпадают до используемого разрешения системных часов,
        # из-за чего updated_at старой заявки может оказаться РАВЕН
        # created_at новой, и тай-брейк по id отдаёт победу новой заявке —
        # эта, admin-сторонняя версия того же теста была пропущена при том
        # фиксе и оставалась flaky (наблюдалось прямо в full suite). Форсируем
        # заведомо более позднюю метку — сама сортировка (list_leads) при
        # этом по-прежнему реальная, не замокана.
        leads = await content_store._read_leads()
        for l in leads:
            if l["id"] == old_lead["id"]:
                l["updated_at"] = "2030-01-01T00:00:00+00:00"
        await content_store._write_leads(leads)

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

    async def test_designer_chat_id_unset_lead_still_created_no_crash_no_notification(self):
        # Coverage gap identified in the Product Readiness audit: the
        # documented graceful-degrade branch in _handle_lead_create
        # (bot/webserver.py) — "if config.DESIGNER_CHAT_ID: ... else:
        # logger.warning(...)" — had no test proving it actually degrades
        # gracefully instead of crashing or silently dropping the lead
        # itself (only the notification should be skipped).
        from aiohttp.test_utils import TestClient, TestServer

        orig_designer = webserver.config.DESIGNER_CHAT_ID
        webserver.config.DESIGNER_CHAT_ID = ""
        try:
            fake_bot = AsyncMock()
            app = webserver.create_app(fake_bot)
            async with TestClient(TestServer(app)) as client:
                with self.assertLogs("bot.webserver", level="WARNING") as log_ctx:
                    resp = await client.post(
                        "/api/leads",
                        headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                        json={"service_name": "Лендинг", "attach_tz": False},
                    )
                self.assertEqual(resp.status, 200)  # сервер не падает
                body = await resp.json()
                self.assertIn("lead_id", body)
        finally:
            webserver.config.DESIGNER_CHAT_ID = orig_designer

        leads = await content_store.list_leads_by_user(42)
        self.assertEqual(len(leads), 1)  # заявка всё равно создана
        fake_bot.send_message.assert_not_awaited()  # но уведомление не отправлено
        self.assertTrue(any("DESIGNER_CHAT_ID" in msg for msg in log_ctx.output))  # предупреждение залогировано

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

    async def test_draft_id_upsert_against_closed_lead_is_409(self):
        # Batch 2 — устаревший draft_id из localStorage клиента совпал с
        # уже закрытой (DONE/CANCELLED) заявкой (см. implementation plan
        # §7.1: тот же класс проблемы, что и у mode="supplement", только
        # через draft_id-апсерт, а не lead_id).
        from aiohttp.test_utils import TestClient, TestServer

        orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.config.DESIGNER_CHAT_ID = "777"
        try:
            app = webserver.create_app(AsyncMock())
            async with TestClient(TestServer(app)) as client:
                first = await client.post(
                    "/api/leads",
                    headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                    json={"service_name": "Лендинг", "draft_id": "d-closed-http-1"},
                )
                lead_id = (await first.json())["lead_id"]
                await content_store.update_lead_status("777", lead_id, "DONE")

                second = await client.post(
                    "/api/leads",
                    headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                    json={"service_name": "Лендинг, попытка изменить после закрытия", "draft_id": "d-closed-http-1"},
                )
                self.assertEqual(second.status, 409)
                self.assertEqual(await second.json(), {"error": "lead_closed"})

            lead = await content_store.get_lead(lead_id)
            self.assertEqual(lead["payload"]["service_name"], "Лендинг")  # апсерт не применился
        finally:
            content_store.config.DESIGNER_CHAT_ID = orig_designer

    async def test_draft_id_upsert_against_open_lead_still_works(self):
        # Regression guard рядом с closed-lead тестом выше — открытые лиды
        # (любой активный статус) должны продолжать апсертиться как раньше.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            first = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"service_name": "Лендинг", "draft_id": "d-open-http-1"},
            )
            lead_id = (await first.json())["lead_id"]

            second = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"service_name": "Лендинг, правки", "draft_id": "d-open-http-1"},
            )
            self.assertEqual(second.status, 200)

        lead = await content_store.get_lead(lead_id)
        self.assertEqual(lead["payload"]["service_name"], "Лендинг, правки")

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

    # ---- Batch 2: Closed lead lifecycle — HTTP layer (409 lead_closed) ----

    async def test_supplement_to_done_lead_is_409(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            await content_store.update_lead_status("777", lead_id, "DONE")
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "поздно"}},
            )
            self.assertEqual(resp.status, 409)
            self.assertEqual(await resp.json(), {"error": "lead_closed"})

        self.assertEqual((await content_store.get_lead(lead_id))["supplements"], [])

    async def test_supplement_to_cancelled_lead_is_409(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            await content_store.update_lead_status("777", lead_id, "CANCELLED")
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "поздно"}},
            )
            self.assertEqual(resp.status, 409)
            self.assertEqual(await resp.json(), {"error": "lead_closed"})

    async def test_wants_file_on_closed_lead_is_blocked_via_409(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            await content_store.update_lead_status("777", lead_id, "DONE")
            resp = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {}, "wants_file": True},
            )
            self.assertEqual(resp.status, 409)

        lead = await content_store.get_lead(lead_id)
        self.assertFalse(lead["awaiting_tz_file"])  # wants_file не применился

    async def test_supplement_no_designer_notification_sent_for_closed_lead(self):
        # 409 приходит ДО любой попытки уведомить дизайнера — закрытая
        # заявка не должна генерировать новую активность designer-стороны
        # (см. implementation plan: "closed leads must not generate new
        # supplement/material activity").
        from aiohttp.test_utils import TestClient, TestServer

        fake_bot = AsyncMock()
        app = webserver.create_app(fake_bot)
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            await content_store.update_lead_status("777", lead_id, "DONE")
            fake_bot.send_message.reset_mock()  # сбрасываем уведомление о создании заявки
            await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "поздно"}},
            )

        fake_bot.send_message.assert_not_awaited()

    async def test_close_then_reopen_then_supplement_succeeds_via_http(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            lead_id = await self._create_lead(client)
            await content_store.update_lead_status("777", lead_id, "DONE")
            blocked = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "пока закрыта"}},
            )
            self.assertEqual(blocked.status, 409)

            await content_store.update_lead_status("777", lead_id, "WAITING_CLIENT")
            reopened = await client.post(
                "/api/leads",
                headers={"X-Telegram-Init-Data": self._init_data(), "Content-Type": "application/json"},
                json={"mode": "supplement", "lead_id": lead_id, "fields": {"comment": "после reopen"}},
            )
            self.assertEqual(reopened.status, 200)

        lead = await content_store.get_lead(lead_id)
        self.assertEqual(len(lead["supplements"]), 1)
        self.assertEqual(lead["supplements"][0]["fields"]["comment"], "после reopen")


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

    async def test_cache_control_is_no_store_for_js_and_css(self):
        # Тот же _no_cache middleware, что и /data/ выше, но для /js/ и /css/
        # префиксов — раньше не проверялось ни для одного из них: WebView в
        # Telegram-клиенте мог годами отдавать app.js/style.css из
        # собственного диск-кэша без единого запроса к серверу (см.
        # _no_cache докстринг в webserver.py).
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            for path in ("/js/app.js", "/css/style.css"):
                resp = await client.get(path)
                self.assertEqual(resp.status, 200, f"{path} should be served")
                self.assertEqual(resp.headers.get("Cache-Control"), "no-store, no-cache, must-revalidate", path)


class WebServerCoreRoutesTests(unittest.IsolatedAsyncioTestCase):
    """P1-3, Batch 14: /health и Mini App shell (handle_index) — раньше не
    были покрыты НИ ОДНИМ автоматическим тестом; единственная проверка была
    curl'ом ПОСЛЕ каждого деплоя (см. финальные отчёты предыдущих batch'ей),
    то есть поломка обнаружилась бы только постфактум в production, а не в
    CI. handle_index не читает content_store (отдаёт webapp/index.html
    напрямую с диска), поэтому, в отличие от PublicDataRouteTests выше,
    изолированный tmpdir/DATA_DIR здесь не нужен."""

    async def test_health_returns_ok(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/health")
            self.assertEqual(resp.status, 200)
            body = await resp.text()
            self.assertEqual(body, "ok")

    async def test_index_serves_mini_app_shell(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/")
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store, no-cache, must-revalidate")
            body = await resp.text()
            self.assertIn('id="app"', body)  # реальный mount point Mini App (см. webapp/index.html), не абы какой HTML

    async def test_all_mini_app_routes_serve_the_same_shell(self):
        # Все 6 путей регистрируются на один и тот же handle_index (см.
        # webserver.create_app) — какой конкретно экран открыть, решает
        # ТОЛЬКО клиентский JS по window.location.pathname (см. app.js::
        # init()), не сервер. Здесь достаточно подтвердить, что каждый путь
        # реально смаршрутизирован (200) — тело/заголовки уже подробно
        # проверены выше для "/", повторять это на каждом пути избыточно.
        from aiohttp.test_utils import TestClient, TestServer

        app = webserver.create_app(AsyncMock())
        async with TestClient(TestServer(app)) as client:
            for path in ("/", "/portfolio", "/about", "/calculator", "/brief", "/myleads"):
                resp = await client.get(path)
                self.assertEqual(resp.status, 200, f"{path} should route to the Mini App shell")


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
        self._orig_designer = content_store.config.DESIGNER_CHAT_ID
        content_store.DATA_DIR = Path(self.tmpdir)
        content_store.config.DESIGNER_CHAT_ID = "777"

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
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

    # ---- Stage B: video/animation join document/photo, same metadata model ----

    async def test_photo_material_attaches_to_correct_lead(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m-photo"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]

        message.photo = [make_fake_document(file_id="photo-1", file_unique_id="photo-uniq-1")]
        await webapp.handle_tz_file(message)

        lead = await content_store.get_lead(lead_id)
        self.assertEqual(len(lead["materials"]), 1)
        self.assertEqual(lead["materials"][0]["file_id"], "photo-1")
        self.assertEqual(lead["materials"][0]["kind"], "photo")

    async def test_video_material_attaches_to_correct_lead(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m-video"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]

        message.video = make_fake_document(file_id="video-1", file_unique_id="video-uniq-1")
        await webapp.handle_tz_file(message)

        lead = await content_store.get_lead(lead_id)
        self.assertEqual(len(lead["materials"]), 1)
        self.assertEqual(lead["materials"][0]["file_id"], "video-1")
        self.assertEqual(lead["materials"][0]["file_unique_id"], "video-uniq-1")
        self.assertEqual(lead["materials"][0]["kind"], "video")

    async def test_animation_material_attaches_to_correct_lead(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m-anim"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]

        message.animation = make_fake_document(file_id="gif-1", file_unique_id="gif-uniq-1")
        await webapp.handle_tz_file(message)

        lead = await content_store.get_lead(lead_id)
        self.assertEqual(len(lead["materials"]), 1)
        self.assertEqual(lead["materials"][0]["file_id"], "gif-1")
        self.assertEqual(lead["materials"][0]["kind"], "animation")

    # ---- Stage B: actual delivery to DESIGNER_CHAT_ID, not just metadata ----
    # (coverage gap identified in Stage A: prior tests only asserted
    # content_store state, never that message.bot.send_message/forward were
    # actually called towards the designer)

    async def _assert_delivered_to_designer(self, message: SimpleNamespace, lead_id: int) -> None:
        message.bot.send_message.assert_awaited_once()
        self.assertEqual(message.bot.send_message.await_args.kwargs["chat_id"], "777")
        self.assertIn(f"#{lead_id}", message.bot.send_message.await_args.kwargs["text"])
        message.forward.assert_awaited_once_with(chat_id="777")

    async def test_document_material_is_delivered_to_designer(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d-doc"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]
        message.bot.send_message.reset_mock()

        message.document = make_fake_document()
        await webapp.handle_tz_file(message)
        await self._assert_delivered_to_designer(message, lead_id)

    async def test_photo_material_is_delivered_to_designer(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d-photo"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]
        message.bot.send_message.reset_mock()

        message.photo = [make_fake_document()]
        await webapp.handle_tz_file(message)
        await self._assert_delivered_to_designer(message, lead_id)

    async def test_video_material_is_delivered_to_designer(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d-video"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]
        message.bot.send_message.reset_mock()

        message.video = make_fake_document()
        await webapp.handle_tz_file(message)
        await self._assert_delivered_to_designer(message, lead_id)

    async def test_animation_material_is_delivered_to_designer(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "d-anim"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]
        message.bot.send_message.reset_mock()

        message.animation = make_fake_document()
        await webapp.handle_tz_file(message)
        await self._assert_delivered_to_designer(message, lead_id)

    # ---- Stage C Batch 1, Finding 2: unsupported media while awaiting a file ----
    # (voice/video_note/sticker — narrow fallback, see handle_unsupported_tz_media)

    async def test_voice_while_awaiting_file_gets_unsupported_message(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "u-voice"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]
        message.answer.reset_mock()
        message.bot.send_message.reset_mock()  # _handle_brief_submission уже уведомил дизайнера о новой заявке

        message.voice = make_fake_document()
        await webapp.handle_unsupported_tz_media(message)

        message.answer.assert_awaited_once_with(texts.TZ_FILE_UNSUPPORTED_TYPE)
        message.bot.send_message.assert_not_awaited()  # ничего не пересылается дизайнеру
        message.forward.assert_not_awaited()
        lead = await content_store.get_lead(lead_id)
        self.assertEqual(lead.get("materials", []), [])  # не записано как материал
        self.assertTrue(lead["awaiting_tz_file"])  # флаг не снят

    async def test_video_note_while_awaiting_file_gets_unsupported_message(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "u-vnote"})
        message.answer.reset_mock()

        message.video_note = make_fake_document()
        await webapp.handle_unsupported_tz_media(message)

        message.answer.assert_awaited_once_with(texts.TZ_FILE_UNSUPPORTED_TYPE)

    async def test_sticker_while_awaiting_file_gets_unsupported_message(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "u-sticker"})
        message.answer.reset_mock()

        message.sticker = make_fake_document()
        await webapp.handle_unsupported_tz_media(message)

        message.answer.assert_awaited_once_with(texts.TZ_FILE_UNSUPPORTED_TYPE)

    async def test_unsupported_media_when_not_awaiting_file_does_nothing(self):
        # Нет заявки, ожидающей файл, вообще — тот же no-op, что уже
        # сегодня для document/photo/video/animation в этой же ситуации
        # (handle_tz_file, найти лид -> None -> return).
        message = make_message()
        message.voice = make_fake_document()

        await webapp.handle_unsupported_tz_media(message)

        message.answer.assert_not_awaited()

    async def test_designer_own_voice_message_not_captured(self):
        # У дизайнера (тот же chat_id, что и DESIGNER_CHAT_ID) в норме нет
        # своей заявки, ожидающей файл — тот же guard (find_lead_awaiting_file
        # по from_user.id), что уже защищает handle_tz_file, без отдельной
        # DESIGNER_CHAT_ID-проверки.
        message = make_message()
        message.from_user = SimpleNamespace(id=777, username="owner", first_name="Дизайнер", last_name=None)
        message.voice = make_fake_document()

        await webapp.handle_unsupported_tz_media(message)

        message.answer.assert_not_awaited()

    async def test_awaiting_state_cleared_after_material_received(self):
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m2"})
        self.assertIsNotNone(await content_store.find_lead_awaiting_file(message.from_user.id))

        message.document = make_fake_document()
        await webapp.handle_tz_file(message)
        self.assertIsNone(await content_store.find_lead_awaiting_file(message.from_user.id))

    async def test_cancel_clears_awaiting_state_and_next_file_is_not_attributed(self):
        # Coverage gap identified in the Product Readiness audit: /cancel's
        # awaiting-file-clear path (start.cmd_cancel -> content_store.
        # mark_tz_file_received) had zero direct test coverage — every
        # existing cmd_cancel test used a chat with no awaiting lead at all.
        message = make_message()  # from_user.id == 1
        await webapp._handle_brief_submission(
            message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "cancel1"},
        )
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]

        # 1-2. клиент ждёт файл, шлёт /cancel (chat_id=1 == from_user.id
        # выше — в приватном чате с ботом они всегда совпадают, см.
        # make_flow_message).
        state = make_state(1)
        cancel_msg = make_flow_message(chat_id=1, text="/cancel")
        await start.cmd_cancel(cancel_msg, state)

        # 3. awaiting-file state очищено — и на уровне lookup, и на самой заявке.
        self.assertIsNone(await content_store.find_lead_awaiting_file(message.from_user.id))
        lead = await content_store.get_lead(lead_id)
        self.assertFalse(lead["awaiting_tz_file"])
        self.assertIsNone(lead["awaiting_tz_file_source"])
        self.assertEqual(lead.get("materials", []), [])  # /cancel не пишет материал (см. mark_tz_file_received)

        # 4. следующий присланный файл больше не привязывается к этой
        # (уже отменённой) заявке — handle_tz_file ищет через
        # find_lead_awaiting_file и, не найдя ожидающую заявку, ничего не делает.
        message.document = make_fake_document(file_id="after-cancel", file_unique_id="after-cancel-u")
        await webapp.handle_tz_file(message)
        lead_after = await content_store.get_lead(lead_id)
        self.assertEqual(lead_after.get("materials", []), [])

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

    # ---- Batch 2: closing a lead cuts off the file-attachment path too ----

    async def test_file_sent_after_lead_closed_is_not_recorded_or_acknowledged(self):
        # Полная цепочка bot-chat стороны: заявка ждёт файл -> дизайнер
        # закрывает её ДО того, как клиент успел прислать файл -> file
        # больше не находит ожидающую заявку (find_lead_awaiting_file
        # исключает закрытые) -> handle_tz_file no-op, тот же silent path,
        # что и для "вообще нет ожидающей заявки" (см. implementation plan
        # §7.4 — намеренно НЕ вводим отдельное "заявка закрыта" сообщение
        # здесь, чтобы не создавать новое late-activity состояние).
        message = make_message()
        await webapp._handle_brief_submission(message, {"service_name": "Лендинг", "attach_tz": True, "draft_id": "m-closed"})
        lead_id = (await content_store.find_lead_awaiting_file(message.from_user.id))["id"]
        message.answer.reset_mock()
        message.bot.send_message.reset_mock()

        await content_store.update_lead_status("777", lead_id, "DONE")

        message.document = make_fake_document(file_id="doc-late", file_unique_id="uniq-late")
        await webapp.handle_tz_file(message)

        message.answer.assert_not_awaited()
        message.bot.send_message.assert_not_awaited()
        message.forward.assert_not_awaited()
        self.assertEqual((await content_store.get_lead(lead_id))["materials"], [])


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

    # ---- Telegram 4096-char defensive limit (E2E MVP audit, Batch 4) ----

    async def test_admin_detail_stays_under_telegram_limit_with_heavy_accumulation(self):
        for _ in range(40):
            await content_store.add_lead_supplement(
                self.lead["id"], {"user_id": 55555, "username": "client"},
                {"comment": "x" * 150, "additional_requirements": "y" * 150},
            )
        for i in range(40):
            await content_store.record_lead_material(self.lead["id"], f"file-{i}", f"uniq-{i}", "document", "new")
        for _ in range(40):
            await content_store.add_owner_message(self.actor, self.lead["id"], "z" * 150, "sent")

        lead = await content_store.get_lead(self.lead["id"])
        text = lead_format.format_lead_admin_detail(lead)

        self.assertLessEqual(len(text), 4096)  # реальный Telegram-лимит на длину сообщения
        self.assertIn("часть истории скрыта", text)  # обрезка сработала явно, не тихо потеряла данные

    async def test_admin_detail_short_lead_is_not_truncated(self):
        # Обычная короткая заявка — формат карточки не меняется вообще.
        await content_store.add_owner_message(self.actor, self.lead["id"], "Короткий ответ", "sent")
        lead = await content_store.get_lead(self.lead["id"])
        text = lead_format.format_lead_admin_detail(lead)

        self.assertNotIn("часть истории скрыта", text)
        self.assertIn("Короткий ответ", text)

    async def test_admin_detail_truncation_does_not_touch_storage(self):
        # Presentation-only — сам lead в storage не усечён, тронута только
        # возвращаемая строка format_lead_admin_detail.
        for _ in range(40):
            await content_store.add_owner_message(self.actor, self.lead["id"], "z" * 150, "sent")

        lead = await content_store.get_lead(self.lead["id"])
        self.assertEqual(len(lead["owner_messages"]), 40)  # ничего не удалено из данных

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


class BehanceCoverResolverTests(unittest.IsolatedAsyncioTestCase):
    """E2E-эксперимент: обложка кейса берётся напрямую с CDN Behance по
    og:image, без нашего object storage и без proxy (см. bot/behance.py).

    Сеть здесь НЕ трогается: _http_get/_http_head замоканы. Живая проверка
    против реального проекта выполнялась отдельно, вне suite — тесты не
    должны зависеть от доступности Behance."""

    REAL_URL = "https://www.behance.net/gallery/237585701/UIUX-Design-for-Marketing-Agency-Website"
    CDN_1400 = "https://mir-s3-cdn-cf.behance.net/project_modules/1400/b63348237585701.69036215a8d6b.jpg"
    CDN_DISP = "https://mir-s3-cdn-cf.behance.net/project_modules/disp/b63348237585701.69036215a8d6b.jpg"

    def _page(self, og_image: str | None) -> bytes:
        tag = f'<meta property="og:image" content="{og_image}" />' if og_image else ""
        return (
            '<html><head><meta property="og:title" content="X" />'
            f'{tag}'
            '<meta property="og:image:width" content="1400" />'
            '<meta property="og:image:height" content="1459" />'
            "</head><body></body></html>"
        ).encode("utf-8")

    # ---- URL validation (host + path), до любого сетевого запроса ----

    def test_accepts_real_project_url(self):
        self.assertTrue(behance.is_behance_project_url(self.REAL_URL))

    def test_rejects_non_behance_host(self):
        self.assertFalse(behance.is_behance_project_url("https://example.com/gallery/123/x"))

    def test_rejects_behance_homepage_and_profile(self):
        # Регрессия на реально воспроизведённую проблему: у главной/профиля
        # тоже есть og:image (generic SEO-логотип Behance), и без проверки
        # пути такая ссылка молча становилась бы "обложкой кейса".
        self.assertFalse(behance.is_behance_project_url("https://www.behance.net/"))
        self.assertFalse(behance.is_behance_project_url("https://www.behance.net/someuser"))

    def test_rejects_malformed_and_empty(self):
        for bad in ("not-a-url", "", "   ", None, "ftp://www.behance.net/gallery/1/x"):
            self.assertFalse(behance.is_behance_project_url(bad), f"expected reject: {bad!r}")

    # ---- og:image extraction ----

    def test_extracts_og_image_both_attribute_orders(self):
        forward = '<meta property="og:image" content="https://cdn/x.jpg" />'
        reverse = '<meta content="https://cdn/x.jpg" property="og:image" />'
        self.assertEqual(behance.extract_og_image(forward), "https://cdn/x.jpg")
        self.assertEqual(behance.extract_og_image(reverse), "https://cdn/x.jpg")

    def test_does_not_confuse_og_image_width_for_og_image(self):
        html_only_dimensions = '<meta property="og:image:width" content="1400" />'
        self.assertIsNone(behance.extract_og_image(html_only_dimensions))

    def test_decodes_html_entities_in_url(self):
        raw = '<meta property="og:image" content="https://cdn/x.jpg?a=1&amp;b=2" />'
        self.assertEqual(behance.extract_og_image(raw), "https://cdn/x.jpg?a=1&b=2")

    def test_returns_none_when_no_og_image(self):
        self.assertIsNone(behance.extract_og_image("<html><head></head></html>"))

    # ---- disp size variant ----

    def test_disp_variant_substitution(self):
        self.assertEqual(behance._disp_variant(self.CDN_1400), self.CDN_DISP)

    def test_disp_variant_returns_none_for_unknown_url_shape(self):
        # Не ломаем URL искусственно: если форма не та, подмены не делаем.
        self.assertIsNone(behance._disp_variant("https://example.com/some/other/image.jpg"))
        self.assertIsNone(behance._disp_variant(self.CDN_DISP))  # уже disp

    # ---- resolve_cover_url ----

    async def test_resolve_prefers_disp_when_accessible(self):
        with patch.object(behance, "_http_get", return_value=(200, self._page(self.CDN_1400))), \
             patch.object(behance, "_http_head", return_value=200):
            cover = await behance.resolve_cover_url(self.REAL_URL)
        self.assertEqual(cover, self.CDN_DISP)

    async def test_resolve_falls_back_to_original_when_disp_unavailable(self):
        def head(url, timeout=10):
            return 404 if "/disp/" in url else 200

        with patch.object(behance, "_http_get", return_value=(200, self._page(self.CDN_1400))), \
             patch.object(behance, "_http_head", side_effect=head):
            cover = await behance.resolve_cover_url(self.REAL_URL)
        self.assertEqual(cover, self.CDN_1400)

    async def test_resolve_raises_when_no_og_image(self):
        with patch.object(behance, "_http_get", return_value=(200, self._page(None))):
            with self.assertRaises(behance.BehanceResolveError):
                await behance.resolve_cover_url(self.REAL_URL)

    async def test_resolve_raises_on_non_200_page(self):
        with patch.object(behance, "_http_get", return_value=(404, b"")):
            with self.assertRaises(behance.BehanceResolveError):
                await behance.resolve_cover_url(self.REAL_URL)

    async def test_resolve_raises_when_behance_unreachable(self):
        with patch.object(behance, "_http_get", side_effect=urllib.error.URLError("network down")):
            with self.assertRaises(behance.BehanceResolveError):
                await behance.resolve_cover_url(self.REAL_URL)

    async def test_resolve_raises_when_image_inaccessible(self):
        with patch.object(behance, "_http_get", return_value=(200, self._page(self.CDN_1400))), \
             patch.object(behance, "_http_head", return_value=403):
            with self.assertRaises(behance.BehanceResolveError):
                await behance.resolve_cover_url(self.REAL_URL)

    async def test_resolve_rejects_non_https_og_image(self):
        with patch.object(behance, "_http_get", return_value=(200, self._page("http://cdn/insecure.jpg"))):
            with self.assertRaises(behance.BehanceResolveError):
                await behance.resolve_cover_url(self.REAL_URL)

    async def test_non_behance_url_never_triggers_network_call(self):
        # Гарантия, что модуль не превращается в универсальный web-fetcher:
        # посторонний URL отсекается ДО первого запроса.
        with patch.object(behance, "_http_get") as mock_get:
            with self.assertRaises(behance.BehanceResolveError):
                await behance.resolve_cover_url("https://example.com/gallery/1/x")
        mock_get.assert_not_called()

    # ---- Fix A: browser-like headers (Render получал 403 от edge Adobe) ----

    def _capture_request(self, fn, *, body=b"<html></html>", content_encoding=None):
        """Перехватывает urllib.request.Request, который реально уходит."""
        captured = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Encoding": content_encoding}

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return FakeResponse()

        with patch.object(behance.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = fn()
        return captured["req"], result

    def _headers_of(self, req):
        return {k.lower(): v for k, v in req.header_items()}

    def test_page_get_sends_browser_like_headers(self):
        req, _ = self._capture_request(lambda: behance._http_get("https://www.behance.net/gallery/1/x"))
        h = self._headers_of(req)
        # User-Agent сохранён без изменений
        self.assertEqual(h["user-agent"], behance._USER_AGENT)
        for header in ("accept", "accept-language", "accept-encoding", "upgrade-insecure-requests"):
            self.assertIn(header, h, f"ожидался заголовок {header}")
        self.assertTrue(h["accept"].startswith("text/html"))
        # Sec-Fetch-* соответствуют навигации по документу
        self.assertEqual(h["sec-fetch-dest"], "document")
        self.assertEqual(h["sec-fetch-mode"], "navigate")

    def test_image_head_sends_image_appropriate_headers(self):
        req, _ = self._capture_request(lambda: behance._http_head("https://mir-s3-cdn-cf.behance.net/x.jpg"))
        h = self._headers_of(req)
        self.assertEqual(h["user-agent"], behance._USER_AGENT)
        self.assertTrue(h["accept"].startswith("image/"))
        self.assertEqual(h["sec-fetch-dest"], "image")

    def test_accept_encoding_never_requests_brotli(self):
        # br нечем распаковать в stdlib — попросив его, мы получили бы тело,
        # из которого og:image уже не извлечь.
        for headers in (behance._PAGE_HEADERS, behance._IMAGE_HEADERS):
            self.assertNotIn("br", headers["Accept-Encoding"])
            self.assertIn("gzip", headers["Accept-Encoding"])

    def test_gzip_response_body_is_decompressed(self):
        import gzip as _gzip
        payload = b'<meta property="og:image" content="https://cdn/x.jpg" />'
        _, (status, body) = self._capture_request(
            lambda: behance._http_get("https://www.behance.net/gallery/1/x"),
            body=_gzip.compress(payload), content_encoding="gzip",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(behance.extract_og_image(body.decode()), "https://cdn/x.jpg")

    def test_deflate_response_body_is_decompressed(self):
        import zlib as _zlib
        payload = b"<html>deflate</html>"
        _, (_, body) = self._capture_request(
            lambda: behance._http_get("https://www.behance.net/gallery/1/x"),
            body=_zlib.compress(payload), content_encoding="deflate",
        )
        self.assertEqual(body, payload)

    def test_uncompressed_response_passes_through(self):
        payload = b"<html>plain</html>"
        _, (_, body) = self._capture_request(
            lambda: behance._http_get("https://www.behance.net/gallery/1/x"),
            body=payload, content_encoding=None,
        )
        self.assertEqual(body, payload)

    def test_http_error_status_is_returned_not_raised(self):
        # Поведение при HTTP-ошибке не меняется Fix A: 403 отдаётся как
        # (код, b"") и превращается в контролируемый BehanceResolveError —
        # именно этот путь наблюдался на production.
        err = urllib.error.HTTPError("https://www.behance.net/gallery/1/x", 403, "Forbidden", {}, None)
        with patch.object(behance.urllib.request, "urlopen", side_effect=err):
            status, body = behance._http_get("https://www.behance.net/gallery/1/x")
        self.assertEqual(status, 403)
        self.assertEqual(body, b"")

    async def test_http_403_still_surfaces_as_controlled_error(self):
        with patch.object(behance, "_http_get", return_value=(403, b"")):
            with self.assertRaises(behance.BehanceResolveError) as ctx:
                await behance.resolve_cover_url(self.REAL_URL)
        self.assertIn("403", str(ctx.exception))

    async def test_image_bytes_are_never_downloaded(self):
        # Ключевое свойство эксперимента: проверяем доступность картинки
        # HEAD-запросом, байты через Render не проходят.
        captured = []

        def head(url, timeout=10):
            captured.append(url)
            return 200

        with patch.object(behance, "_http_get", return_value=(200, self._page(self.CDN_1400))) as mock_get, \
             patch.object(behance, "_http_head", side_effect=head):
            await behance.resolve_cover_url(self.REAL_URL)

        # _http_get вызван РОВНО один раз и только для HTML-страницы проекта.
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.args[0], self.REAL_URL)
        # Картинка запрашивалась только через HEAD.
        self.assertTrue(all("mir-s3-cdn-cf.behance.net" in u for u in captured))


class BehanceCoverIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Цепочка external_url -> resolver -> cover -> portfolio.json, целиком
    на существующих функциях content_store (add_case/update_case). Storage
    abstraction (upload_image/delete_image/is_configured) не участвует."""

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

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_behance_case_stores_project_url_and_resolved_cover_separately(self):
        project_url = BehanceCoverResolverTests.REAL_URL
        resolved = BehanceCoverResolverTests.CDN_DISP

        with patch.object(behance, "_http_get", return_value=(200, (
                f'<meta property="og:image" content="{BehanceCoverResolverTests.CDN_1400}" />'
            ).encode("utf-8"))), \
             patch.object(behance, "_http_head", return_value=200):
            cover = await behance.resolve_cover_url(project_url)

        await content_store.add_case(
            self.actor, case_id="case_behance_test", title="TEST — Behance Cover",
            type_id="landing", cover=cover, task="E2E", related_service=None,
        )
        await content_store.update_case(self.actor, "case_behance_test", external_url=project_url)

        case = next(c for c in await content_store.list_cases() if c["id"] == "case_behance_test")
        # Семантика полей не смешивается: ссылка на проект остаётся ссылкой,
        # cover — прямой URL картинки.
        self.assertEqual(case["external_url"], project_url)
        self.assertEqual(case["cover"], resolved)
        self.assertTrue(case["cover"].startswith("https://"))
        self.assertIn(resolved, case["images"])

    async def test_existing_local_cover_cases_are_untouched(self):
        # Регрессия: эксперимент не должен менять уже существующие кейсы с
        # относительными путями к демо-SVG.
        cases = await content_store.list_cases()
        legacy = [c for c in cases if str(c.get("cover", "")).startswith("img/portfolio/")]
        self.assertTrue(legacy, "ожидались демо-кейсы с относительными путями")
        for case in legacy:
            self.assertFalse(case["cover"].startswith("http"))


class BehanceAdminFlowTests(unittest.IsolatedAsyncioTestCase):
    """Интеграция Behance resolver в существующий admin flow: создание кейса
    ссылкой вместо фото и смена external_url у существующего кейса.

    Сеть не трогается — behance._http_get/_http_head замоканы. Проверяется
    именно admin-цепочка, сам resolver покрыт BehanceCoverResolverTests."""

    PROJECT_URL = BehanceCoverResolverTests.REAL_URL
    CDN_1400 = BehanceCoverResolverTests.CDN_1400
    CDN_DISP = BehanceCoverResolverTests.CDN_DISP

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

    def tearDown(self):
        content_store.DATA_DIR = self._orig_data_dir
        content_store.config.DESIGNER_CHAT_ID = self._orig_designer
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mock_behance_ok(self):
        page = f'<meta property="og:image" content="{self.CDN_1400}" />'.encode("utf-8")
        return (
            patch.object(behance, "_http_get", return_value=(200, page)),
            patch.object(behance, "_http_head", return_value=200),
        )

    async def _case(self, case_id):
        return next((c for c in await content_store.list_cases() if c["id"] == case_id), None)

    # ---- Test A + B: create via Behance URL sets cover, keeps external_url ----

    async def test_A_B_create_case_from_behance_url_sets_cover_and_keeps_project_url(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.add_case_photo)
        case_id = await content_store.next_case_id()
        await state.update_data(case_id=case_id, title="TEST — Behance Cover", type_id="site")

        get_p, head_p = self._mock_behance_ok()
        with get_p, head_p:
            await admin.cases_add_photo_behance(
                make_flow_message_factory(chat_id=self.actor, start_id=7000)(text=self.PROJECT_URL), state
            )
        # Перешли к описанию — тот же шаг, что и после загрузки фото.
        self.assertEqual(await state.get_state(), AdminStates.add_case_description.state)
        self.assertEqual((await state.get_data())["cover"], self.CDN_DISP)

        await admin.cases_add_description(
            make_flow_message_factory(chat_id=self.actor, start_id=7100)(text="E2E описание"), state
        )

        case = await self._case(case_id)
        self.assertIsNotNone(case)
        # Test A: cover — CDN-ссылка Behance (disp-вариант)
        self.assertEqual(case["cover"], self.CDN_DISP)
        # Test B: external_url — исходная ссылка на проект, НЕ подменена картинкой
        self.assertEqual(case["external_url"], self.PROJECT_URL)
        self.assertNotEqual(case["external_url"], case["cover"])

    # ---- Test C: invalid / non-project URL -> controlled error ----

    async def test_C_non_project_behance_url_is_rejected_without_creating_case(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.add_case_photo)
        await state.update_data(case_id="case_should_not_exist", title="X", type_id="site")

        msg = make_flow_message_factory(chat_id=self.actor, start_id=7200)(text="https://www.behance.net/someuser")
        with patch.object(behance, "_http_get") as mock_get:
            await admin.cases_add_photo_behance(msg, state)
        mock_get.assert_not_called()  # отсечено до сети
        # Остались на том же шаге, cover не выставлен, кейс не создан.
        self.assertEqual(await state.get_state(), AdminStates.add_case_photo.state)
        self.assertIsNone((await state.get_data()).get("cover"))
        self.assertIsNone(await self._case("case_should_not_exist"))

    async def test_C_resolver_failure_shows_friendly_error_without_traceback(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.add_case_photo)
        await state.update_data(case_id="case_fail", title="X", type_id="site")

        msg = make_flow_message_factory(chat_id=self.actor, start_id=7300)(text=self.PROJECT_URL)
        with patch.object(behance, "_http_get", return_value=(404, b"")):
            await admin.cases_add_photo_behance(msg, state)

        # step_from_text редактирует anchor, если он засеян в state, иначе
        # шлёт новое сообщение (см. bot/flow.py) — здесь anchor не засеян.
        if msg.bot.edit_message_text.await_args is not None:
            shown = msg.bot.edit_message_text.await_args.args[0]
        else:
            shown = msg.answer.await_args.args[0]
        self.assertIn("Не удалось получить обложку с Behance", shown)
        self.assertNotIn("Traceback", shown)
        self.assertNotIn("BehanceResolveError", shown)
        self.assertEqual(await state.get_state(), AdminStates.add_case_photo.state)
        self.assertIsNone((await state.get_data()).get("cover"))

    # ---- Test D: existing upload flow unchanged ----

    async def test_D_existing_photo_upload_flow_still_uses_storage_backend(self):
        state = make_state(self.actor)
        await state.set_state(AdminStates.add_case_photo)
        await state.update_data(case_id="case_photo_flow", title="X", type_id="site")

        photo_msg = make_photo_message(self.actor)
        with patch.object(content_store, "save_case_photo", new=AsyncMock(return_value="img/portfolio/case_x.jpg")) as mock_save:
            await admin.cases_add_photo(photo_msg, state)

        # Обычная загрузка по-прежнему идёт через существующий storage.
        mock_save.assert_awaited_once()
        self.assertEqual((await state.get_data())["cover"], "img/portfolio/case_x.jpg")
        # Behance тут не участвует — external_url не появляется.
        self.assertIsNone((await state.get_data()).get("external_url"))
        self.assertEqual(await state.get_state(), AdminStates.add_case_description.state)

    # ---- Test E: non-Behance case unaffected; edit path ----

    async def test_E_editing_non_behance_external_url_does_not_touch_cover(self):
        await content_store.add_case(
            self.actor, case_id="case_plain", title="Plain", type_id="site",
            cover="img/portfolio/demo_case_1.svg", task="t", related_service=None,
        )
        state = make_state(self.actor)
        await state.set_state(AdminStates.edit_case_value)
        await state.update_data(case_id="case_plain", field="external_url")

        msg = make_flow_message_factory(chat_id=self.actor, start_id=7400)(text="https://example.com/portfolio")
        with patch.object(behance, "_http_get") as mock_get:
            await admin.cases_edit_value(msg, state)
        mock_get.assert_not_called()

        case = await self._case("case_plain")
        self.assertEqual(case["external_url"], "https://example.com/portfolio")
        self.assertEqual(case["cover"], "img/portfolio/demo_case_1.svg")  # обложка не тронута

    async def test_edit_external_url_to_behance_updates_cover(self):
        await content_store.add_case(
            self.actor, case_id="case_to_behance", title="X", type_id="site",
            cover="img/portfolio/demo_case_2.svg", task="t", related_service=None,
        )
        state = make_state(self.actor)
        await state.set_state(AdminStates.edit_case_value)
        await state.update_data(case_id="case_to_behance", field="external_url")

        get_p, head_p = self._mock_behance_ok()
        with get_p, head_p:
            await admin.cases_edit_value(
                make_flow_message_factory(chat_id=self.actor, start_id=7500)(text=self.PROJECT_URL), state
            )

        case = await self._case("case_to_behance")
        self.assertEqual(case["external_url"], self.PROJECT_URL)
        self.assertEqual(case["cover"], self.CDN_DISP)

    async def test_editing_other_text_field_is_unaffected(self):
        await content_store.add_case(
            self.actor, case_id="case_title_edit", title="Old", type_id="site",
            cover="img/portfolio/demo_case_3.svg", task="t", related_service=None,
        )
        state = make_state(self.actor)
        await state.set_state(AdminStates.edit_case_value)
        await state.update_data(case_id="case_title_edit", field="title")

        with patch.object(behance, "_http_get") as mock_get:
            await admin.cases_edit_value(
                make_flow_message_factory(chat_id=self.actor, start_id=7600)(text="New title"), state
            )
        mock_get.assert_not_called()

        case = await self._case("case_title_edit")
        self.assertEqual(case["title"], "New title")
        self.assertEqual(case["cover"], "img/portfolio/demo_case_3.svg")


if __name__ == "__main__":
    unittest.main()
