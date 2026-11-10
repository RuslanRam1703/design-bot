import dataclasses
import json
import logging

from aiogram import F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from bot import config, content_store, texts, client_materials_archive
from bot.calculator import calculate
from bot.data import load_pricing
from bot.handlers.start import relay_client_media_to_designer
from bot.lead import format_lead_message, format_material_message
from bot.notifications import notify_designer_lead

router = Router(name="webapp")
logger = logging.getLogger(__name__)


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message) -> None:
    """Legacy-путь: Telegram.WebApp.sendData() официально работает только
    для Mini App, запущенного через KeyboardButton.web_app
    (https://core.telegram.org/bots/webapps) — мы ушли от этой кнопки ради
    initData (см. bot/keyboards.py), поэтому submitBrief() теперь шлёт
    POST /api/leads (bot/webserver.py::handle_create_lead), а не sendData().
    Хендлер оставлен нетронутым как fallback — на случай, если что-то в
    будущем всё же вызовет sendData() (например, старый закэшированный
    app.js у части клиентов до следующего обращения к серверу)."""
    try:
        payload = json.loads(message.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Не удалось разобрать web_app_data: %r", message.web_app_data.data)
        await message.answer("Не получилось обработать данные из мини-приложения.")
        return

    action = payload.get("action")
    if action == "submit_brief":
        await _handle_brief_submission(message, payload)
    else:
        logger.warning("Неизвестное action из webapp: %r", action)


async def _handle_brief_submission(message: Message, payload: dict) -> None:
    calc_result = None
    calc_payload = payload.get("calc")
    if calc_payload and calc_payload.get("service_id"):
        pricing = await content_store.read_async("pricing.json") if content_store.is_redis_backed() else load_pricing()
        calc_result = calculate(
            pricing=pricing,
            service_id=calc_payload["service_id"],
            selected=calc_payload.get("options", []),
            urgent=bool(calc_payload.get("urgent")),
            complex_=bool(calc_payload.get("complex")),
        )

    user = message.from_user
    # Не требуем username — Telegram user_id уже достаточен как identity и
    # как основной канал ответа через бота (см. /admin -> Заявки).
    telegram_identity = {
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    calc_summary = dataclasses.asdict(calc_result) if calc_result and calc_result.valid else None
    # draft_id — сгенерирован Mini App вместе с черновиком (localStorage) и
    # используется ТОЛЬКО для upsert повторного/дублирующегося submit ОДНОГО
    # И ТОГО ЖЕ ещё не изменённого черновика (не для "Дополнить информацию" —
    # это отдельный supplement-режим, см. bot/webserver.py::
    # _handle_lead_supplement и аудит). add_lead() сама выставляет
    # awaiting_tz_file из payload["attach_tz"] — см. bot/content_store.py,
    # тот же persistent-механизм, что и для основного HTTP-пути
    # (handle_tz_file ниже не различает, откуда взялась заявка).
    lead = await content_store.add_lead(payload, telegram_identity, calc_summary, draft_id=payload.get("draft_id"))

    # Both legacy and HTTP submission use the same persisted delivery state.
    if lead["created"] or lead.get("changed") or (lead.get("designer_notification") or {}).get("state") != "sent":
        await notify_designer_lead(message.bot, lead)

    price_range = None
    if calc_summary:
        price_range = f"{calc_summary['price_from']:,} – {calc_summary['price_to']:,} ₽".replace(",", " ")

    if payload.get("attach_tz"):
        await message.answer(texts.lead_ack_ask_file(lead["id"], payload.get("service_name"), price_range))
    else:
        await message.answer(texts.lead_received_ack(lead["id"], payload.get("service_name"), price_range))



# Native client lead navigation: callbacks are resolved from the authenticated
# Telegram sender, never from a user_id supplied by a client.
def _lead_nav(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "clientleads:list")
async def client_leads_list(callback: CallbackQuery) -> None:
    leads = await content_store.list_leads_by_user(callback.from_user.id)
    rows = [
        [InlineKeyboardButton(
            text=f"{(lead.get('payload') or {}).get('service_name') or 'Заявка'} · {lead.get('status', 'NEW')}",
            callback_data=f"clientleads:open:{lead['id']}",
        )]
        for lead in leads[:20]
    ]
    rows.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="clientleads:home")])
    await callback.message.edit_text(
        "Ваши заявки. Выберите заявку:" if leads else "У вас пока нет заявок.",
        reply_markup=_lead_nav(rows),
    )
    await callback.answer()


