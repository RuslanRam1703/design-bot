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
старше 48 часов, уже удалено и т.п.) — это не должно ронять бота.

ДВА НЕЗАВИСИМЫХ ANCHOR'а (см. UX-аудит про исчезающую persistent
reply-клавиатуру после FAQ/admin — zero-width-хак refresh_reply_keyboard
оказался ненадёжен в реальном Telegram Desktop: удаление carrier-сообщения
сбрасывало клавиатуру, несмотря на is_persistent=True):

- NAV anchor (_NAV_ANCHOR_*) — persistent navigation: ОДНО сообщение на весь
  чат, несущее ReplyKeyboardMarkup. Создаётся один раз лениво (см.
  ensure_nav_anchor), никогда не удаляется RULE 1/2/3, редактируется только
  при явном возврате в главное меню (см. reset_nav_screen).
- TRANSIENT anchor (_ANCHOR_*, прежнее имя, теперь только для этого) — то,
  чем управляют RULE 1/2/3 ниже: FAQ/Admin/Portfolio/... экраны и шаги
  сценариев. Несёт InlineKeyboardMarkup или ничего — никогда persistent
  reply-клавиатуру."""

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import texts
from bot.keyboards import reply_keyboard_for_chat

_ANCHOR_MSG_KEY = "_flow_msg_id"
_ANCHOR_CHAT_KEY = "_flow_chat_id"
_NAV_ANCHOR_MSG_KEY = "_nav_msg_id"
_NAV_ANCHOR_CHAT_KEY = "_nav_chat_id"

# Тексты ошибок Telegram Bot API, однозначно означающие "сообщения больше
# не существует" (реально удалено) — только они оправдывают пересоздание
# NAV anchor в reset_nav_screen. Любая другая ошибка edit_message_text
# (флуд-контроль, транзитная задержка и т.п.) — best-effort игнорируется.
#
# Строковое сравнение — не "придумано", а единственный доступный в aiogram
# способ: и "message is not modified", и "message to edit not found", и
# "message can't be edited" Telegram возвращает с ОДНИМ и тем же HTTP-кодом
# 400 (см. aiogram.client.session.base.BaseSession.check_response —
# статус-код 400 всегда даёт один и тот же класс TelegramBadRequest,
# отдельного класса на каждый текст нет; TelegramNotFound в aiogram
# маппится только на реальный HTTP 404, который Telegram для этих ошибок
# не отдаёт). Различить их можно только по exc.message (description).
_NAV_ANCHOR_GONE_MARKERS = ("message to edit not found", "message can't be edited")


async def open_flow(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Показать новый TRANSIENT корневой экран/сценарий (нет предыдущего
    сообщения бота, которое можно было бы отредактировать). Сначала
    гарантирует persistent navigation anchor (см. ensure_nav_anchor —
    no-op, если он уже существует; никогда не пересекается с тем, что
    делает этот метод дальше), затем удаляет то, что было отмечено как
    текущий TRANSIENT экран (RULE 2), затем триггер (RULE 1), затем
    отправляет новое сообщение и запоминает его как текущий TRANSIENT экран
    — все дальнейшие шаги (через step_from_text/step_from_callback) будут
    редактировать именно его, а не копиться новыми сообщениями.

    reply_markup здесь — ТОЛЬКО InlineKeyboardMarkup или None. Persistent
    reply-клавиатура — исключительно забота nav anchor'а (см. модуль-level
    docstring, ensure_nav_anchor, reset_nav_screen); TRANSIENT экран её
    никогда не несёт и не обязан её "освежать"."""
    await ensure_nav_anchor(message, state)

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


async def ensure_nav_anchor(message: Message, state: FSMContext) -> bool:
    """Гарантирует существование persistent NAV anchor'а — единственного
    источника постоянной reply-клавиатуры в чате (см. модуль-level
    docstring). Если anchor уже отслеживается в state.data — no-op: ничего
    не отправляет и не удаляет. Иначе — отправляет texts.WELCOME с
    reply_keyboard_for_chat() и запоминает его.

    НЕ использует zero-width-сообщения (см. UX-аудит: send-then-delete
    carrier оказался ненадёжен в реальном Telegram Desktop даже с
    is_persistent=True) — nav anchor остаётся живым сообщением всегда.

    Возвращает True, если anchor был только что создан этим вызовом (и
    поэтому уже показывает WELCOME — см. reset_nav_screen, которому не
    нужно затем ещё и редактировать его)."""
    data = await state.get_data()
    if data.get(_NAV_ANCHOR_MSG_KEY) and data.get(_NAV_ANCHOR_CHAT_KEY):
        return False
    await _create_nav_anchor(message, state)
    return True


