import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import config, content_store, flow, texts
from bot.keyboards import main_menu_confirm_keyboard, webapp_open_keyboard

router = Router(name="start")
logger = logging.getLogger(__name__)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # NAV anchor (persistent reply-клавиатура) и TRANSIENT-экран — теперь
    # разделены (см. bot/flow.py, UX-аудит про исчезающую клавиатуру после
    # FAQ/admin). /start не отправляет WELCOME отдельным сообщением — он
    # либо создаёт NAV anchor с этим текстом (первое взаимодействие в
    # чате), либо просто редактирует уже существующий обратно в WELCOME.
    await flow.reset_nav_screen(message, state)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    # Не через flow.open_root — Markdown-форматирование (`chat_id` в
    # обратных кавычках) не проходит через универсальный open_root, который
    # не принимает parse_mode; расширять ради одной команды не стали
    # (см. Part 17 ТЗ — не переписывать без необходимости). RULE 1
    # (удаление триггера) применяем отдельно, best-effort.
    await flow.delete_trigger(message)
    await message.answer(texts.MY_ID_TEMPLATE.format(chat_id=message.chat.id), parse_mode="Markdown")


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message, state: FSMContext) -> None:
    # NAV anchor (persistent-клавиатура) гарантируется автоматически внутри
    # flow.open_root/open_flow (см. bot/flow.py::ensure_nav_anchor) —
    # отдельно заботиться о ней здесь не нужно.
    await flow.open_root(message, state, "Открыть портфолио:", webapp_open_keyboard(config.WEBAPP_URL, "portfolio", "📁 Открыть портфолио"))


