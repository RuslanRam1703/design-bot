"""Owner controls to block an individual Telegram user without quotas.

IDs are durable. Username-only watches are provisional; a handle may
change or be reassigned, so the screen explicitly warns the owner.
"""
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as K, Message

from bot import access_control, admin_keyboards, config, flow, texts
from bot.states import AccessStates

router = Router(name="admin_access")


def _owner_message(m: Message):
    return bool(config.DESIGNER_CHAT_ID) and str(m.chat.id) == config.DESIGNER_CHAT_ID


def _owner_callback(c: CallbackQuery):
    return bool(config.DESIGNER_CHAT_ID) and str(c.message.chat.id) == config.DESIGNER_CHAT_ID


router.message.filter(_owner_message)
router.callback_query.filter(_owner_callback)


def keyboard(rows):
    return K(inline_keyboard=[[B(text=label, callback_data=data) for label, data in row] for row in rows])


def home_keyboard():
    return keyboard([
        [("⛔ Заблокировать по ID или @username", "access:input")],
        [("📋 Список заблокированных", "access:list")],
        [("◀️ Админ-меню", "access:admin")],
    ])


def back_keyboard():
    return keyboard([[("◀️ Управление доступом", "access:root")]])


async def _home(cb: CallbackQuery, state: FSMContext):
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(cb, state,
        "👥 Пользователи и доступ\n\n"
        "Блокировка по ID надёжная. По @username — предварительная: имя "
        "может измениться или перейти другому человеку. Общих лимитов на заявки нет.",
        home_keyboard())


@router.callback_query(F.data == "access:root")
async def root(cb: CallbackQuery, state: FSMContext):
    await _home(cb, state)
    await cb.answer()


@router.callback_query(F.data == "access:admin")
async def admin_root(cb: CallbackQuery, state: FSMContext):
    await flow.reset_state_keep_nav(state)
    await flow.step_from_callback(cb, state, "Админ-меню:", admin_keyboards.admin_root_keyboard())
    await cb.answer()


@router.callback_query(F.data == "access:input")
async def input_prompt(cb: CallbackQuery, state: FSMContext):
    await flow.step_from_callback(cb, state,
        "Введите числовой Telegram ID или @username пользователя.\n"
        "Можно найти ID по ранее полученной заявке. "
        "По одному имени заранее подтвердить личность невозможно.",
        back_keyboard())
    await state.set_state(AccessStates.target_input)
    await cb.answer()