async def _create_nav_anchor(message: Message, state: FSMContext) -> None:
    sent = await message.answer(texts.WELCOME, reply_markup=reply_keyboard_for_chat(message.chat.id))
    await state.update_data(**{_NAV_ANCHOR_MSG_KEY: sent.message_id, _NAV_ANCHOR_CHAT_KEY: sent.chat.id})


async def _delete_tracked_transient(message: Message, state: FSMContext) -> None:
    """Общая часть _cleanup_transient/cancel_transient: best-effort удалить
    ТОЛЬКО то TRANSIENT-сообщение, что реально отслеживается в state.data
    (если оно есть) и забыть его id. НЕ трогает FSM-состояние и не удаляет
    триггер — вызывающие стороны расходятся именно в этом (см. finish_flow
    в _cleanup_transient и его отсутствие в cancel_transient)."""
    data = await state.get_data()
    transient_id = data.get(_ANCHOR_MSG_KEY)
    transient_chat = data.get(_ANCHOR_CHAT_KEY)
    if transient_id and transient_chat:
        try:
            await message.bot.delete_message(chat_id=transient_chat, message_id=transient_id)
        except TelegramAPIError:
            pass
        await state.update_data(**{_ANCHOR_MSG_KEY: None, _ANCHOR_CHAT_KEY: None})


async def _cleanup_transient(message: Message, state: FSMContext) -> None:
    """Общая часть для reset_nav_screen и main_menu_cleanup: завершить
    активный сценарий (аварийный выход — RULE 2-совместимо), удалить
    текущий TRANSIENT экран (если был) и триггер. НЕ трогает NAV anchor
    вообще — это исключительно забота вызывающей стороны."""
    await finish_flow(state)
    await _delete_tracked_transient(message, state)
    await delete_trigger(message)


async def cancel_transient(message: Message, state: FSMContext) -> None:
    """Текстовый /cancel (см. bot/handlers/admin.py::admin_cancel_command)
    — best-effort очистка сообщений, аналог того, что инлайн "❌ Отмена"
    получает бесплатно за счёт callback.message.edit_text (см. admin_cancel:
    callback ВСЕГДА указывает на реальное текущее сообщение и редактирует
    его на месте — orphan там структурно невозможен). У текстовой команды
    такого указателя нет: Bot API не даёт message ссылку на "предыдущее
    сообщение бота, на которое это ответ", если пользователь не использовал
    явный Telegram reply (мастера admin на reply-цепочки не рассчитаны).
    Поэтому опираться можно только на то, что уже отслеживается в
    state.data (_ANCHOR_MSG_KEY/_ANCHOR_CHAT_KEY).

    В отличие от _cleanup_transient НЕ вызывает finish_flow(): вызывающий
    код (admin_cancel_command) сам ставит next_state из _resolve_cancel,
    который может быть НЕ None (например, AdminStates.edit_service_field_pick
    с сохранённым service_id) — принудительный сброс state здесь сломал бы
    это контекстное возвращение.

    Архитектурная граница (см. P1-3 диагностику): _ANCHOR_MSG_KEY сегодня
    обновляется только в open_root (/admin) и FAQ-add wizard (см.
    bot/handlers/admin.py) — остальные экраны используют прямой
    callback.message.edit_text/message.answer в обход flow.py и никогда
    его не обновляют. Поэтому эта функция гарантированно попадает в
    актуальный prompt, только пока сценарий ещё не прошёл ни одного
    СВОЕГО текстового/фото шага (первый вопрос мастера после чистой
    inline-навигации, и весь FAQ-add wizard). Если /cancel набран на
    втором и далее текстовом шаге одного мастера, _ANCHOR_MSG_KEY
    указывает на более старое, уже отработавшее сообщение, а не на
    actually текущий prompt — функция всё равно удаляет то, что
    отслеживается (устраняя хотя бы этот более старый "хвост"), но не
    способна закрыть более свежий orphan, чей id ей просто неоткуда
    узнать без миграции остальных admin.py-хендлеров на flow.py
    (сознательно вне scope этой задачи)."""
    await _delete_tracked_transient(message, state)
    await delete_trigger(message)