@router.message(Command("about"))
async def cmd_about(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть «Обо мне»:", webapp_open_keyboard(config.WEBAPP_URL, "about", "👤 Открыть «Обо мне»"))


@router.message(Command("brief"))
async def cmd_brief(message: Message, state: FSMContext) -> None:
    await flow.open_root(message, state, "Открыть заявку:", webapp_open_keyboard(config.WEBAPP_URL, "brief", "✍️ Оставить заявку"))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    # Раньше проверялось FSM-состояние BriefStates.awaiting_tz_file — теперь
    # "жду файл" хранится persistent в самой заявке (см.
    # content_store.find_lead_awaiting_file/mark_tz_file_received,
    # bot/handlers/webapp.py::handle_tz_file), переживает рестарт бота.
    # /cancel здесь просто снимает флаг ожидания — заявка остаётся как есть.
    awaiting_lead = await content_store.find_lead_awaiting_file(message.from_user.id)
    if awaiting_lead is not None:
        await content_store.mark_tz_file_received(awaiting_lead["id"])
        text = "Хорошо, отменил ожидание файла."
    else:
        text = "Отменять было нечего."
    # reply_markup=None — TRANSIENT-экран не несёт persistent-клавиатуру,
    # её уже обеспечивает NAV anchor (см. bot/flow.py).
    await flow.open_root(message, state, text, None)


@router.message(F.text == texts.OPEN_APP_BUTTON)
async def open_app_button(message: Message, state: FSMContext) -> None:
    # "🚀 Открыть приложение" — этой кнопки больше нет в main_reply_keyboard
    # (прямой запуск теперь через Telegram Menu Button, см. bot/main.py::
    # _setup_menu_button), но handler оставлен: у части клиентов может быть
    # закэширована старая reply-клавиатура ДО этого деплоя — если такая
    # кнопка всё же придёт текстом, она по-прежнему должна работать, а не
    # тихо проваливаться в fallback_text.
    await flow.open_root(
        message, state,
        "Открыть приложение:",
        webapp_open_keyboard(config.WEBAPP_URL, "portfolio", texts.OPEN_APP_BUTTON),
    )


async def main_menu_or_confirm(message: Message, state: FSMContext) -> None:
    """Общая логика "⌂ Главное меню" для клиента (этот router) и владельца
    (bot/handlers/admin.py::admin_main_menu_button — тот же текст кнопки,
    но зарегистрирован отдельно и РАНО в admin.py, до AdminStates.*, F.text
    мастеров, иначе они проглотили бы текст кнопки как обычный ввод — см.
    bot/keyboards.py::main_reply_keyboard).

    Нет активного bot/FSM-состояния — терять нечего, просто сбрасываем
    TRANSIENT-экран и триггер (см. flow.main_menu_cleanup) — БЕЗ WELCOME:
    "Главное меню" не /start, NAV anchor уже существует и его не нужно
    трогать. Есть — показываем подтверждение (inline, без отдельного
    долгоживущего state под саму confirmation, см.
    bot/keyboards.py::main_menu_confirm_keyboard)."""
    if await state.get_state() is None:
        await flow.main_menu_cleanup(message, state)
        return
    await flow.delete_trigger(message)
    # NAV anchor не затронут этим экраном — освежать нечего (см. bot/flow.py).
    await message.answer(texts.MAIN_MENU_CONFIRM_TEXT, reply_markup=main_menu_confirm_keyboard())


@router.message(F.text == texts.MAIN_MENU_BUTTON)
async def main_menu_button(message: Message, state: FSMContext) -> None:
    await main_menu_or_confirm(message, state)


@router.callback_query(F.data == "mainmenu:confirm")
async def main_menu_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    # callback.message — то самое confirmation-сообщение; main_menu_cleanup
    # сам удалит его как "триггер" (RULE 1). NAV anchor не трогаем — это
    # "Главное меню", не /start (см. bot/flow.py::main_menu_cleanup).
    await flow.main_menu_cleanup(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "mainmenu:decline")
async def main_menu_decline(callback: CallbackQuery, state: FSMContext) -> None:
    # Ничего не сбрасываем — только убираем сам вопрос; экран/состояние,
    # которые были ДО "Главное меню", остаются как есть.
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass
    await callback.answer()


def _client_identity_line(message: Message) -> str:
    user = message.from_user
    name = " ".join(filter(None, [user.first_name, user.last_name])) or "не указано"
    contact = f"@{user.username}" if user.username else f"id {user.id}"
    return f"Клиент: {name} ({contact})"


# Presentation-only защита от Telegram-лимита на длину исходящего сообщения
# (4096 символов) — тот же принцип, что и bot/lead.py::_MAX_DETAIL_LENGTH/
# _clamp_to_telegram_limit (запас под сам маркер обрезки), но не та же
# функция: там обрезаются целыми СТРОКАМИ с конца списка (подходит для
# карточки заявки с множеством коротких строк), здесь единственное поле
# переменной длины — сам текст клиента, поэтому обрезается только оно,
# заголовок/идентичность/список активных заявок всегда сохраняются целиком.
_MAX_RELAY_LENGTH = 4000

# Реальный жёсткий лимит Telegram на исходящее текстовое сообщение.
_TELEGRAM_HARD_LIMIT = 4096


def _clamp_relay_client_text(header_lines: list[str], client_text: str) -> str:
    header_len = len("\n".join(header_lines)) + 1  # +1 — перевод строки перед текстом клиента
    available = max(_MAX_RELAY_LENGTH - header_len, 0)
    if len(client_text) <= available:
        return client_text
    if available == 0:
        # Патологический случай (Stage C Batch 1 review) — самому header'у
        # уже некуда деться, добавлять маркер "…" здесь означало бы вернуть
        # непустую строку без единого символа текста клиента под ней, что
        # раньше и приводило к превышению бюджета (header_len + маркер).
        # Пустая строка честно отражает "текста клиента здесь не поместилось".
        return ""
    marker = "…"
    return client_text[: available - len(marker)] + marker


def _enforce_telegram_hard_limit(text: str) -> str:
    """Последний, безусловный рубеж (Stage C Batch 1, fix после review) —
    _clamp_relay_client_text ограничивает только текст клиента и рассчитан
    на header в разумных пределах; сам header (номер заявки, identity,
    список активных заявок) сознательно не обрезается там, чтобы не терять
    контекст в обычном случае. Но если header патологически большой
    (например, у одного клиента сотни активных заявок — ничем в этом файле
    не ограничено), даже пустой текст клиента не спасает: собранное
    сообщение всё равно может быть длиннее реального лимита Telegram.
    Эта функция — safety net поверх уже полностью собранной строки,
    применяется последней, прямо перед отправкой."""
    if len(text) <= _TELEGRAM_HARD_LIMIT:
        return text
    marker = "…"
    return text[: _TELEGRAM_HARD_LIMIT - len(marker)] + marker


# Зарегистрирован ПЕРЕД fallback_text ниже, тем же catch-all F.text — в
# рамках одного router первый совпавший хендлер побеждает (см. bot/main.py:
# роутеры пробуются по порядку, а внутри router — тоже по порядку
# регистрации), поэтому через обычный dispatch fallback_text для клиентов
# больше не достижим вообще. Единственный оставшийся вызов fallback_text —
# явный прямой вызов ниже, только для сообщений из DESIGNER_CHAT_ID (сбой
# самого relay обрабатывается отдельно, своим текстом, см. except ниже, а
# не падением в generic fallback).
@router.message(F.text)
async def relay_client_text_to_designer(message: Message, state: FSMContext) -> None:
    """Свободный текст клиента -> рабочий чат дизайнера (Stage B) — раньше
    любой нераспознанный текст уходил в fallback_text и не доходил до
    дизайнера вообще (см. Stage A аудит). DESIGNER_CHAT_ID исключён явно
    (str(...) == config.DESIGNER_CHAT_ID, тот же паттерн, что и
    content_store._require_designer) — иначе собственная идле-переписка
    дизайнера со своим ботом вне admin FSM (нет активного AdminStates —
    admin.router её не перехватывает и она долетает досюда) relay'илась бы
    дизайнеру же, что бессмысленно; для этого случая — прежнее поведение
    (fallback_text).

    Активная заявка определяется через content_store.ACTIVE_LEAD_STATUSES
    (NEW/VIEWED/IN_PROGRESS/WAITING_CLIENT) — DONE/CANCELLED намеренно не
    считаются: сообщение по давно закрытой заявке не должно молча к ней
    прикрепляться (см. Stage B ТЗ). Ровно одна активная заявка -> сообщение
    привязывается к ней явно; ноль или несколько -> "Общее обращение" (бот
    сознательно не угадывает, к какой из нескольких заявок относится текст
    — это решает дизайнер сам, ему показан список номеров).

    Без parse_mode (обычный текст, не HTML) — текст клиента подставляется
    как есть, без escape: HTML-режим здесь означал бы риск сломать
    отправку, если в тексте клиента случайно окажутся "<"/">" (Telegram
    попытался бы распарсить их как теги и отверг бы сообщение целиком)."""
    if not config.DESIGNER_CHAT_ID or str(message.chat.id) == config.DESIGNER_CHAT_ID:
        await fallback_text(message, state)
        return

    active_leads = [
        lead for lead in await content_store.list_leads_by_user(message.from_user.id)
        if lead["status"] in content_store.ACTIVE_LEAD_STATUSES
    ]
    identity_line = _client_identity_line(message)

    if len(active_leads) == 1:
        lines = [f"💬 Сообщение по заявке #{active_leads[0]['id']}", "", identity_line]
    else:
        lines = ["💬 Общее обращение", "", identity_line]
        if active_leads:
            lead_ids = ", ".join(f"#{lead['id']}" for lead in active_leads)
            lines.append(f"Активные заявки: {lead_ids}")
    header_lines = lines + ["", "Текст клиента:"]
    lines = header_lines + [_clamp_relay_client_text(header_lines, message.text)]
    text = _enforce_telegram_hard_limit("\n".join(lines))

    try:
        await message.bot.send_message(chat_id=config.DESIGNER_CHAT_ID, text=text)
    except Exception:
        logger.exception("Не удалось передать сообщение клиента дизайнеру (user_id=%s)", message.from_user.id)
        await message.answer("Не получилось отправить сообщение дизайнеру. Попробуйте ещё раз чуть позже.")
        return

    await message.answer("Сообщение отправлено дизайнеру ✅")


# Держим последним в этом роутере (тот же catch-all F.text, что и раньше —
# сохранён ради этой позиции и общего router-порядка, а не потому что
# дальнейший dispatch когда-либо сюда попадёт: relay_client_text_to_designer
# выше матчит тот же F.text и зарегистрирован раньше, поэтому реально вызывается
# только явно, напрямую из него — для сообщений из DESIGNER_CHAT_ID, см. Stage B).
@router.message(F.text)
async def fallback_text(message: Message, state: FSMContext) -> None:
    await flow.open_root(
        message, state,
        "Не совсем поняла 🙂 Воспользуйтесь кнопками ниже.",
        None,
    )