@router.message(AccessStates.target_input, F.text, ~F.text.startswith("/"), F.text != texts.MAIN_MENU_BUTTON)
async def receive_target(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.isascii() and raw.isdecimal() and len(raw) <= 16:
        user_id = int(raw)
        if user_id < 1 or str(user_id) == config.DESIGNER_CHAT_ID:
            await flow.step_from_text(message, state, "Этот ID нельзя блокировать.", back_keyboard())
            return
        text = f"Заблокировать пользователя с Telegram ID {user_id}?"
        choice = keyboard([[("⛔ Подтвердить блокировку", f"access:block:{user_id}")], [("◀️ Отмена", "access:root")]])
    else:
        username = access_control.normalize_username(raw)
        if not username:
            await flow.step_from_text(message, state, "Неверный ID или @username. Попробуйте ещё раз.", back_keyboard())
            return
        matches = await access_control.history_matches(username)
        if len(matches) == 1:
            match = matches[0]
            user_id = match["id"]
            text = (f"Найден в истории заявок: @{escape(username)}\n"
                    f"Имя: {escape(match['first_name'])}\n"
                    f"ID: {user_id}\n\n"
                    "Это историческая запись, пользователь мог изменить имя. "
                    "Проверьте, что это нужный человек.")
            choice = keyboard([[("⛔ Заблокировать найденный ID", f"access:block:{user_id}")],
                               [("◀️ Отмена", "access:root")]])
        else:
            reason = "Найдено несколько ID" if matches else "В истории заявок не найден"
            text = (f"@{escape(username)}: {reason}.\n\n"
                    "Можно добавить предварительную блокировку. При первом обращении "
                    "с этим именем бот запомнит ID и запретит доступ. "
                    "Если username сменится до обращения, правило не сработает. "
                    "Имя могло перейти другому владельцу.")
            choice = keyboard([[("⛔ Добавить предварительное правило", f"access:watch:{username}")],
                               [("◀️ Отмена", "access:root")]])
    await flow.step_from_text(message, state, text, choice)
    await state.set_state(None)


@router.callback_query(F.data.startswith("access:block:"))
async def block(cb: CallbackQuery, state: FSMContext):
    try:
        user_id = int(cb.data.rsplit(":", 1)[1])
        added = await access_control.block_id(cb.message.chat.id, user_id)
    except ValueError as exc:
        await cb.answer(str(exc), show_alert=True)
        return
    await flow.step_from_callback(cb, state,
        f"{'Заблокирован' if added else 'Уже заблокирован'} ID {user_id}. "
        "Его заявки, сообщения и нажатия кнопок больше не обрабатываются.",
        back_keyboard())
    await cb.answer()


@router.callback_query(F.data.startswith("access:watch:"))
async def watch(cb: CallbackQuery, state: FSMContext):
    username = cb.data.rsplit(":", 1)[1]
    try:
        added = await access_control.block_username(cb.message.chat.id, username)
    except ValueError as exc:
        await cb.answer(str(exc), show_alert=True)
        return
    await flow.step_from_callback(cb, state,
        f"{'Добавлено правило' if added else 'Правило уже существует'} для @{escape(username)}.\n"
        "Оно будет проверено при следующем обращении. При смене username до обращения "
        "правило не идентифицирует человека.",
        back_keyboard())
    await cb.answer()


@router.callback_query(F.data == "access:list")
async def blocked_list(cb: CallbackQuery, state: FSMContext):
    data = await access_control.view()
    ids = list(data.get("blocked_ids", {}).items())
    names = list(data.get("watch_usernames", {}))
    rows = []
    for uid, info in ids[-16:]:
        label = f"✅ Разблокировать {uid} ({('@'+info['username']) if info.get('username') else 'ID'})"
        rows.append([(label[:64], f"access:unblock:{uid}")])
    for name in names[-16:]:
        rows.append([(f"↩️ Убрать @{name}", f"access:unwatch:{name}")])
    rows.append([("◀️ Управление доступом", "access:root")])
    await flow.step_from_callback(cb, state,
        f"⛔ Заблокированные: {len(ids)}\n"
        f"Ожидающие @username: {len(names)}\n\n"
        "Ниже — последние записи. Нажатие разблокирует пользователя.",
        keyboard(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("access:unblock:"))
async def unblock(cb: CallbackQuery, state: FSMContext):
    try:
        uid = int(cb.data.rsplit(":", 1)[1])
        removed = await access_control.unblock_id(cb.message.chat.id, uid)
    except ValueError:
        await cb.answer("Некорректный ID", show_alert=True)
        return
    await flow.step_from_callback(cb, state,
        f"ID {uid}: {'доступ восстановлен' if removed else 'запись уже отсутствует'}.", back_keyboard())
    await cb.answer()


@router.callback_query(F.data.startswith("access:unwatch:"))
async def unwatch(cb: CallbackQuery, state: FSMContext):
    username = cb.data.rsplit(":", 1)[1]
    removed = await access_control.remove_watch(cb.message.chat.id, username)
    await flow.step_from_callback(cb, state,
        f"@{escape(username)}: {'правило удалено' if removed else 'правило уже отсутствует'}.",
        back_keyboard())
    await cb.answer()


@router.message(AccessStates.target_input, Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await flow.open_root(message, state, "Админ-меню:", admin_keyboards.admin_root_keyboard())