@router.callback_query(F.data == "clientleads:home")
async def client_leads_home(callback: CallbackQuery) -> None:
    from bot.keyboards import client_home_keyboard
    await callback.message.edit_text(texts.CLIENT_HOME, reply_markup=client_home_keyboard(config.WEBAPP_URL))
    await callback.answer()


@router.callback_query(F.data.startswith("clientleads:open:"))
async def client_leads_open(callback: CallbackQuery) -> None:
    try:
        lead_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    lead = next((item for item in await content_store.list_leads_by_user(callback.from_user.id) if item["id"] == lead_id), None)
    if lead is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    payload = lead.get("payload") or {}
    materials = lead.get("materials") or []
    description = (payload.get("task_description") or "Не указано").strip()
    if len(description) > 900:
        description = description[:897] + "..."
    price = lead.get("calc_summary") or {}
    price_text = ""
    if isinstance(price, dict) and price.get("price_from") is not None:
        price_text = f"\\nРасчёт: {price['price_from']}–{price.get('price_to', price['price_from'])} ₽"
    text = (
        f"Заявка №{lead_id}\\n"
        f"Услуга: {payload.get('service_name') or 'Не указана'}\\n"
        f"Статус: {lead.get('status', 'NEW')}\\n"
        f"Задача: {description}{price_text}\\n"
        f"Материалы: {len(materials)}"
    ).replace("\\n", "\n")
    rows = []
    if lead.get("status") not in content_store.TERMINAL_LEAD_STATUSES:
        rows.append([InlineKeyboardButton(text="📎 Добавить материалы", callback_data=f"clientleads:attach:{lead_id}")])
    if materials:
        rows.append([InlineKeyboardButton(text="📁 Посмотреть материалы", callback_data=f"clientleads:files:{lead_id}")])
    if lead.get("supplements"):
        rows.append([InlineKeyboardButton(text="📝 История дополнений", callback_data=f"clientleads:changes:{lead_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Мои заявки", callback_data="clientleads:list")])
    await callback.message.edit_text(text, reply_markup=_lead_nav(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("clientleads:attach:"))
async def client_leads_attach(callback: CallbackQuery) -> None:
    try:
        lead_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    lead = next((item for item in await content_store.list_leads_by_user(callback.from_user.id) if item["id"] == lead_id), None)
    if lead is None or lead.get("status") in content_store.TERMINAL_LEAD_STATUSES:
        await callback.answer("Заявка недоступна для загрузки", show_alert=True)
        return
    await content_store.begin_lead_material_upload(callback.from_user.id, lead_id)
    await callback.message.edit_text(
        f"Отправьте фото, видео или документ к заявке №{lead_id} следующим сообщением. "
        "После получения файла вы сможете добавить следующий.",
        reply_markup=_lead_nav([[InlineKeyboardButton(text="Отмена", callback_data=f"clientleads:stop:{lead_id}")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("clientleads:stop:"))
async def client_leads_stop(callback: CallbackQuery) -> None:
    lead_id = int(callback.data.rsplit(":", 1)[1])
    await content_store.stop_lead_material_upload(callback.from_user.id, lead_id)
    await callback.message.edit_text("Добавление файлов отменено.", reply_markup=_lead_nav([[InlineKeyboardButton(text="◀️ К заявке", callback_data=f"clientleads:open:{lead_id}")]]))
    await callback.answer()



@router.callback_query(F.data.startswith("clientleads:changes:"))
async def client_leads_changes(callback: CallbackQuery) -> None:
    try:
        lead_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная заявка", show_alert=True)
        return
    lead = next((item for item in await content_store.list_leads_by_user(callback.from_user.id) if item["id"] == lead_id), None)
    if lead is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    labels = {"pending": "На рассмотрении", "approved": "Согласовано", "needs_clarification": "Требует уточнения", "not_agreed": "Не согласовано"}
    supplements = lead.get("supplements") or []
    lines = [f"Дополнения к заявке №{lead_id}"]
    for supplement in supplements[-10:]:
        status = labels.get(supplement.get("review_status", "pending"), "На рассмотрении")
        fields = supplement.get("fields") or {}
        preview = " · ".join(str(value)[:110] for value in fields.values() if value)[:240]
        lines.append(f"№{supplement['id']} · {status}\\n{preview or 'Дополнение с материалами'}")
    await callback.message.edit_text(
        "\\n\\n".join(lines).replace("\\n", "\n"),
        reply_markup=_lead_nav([[InlineKeyboardButton(text="◀️ К заявке", callback_data=f"clientleads:open:{lead_id}")]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("clientleads:files:"))
async def client_leads_files(callback: CallbackQuery) -> None:
    lead_id = int(callback.data.rsplit(":", 1)[1])
    lead = next((item for item in await content_store.list_leads_by_user(callback.from_user.id) if item["id"] == lead_id), None)
    if lead is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    materials = lead.get("materials") or []
    for material in materials[:20]:
        archive_ref = material.get("archive_ref")
        if isinstance(archive_ref, dict) and archive_ref.get("chat_id") and archive_ref.get("message_id"):
            try:
                await callback.bot.copy_message(
                    chat_id=callback.from_user.id,
                    from_chat_id=archive_ref["chat_id"],
                    message_id=archive_ref["message_id"],
                )
                continue
            except Exception:
                logger.exception("Не удалось восстановить файл из приватного архива заявки %s", lead_id)
        file_id = material.get("file_id")
        if not file_id:
            continue
        kind = material.get("kind")
        try:
            if kind == "photo":
                await callback.bot.send_photo(callback.from_user.id, file_id)
            elif kind == "video":
                await callback.bot.send_video(callback.from_user.id, file_id)
            elif kind == "animation":
                await callback.bot.send_animation(callback.from_user.id, file_id)
            else:
                await callback.bot.send_document(callback.from_user.id, file_id)
        except Exception:
            logger.exception("Не удалось показать материал заявки %s", lead_id)
    await callback.answer("Материалы отправлены в этот чат" if materials else "Материалов пока нет")


@router.message(F.document | F.photo | F.video | F.animation)
async def handle_tz_file(message: Message) -> None:
    """Не привязано к FSM-состоянию (раньше — BriefStates.awaiting_tz_file,
    терялось при рестарте бота) — вместо этого ищем заявку САМОГО этого
    отправителя (message.from_user.id — всегда надёжный Telegram-identity,
    как и для создания заявки), которая ждёт файл — см.
    content_store.find_lead_awaiting_file. Чужую заявку так найти
    невозможно: фильтр по user_id, не по произвольному id из сообщения.
    add_lead/add_lead_supplement поддерживают инвариант "максимум одна
    ожидающая заявка на клиента" (см. content_store._clear_other_awaiting и
    аудит) — при нескольких заявках одного клиента файл не может уйти не
    туда. Если ждущей заявки нет — просто ничего не делаем, документ/фото/
    видео/анимация не наш случай (например, админ шлёт архив бэкапа — это
    отдельный, FSM-scoped хендлер в admin.py, который стоит раньше в
    порядке роутеров и перехватит его первым).

    video/animation (Stage B) — тот же паттерн, что document/photo: только
    file_id/file_unique_id, сам файл не скачивается (см. модуль-докстринг
    content_store.record_lead_material). Message.document/.photo/.video/
    .animation взаимно исключающие поля одного и того же Update — Telegram
    заполняет ровно одно из них на сообщение, порядок проверок ниже не
    влияет на корректность."""
    lead = await content_store.find_lead_awaiting_file(message.from_user.id)
    if lead is None:
        # Ожидающей заявки нет — материал не записываем и заявку не трогаем
        # (это и есть защита от перехвата посторонних файлов). Но и молча
        # терять файл нельзя: раньше он исчезал бесследно, без ответа
        # клиенту и без единого следа у дизайнера. Пересылаем как обычное
        # обращение — тем же способом, что и свободный текст клиента.
        # Внутри есть та же защита от собственных файлов дизайнера
        # (DESIGNER_CHAT_ID), что и у текстового relay.
        await relay_client_media_to_designer(message)
        return

    if message.document:
        file_id, file_unique_id, kind = message.document.file_id, message.document.file_unique_id, "document"
    elif message.video:
        file_id, file_unique_id, kind = message.video.file_id, message.video.file_unique_id, "video"
    elif message.animation:
        file_id, file_unique_id, kind = message.animation.file_id, message.animation.file_unique_id, "animation"
    else:
        photo = message.photo[-1]
        file_id, file_unique_id, kind = photo.file_id, photo.file_unique_id, "photo"
    source = lead.get("awaiting_tz_file_source") or "new"
    # Материал сохраняем ДО пересылки — если пересылка упадёт (Telegram API
    # недоступен и т.п.), связь "файл ↔ заявка" всё равно не теряется, её
    # можно будет восстановить вручную через file_id из /admin.
    archive_ref = None
    if config.CLIENT_MATERIALS_CHANNEL:
        try:
            archive_ref = await client_materials_archive.archive_client_material(
                message.bot, file_id=file_id, kind=kind, lead_id=lead["id"],
            )
        except client_materials_archive.ClientArchiveUnavailable:
            # Do not discard the original Telegram file_id when archive delivery fails.
            logger.exception("Материал заявки %s не удалось скопировать в архив", lead["id"])
    stored = await content_store.record_lead_material(
        lead["id"], file_id, file_unique_id, kind, source,
        user_id=message.from_user.id, archive_ref=archive_ref,
    )
    if not stored:
        await message.answer("Заявка больше не принимает материалы. Файл не прикреплён.")
        return

    if config.DESIGNER_CHAT_ID:
        try:
            # message.forward() не умеет добавлять caption (ограничение Bot
            # API) — номер заявки уходит отдельным сообщением перед форвардом,
            # иначе пересланный файл в чате владельца ничем не отличался бы
            # от материала любой другой заявки (см. аудит).
            await message.bot.send_message(
                chat_id=config.DESIGNER_CHAT_ID, text=format_material_message(lead["id"]), parse_mode="HTML",
            )
            await message.forward(chat_id=config.DESIGNER_CHAT_ID)
        except Exception:
            logger.exception("Не удалось переслать материал дизайнеру (lead #%s)", lead["id"])
    await message.answer(
        texts.TZ_FILE_FORWARDED,
        reply_markup=_lead_nav([
            [InlineKeyboardButton(text="📎 Добавить ещё", callback_data=f"clientleads:attach:{lead['id']}")],
            [InlineKeyboardButton(text="✅ Готово · К заявке", callback_data=f"clientleads:open:{lead['id']}")],
        ]),
    )


@router.message(F.voice | F.video_note | F.sticker)
async def handle_unsupported_tz_media(message: Message) -> None:
    """Узкий fallback (Stage C Batch 1) — раньше voice/video_note/sticker,
    присланные пока заявка ждёт файл (awaiting_tz_file), не совпадали ни с
    одним хендлером ни в одном router (см. handle_tz_file выше — фильтр
    только document/photo/video/animation) и просто пропадали: ни материал
    не записывался, ни ответ клиенту не уходил, ни awaiting_tz_file не
    снимался — тихий тупик (Stage C аудит).

    Зарегистрирован в этом же router СРАЗУ ПОСЛЕ handle_tz_file — Telegram
    заполняет у Message ровно одно из document/photo/video/animation/voice/
    video_note/sticker на сообщение, так что эти два фильтра никогда не
    матчат одно и то же обновление, порядок регистрации здесь не влияет на
    выбор между ними, только гарантирует, что webapp.router (см. bot/main.py,
    раньше faq/start) вообще получит шанс их обработать.

    Тот же guard, что и у handle_tz_file (find_lead_awaiting_file по
    message.from_user.id) — единственная причина, по которой это не задевает
    ни посторонние сообщения клиента (нет ожидающей заявки -> no-op), ни
    дизайнера/админа (у него в норме нет своей заявки, ожидающей файл, тем
    же принципом, что и у handle_tz_file, отдельная проверка на
    DESIGNER_CHAT_ID не нужна). Материал НЕ записывается, ничего не
    пересылается, awaiting_tz_file НЕ снимается — заявка остаётся ждать
    подходящий файл, клиент только получает понятное объяснение, что
    прислать вместо этого."""
    lead = await content_store.find_lead_awaiting_file(message.from_user.id)
    if lead is None:
        return
    await message.answer(texts.TZ_FILE_UNSUPPORTED_TYPE)