async def reset_nav_screen(message: Message, state: FSMContext) -> None:
    """Только реальный /start (см. bot/handlers/start.py::cmd_start —
    единственный вызывающий). "⌂ Главное меню" через это НЕ идёт (см.
    main_menu_cleanup ниже) — только /start показывает/пересоздаёт WELCOME
    через NAV anchor; повторные "Главное меню" NAV anchor не трогают
    вообще (см. UX-аудит: каждое такое касание — потенциальный источник
    дублирования при транзитных ошибках Bot API).

    Завершает активный сценарий, удаляет текущий TRANSIENT экран и
    триггер (см. _cleanup_transient), затем показывает приветствие ЧЕРЕЗ
    уже существующий NAV anchor (edit_message_text), а не новым
    сообщением — то самое сообщение, что несёт reply-клавиатуру, никогда
    не пересоздаётся без необходимости (см. ensure_nav_anchor).

    Если редактирование не удалось из-за того, что anchor реально пропал
    (например, пользователь удалил его вручную — Telegram это разрешает в
    приватных чатах) — best-effort пересоздаёт anchor, не роняя handler.
    "message is not modified" — штатный, не ошибочный случай, отдельно не
    пересоздаёт anchor.

    Пересоздание — ТОЛЬКО при однозначных признаках, что сообщения больше
    не существует (см. _NAV_ANCHOR_GONE_MARKERS). Любая другая ошибка
    (транзитная — flood control, eventual-consistency lag сразу после
    отправки и т.п.) — best-effort игнорируется, БЕЗ пересоздания: см.
    production-аудит — более широкое условие ("любая другая ошибка =
    anchor удалён") приводило к каскадному дублированию NAV anchor на
    транзитных ошибках edit_message_text, не связанных с реальным
    удалением сообщения."""
    await _cleanup_transient(message, state)

    if await ensure_nav_anchor(message, state):
        return  # только что отправлен этим же вызовом — уже показывает WELCOME

    data = await state.get_data()
    nav_id = data.get(_NAV_ANCHOR_MSG_KEY)
    nav_chat = data.get(_NAV_ANCHOR_CHAT_KEY)
    try:
        await message.bot.edit_message_text(texts.WELCOME, chat_id=nav_chat, message_id=nav_id)
    except TelegramAPIError as exc:
        error_text = exc.message.lower()
        if "not modified" in error_text:
            return
        if any(marker in error_text for marker in _NAV_ANCHOR_GONE_MARKERS):
            await _create_nav_anchor(message, state)
        # любая другая ошибка — best-effort, ничего не пересоздаём (см. docstring)


async def main_menu_cleanup(message: Message, state: FSMContext) -> None:
    """"⌂ Главное меню" (оба пути — см. bot/handlers/start.py::
    main_menu_or_confirm и main_menu_confirm) — ТОЛЬКО сброс сценария и
    очистка TRANSIENT-экрана (см. _cleanup_transient). NAV anchor
    оставляется КАК ЕСТЬ: не создаётся, не редактируется, не
    пересоздаётся — ни одного сетевого вызова к нему. Кнопка "Главное
    меню" физически не могла быть нажата, если NAV anchor (несущий её
    reply-клавиатуру) уже не существует — трогать его тут функционально
    незачем, а каждое касание (edit_message_text на каждый клик) было
    источником production-дублирования WELCOME при транзитных ошибках
    Bot API (см. UX-аудит)."""
    await _cleanup_transient(message, state)


async def finish_flow(state: FSMContext) -> None:
    """Завершить FSM-состояние сценария, не теряя id текущего экрана — чтобы
    следующий open_flow/open_root корректно удалил именно его (RULE 2)."""
    await state.set_state(None)


