"""Управление накоплением сообщений в чате — архитектурный принцип перенесён
из соседнего проекта Personal Assistant (src/bot/utils/flow.py), не
придуман заново. Три правила:

RULE 1 — триггер (команда/текст кнопки нижнего меню) удаляется после
обработки, чтобы не оставаться в чате рядом с результатом.
RULE 2 — при открытии нового корневого экрана старое сообщение бота
(предыдущий корневой экран) удаляется, а не остаётся висеть над новым.
RULE 3 — шаги внутри одного сценария редактируют одно и то же сообщение
(edit_text) вместо отправки нового сообщения на каждый шаг.

Все удаления — best-effort (try/except): Telegram может отказать (сообщение
старше 48 часов, уже удалено и т.п.) — это не должно ронять бота."""

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup

from bot.keyboards import reply_keyboard_for_chat

_ANCHOR_MSG_KEY = "_flow_msg_id"
_ANCHOR_CHAT_KEY = "_flow_chat_id"


async def open_flow(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
) -> None:
    """Показать новый корневой экран/сценарий (нет предыдущего сообщения
    бота, которое можно было бы отредактировать). Сначала удаляет то, что
    было отмечено как текущий экран (RULE 2), затем триггер (RULE 1), затем
    отправляет новое сообщение и запоминает его как текущий экран — все
    дальнейшие шаги (через step_from_text/step_from_callback) будут
    редактировать именно его, а не копиться новыми сообщениями.

    Если сам этот новый экран несёт НЕ persistent reply-клавиатуру (то есть
    inline или вообще без разметки) — ПЕРЕД удалением предыдущего anchor
    сначала "освежаем" reply-клавиатуру (см. refresh_reply_keyboard ниже).
    Раньше это было обязанностью каждого вызывающего кода по отдельности
    (и /admin, например, эту обязанность и не выполнял вовсе — реальный
    пробел, найденный при аудите) — и, что важнее, делалось уже ПОСЛЕ
    удаления предыдущего anchor. Если этот anchor был последним сообщением,
    подтверждавшим reply-клавиатуру клиенту, в этом промежутке клиент мог
    остаться без нормальной reply-клавиатуры до следующего подтверждения
    (см. UX-аудит, regression после интеграции FAQ в этот же lifecycle) —
    порядок здесь важен, а не просто "сделать оба действия"."""
    if not isinstance(reply_markup, ReplyKeyboardMarkup):
        await refresh_reply_keyboard(message, reply_keyboard_for_chat(message.chat.id))

    data = await state.get_data()
    prev_id = data.get(_ANCHOR_MSG_KEY)
    prev_chat = data.get(_ANCHOR_CHAT_KEY)
    if prev_id and prev_chat:
        try:
            await message.bot.delete_message(chat_id=prev_chat, message_id=prev_id)
        except TelegramAPIError:
            pass

    await delete_trigger(message)

    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(**{_ANCHOR_MSG_KEY: sent.message_id, _ANCHOR_CHAT_KEY: sent.chat.id})


async def step_from_callback(
    callback: CallbackQuery, state: FSMContext, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Шаг сценария после нажатия кнопки — редактирует то же сообщение (RULE 3)."""
    await callback.message.edit_text(text, reply_markup=reply_markup)
    await state.update_data(
        **{_ANCHOR_MSG_KEY: callback.message.message_id, _ANCHOR_CHAT_KEY: callback.message.chat.id}
    )


async def step_from_text(
    message: Message, state: FSMContext, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Шаг сценария после того, как пользователь напечатал ответ: удаляет
    его сообщение (чтобы не копилось) и редактирует текущий экран (RULE 3).
    Если редактирование не удалось (например, экран потерян) — отправляет
    новое сообщение и берёт его в качестве текущего экрана."""
    data = await state.get_data()
    anchor_id = data.get(_ANCHOR_MSG_KEY)
    anchor_chat = data.get(_ANCHOR_CHAT_KEY)

    try:
        await message.delete()
    except TelegramAPIError:
        pass

    if anchor_id:
        try:
            await message.bot.edit_message_text(
                text, chat_id=anchor_chat, message_id=anchor_id, reply_markup=reply_markup
            )
            return
        except TelegramAPIError:
            pass

    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(**{_ANCHOR_MSG_KEY: sent.message_id, _ANCHOR_CHAT_KEY: sent.chat.id})


async def advance(
    event: Message | CallbackQuery,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Шаг сценария независимо от того, что его вызвало — кнопка или текст."""
    if isinstance(event, CallbackQuery):
        await step_from_callback(event, state, text, reply_markup)
    else:
        await step_from_text(event, state, text, reply_markup)


async def delete_trigger(message: Message) -> None:
    """Best-effort удаление сообщения-триггера (команда или текст кнопки
    нижнего меню) — RULE 1."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def refresh_reply_keyboard(message: Message, reply_markup: ReplyKeyboardMarkup) -> None:
    """"Освежает" постоянную reply-клавиатуру после ответа, который несёт
    (или сейчас понесёт) InlineKeyboardMarkup — Bot API не позволяет одному
    сообщению нести оба типа разметки сразу, поэтому шлём отдельное
    сообщение с невидимым текстом (zero-width space) только чтобы
    Telegram-клиент подтвердил reply-клавиатуру, и сразу его best-effort
    удаляем — не должно оставлять следа в чате и не должно ронять
    вызывающий handler, если отправка или удаление не удались (тот же
    принцип, что и delete_trigger).

    Вызывается автоматически из open_flow (см. выше) для любого корневого
    экрана без своей persistent-клавиатуры — отдельно вызывать эту функцию
    для /portfolio, /about, /brief, /faq, /admin, "🚀 Открыть приложение" не
    нужно, open_flow уже это делает. Остаётся публичной для случаев вне
    open_flow (см. bot/handlers/start.py::main_menu_or_confirm — экран
    подтверждения не создаёт новый anchor, поэтому не идёт через open_flow)."""
    try:
        sent = await message.answer("​", reply_markup=reply_markup)
    except TelegramAPIError:
        return
    try:
        await message.bot.delete_message(chat_id=sent.chat.id, message_id=sent.message_id)
    except TelegramAPIError:
        pass


async def finish_flow(state: FSMContext) -> None:
    """Завершить FSM-состояние сценария, не теряя id текущего экрана — чтобы
    следующий open_flow/open_root корректно удалил именно его (RULE 2)."""
    await state.set_state(None)


async def open_root(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
) -> None:
    """Как open_flow, но для входных точек нижнего меню/команд, которые
    показывают корневой экран без собственного FSM-состояния (Портфолио,
    Обо мне, /admin и т.п.) — это же и "аварийный выход" из любого зависшего
    сценария: одного нажатия достаточно, чтобы и увидеть нужный экран, и
    сбросить старое состояние (иначе следующее случайное сообщение could
    провалиться в хендлер, который всё ещё думает, что сценарий активен)."""
    await finish_flow(state)
    await open_flow(message, state, text, reply_markup)