async def reset_state_keep_nav(state: FSMContext) -> None:
    """Безопасная замена state.clear() там, где вызывающему коду нужно
    сбросить FSM-состояние и ЛОКАЛЬНЫЕ данные сценария до "чистого листа",
    но НЕ трогать NAV anchor bookkeeping (P1-3 аудит, Batch 0).

    aiogram FSMContext.clear() == set_state(None) + set_data({}) — это
    стирает ВЕСЬ per-chat data dict, включая _NAV_ANCHOR_MSG_KEY/
    _NAV_ANCHOR_CHAT_KEY, хотя физическое NAV-сообщение (persistent
    reply-клавиатура) при этом никуда не девается. Следующий вызов
    ensure_nav_anchor() в любом месте этого же чата (следующий /start,
    /admin, /portfolio...) видел бы оба ключа отсутствующими и создавал бы
    ВТОРОЙ, дублирующий WELCOME с собственной reply-клавиатурой поверх уже
    существующего — ровно тот класс бага, ради которого вообще был
    разделён NAV/TRANSIENT anchor (см. докстринг модуля).

    Сохраняет ТОЛЬКО эти два ключа — TRANSIENT anchor
    (_ANCHOR_MSG_KEY/_ANCHOR_CHAT_KEY) стирается точно так же, как и раньше
    при state.clear(): ни один существующий вызывающий код в admin.py не
    полагается на его сохранность после сброса (единственное место,
    которому это было бы важно — FAQ-add wizard — уже сознательно
    использует state.set_state(None) вместо state.clear(), не эту
    функцию, см. bot/handlers/admin.py::faq_add_answer)."""
    data = await state.get_data()
    preserved = {
        key: data[key]
        for key in (_NAV_ANCHOR_MSG_KEY, _NAV_ANCHOR_CHAT_KEY)
        if key in data
    }
    await state.set_state(None)
    await state.set_data(preserved)


async def set_data_keep_nav(state: FSMContext, data: dict) -> None:
    """Безопасная замена state.set_data(data) там, где вызывающий код сам
    строит НОВЫЙ, полный data dict "с нуля" (не через update_data, который
    и так уже мержит, см. reset_state_keep_nav's докстринг) — но должен при
    этом сохранить NAV anchor bookkeeping (P1-3 аудит, продолжение Batch 0).

    В отличие от reset_state_keep_nav (которая отбрасывает ВСЁ, кроме NAV —
    подходит для "сценарий полностью завершён/отменён, локальный контекст
    больше не нужен"), эта функция для случая, где вызывающий код explicitly
    хочет сохранить СВОИ СОБСТВЕННЫЕ ключи (например, _resolve_cancel в
    bot/handlers/admin.py возвращает {"service_id": ...} или {"case_id": ...}
    — cancel должен вернуть на родительский экран С ЭТИМ контекстом, а не
    в пустоту) — reset_state_keep_nav здесь неприменима, она стёрла бы и
    их тоже (проверено эмпирически при аудите).

    Результат — ровно data ∪ {сохранённые NAV-ключи, если были}, НЕ обычный
    merge (текущие ключи, которых нет ни в data, ни среди NAV-ключей,
    отбрасываются — ровно то же поведение, что и у прямого
    state.set_data(data) сегодня, плюс сохранение NAV). Если NAV-ключей в
    текущем state не было — они не создаются искусственно."""
    current = await state.get_data()
    preserved = {
        key: current[key]
        for key in (_NAV_ANCHOR_MSG_KEY, _NAV_ANCHOR_CHAT_KEY)
        if key in current
    }
    await state.set_data({**data, **preserved})


async def open_root(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Как open_flow, но для входных точек нижнего меню/команд, которые
    показывают корневой экран без собственного FSM-состояния (Портфолио,
    Обо мне, /admin и т.п.) — это же и "аварийный выход" из любого зависшего
    сценария: одного нажатия достаточно, чтобы и увидеть нужный экран, и
    сбросить старое состояние (иначе следующее случайное сообщение could
    провалиться в хендлер, который всё ещё думает, что сценарий активен)."""
    await finish_flow(state)
    await open_flow(message, state, text, reply_markup)
