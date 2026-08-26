"""Форматирование заявки (брифа) в читаемое сообщение для дизайнера.

Коды полей должны совпадать с тем, что присылает webapp/js/app.js в sendData —
см. HAVE_LABELS / DEADLINE_LABELS / BUDGET_LABELS ниже и комментарий в app.js.
"""

from html import escape as _esc

from bot.calculator import CalcResult

HAVE_LABELS = {
    "text": "готовый текст",
    "references": "референсы",
    "brand": "фирменный стиль",
    "materials": "готовые материалы",
    "old_design": "старый дизайн",
    "none": "ничего нет",
}

DEADLINE_LABELS = {
    "asap": "как можно скорее",
    "2weeks": "1–2 недели",
    "month": "в течение месяца",
    "unknown": "срок не определён",
}

BUDGET_LABELS = {
    "lt20": "до 20 000 ₽",
    "20-40": "20 000–40 000 ₽",
    "40-70": "40 000–70 000 ₽",
    "70-100": "70 000–100 000 ₽",
    "gt100": "более 100 000 ₽",
    "undecided": "не определился",
}

# lead["materials"][]["kind"] -> отображаемое название. Единственный источник
# истины для подписи типа материала — переиспользуется и в карточке заявки
# (format_lead_admin_detail ниже), и в экране "Материалы" (bot/handlers/
# admin.py::lead_materials_open), чтобы не разойтись при добавлении нового
# типа (Stage B: video/animation).
MATERIAL_KIND_LABELS = {
    "document": "файл",
    "photo": "фото",
    "video": "видео",
    "animation": "GIF/анимация",
}

# "direct" (обычный заход в заявку) не показываем — это большинство заявок,
# и указывать источник имеет смысл только там, где он несёт сигнал: клиент
# пришёл "разогретым" с конкретного кейса/расчёта, а не просто открыл бриф.
SOURCE_LABELS = {
    "calculator": "через калькулятор",
    "about": "со страницы «Обо мне»",
}


# Потолок на КАЖДОЕ свободное поле клиента в уведомлении дизайнеру.
#
# Зачем отдельный механизм, а не уже существующий _clamp_to_telegram_limit
# (см. ниже): тот режет ЦЕЛЫМИ строками с конца и создан для админской
# карточки, длина которой набегает из МНОГИХ строк (supplements/materials/
# owner_messages). Здесь же переполнение даёт ОДНА строка — "<b>Задача:</b>
# " плюс сколько угодно текста клиента. Построчная обрезка такую строку не
# укоротит, а выбросит целиком: замерено — уведомление из трёх строк с
# 20 000 символов в середине превращалось в 95 символов, где от заявки не
# оставалось ничего. Это было бы хуже нынешнего бага: сообщение выглядит
# нормальным, но не содержит запроса клиента, и в логах при этом чисто.
#
# 1000 символов на поле: обычная заявка (десятки-сотни символов) не
# задевается вообще, а шесть полей по 1000 плюс разметка дают ~6 КБ —
# заведомо конечную величину, которую финальная сетка ниже дожимает до
# лимита Telegram.
_MAX_FIELD_LENGTH = 1000

_CLIP_MARKER = "… (обрезано, полный текст — в «Заявки»)"


def _clip(value: str) -> str:
    """Ограничивает свободный текст клиента ДО html-экранирования.

    Порядок принципиален: обрезка уже экранированной строки может разрубить
    HTML-сущность пополам ("A&amp;" -> "A&am"), Telegram отвергнет такое
    сообщение с parse_mode="HTML", и дизайнер снова молча не получит
    заявку — ровно тот сбой, который здесь и чинится. На сыром тексте
    сущностей ещё нет, а _esc() после обрезки экранирует и текст, и маркер
    целиком."""
    if not value or len(value) <= _MAX_FIELD_LENGTH:
        return value
    return value[:_MAX_FIELD_LENGTH] + _CLIP_MARKER


def format_lead_message(payload: dict, calc: CalcResult | None, lead_id: int, from_user_id: int, username: str | None) -> str:
    lines = [f"🆕 <b>Новая заявка #{lead_id}</b>", ""]

    service_name = _clip(payload.get("service_name") or "не указана")
    lines.append(f"<b>Услуга:</b> {_esc(service_name)}")

    source = payload.get("source")
    source_case_title = payload.get("source_case_title")
    if source == "case" and source_case_title:
        lines.append(f"<b>Источник:</b> кейс «{_esc(_clip(source_case_title))}» — похожий проект")
    elif source in SOURCE_LABELS:
        lines.append(f"<b>Источник:</b> {SOURCE_LABELS[source]}")

    task = _clip((payload.get("task_description") or "").strip())
    if task:
        lines.append(f"<b>Задача:</b> {_esc(task)}")

    have = payload.get("have") or []
    have_text = _clip(", ".join(HAVE_LABELS.get(h, h) for h in have)) or "не указано"
    # _esc обязателен: HAVE_LABELS.get(h, h) возвращает СЫРОЕ значение
    # клиента, если ключ неизвестен, — то есть в сообщение с
    # parse_mode="HTML" попадал произвольный HTML клиента. "<b>x</b>"
    # отрисовывался как настоящая разметка, а незакрытый "<b" ломал разбор
    # целиком: Telegram отвергал сообщение, ошибка гасилась в webserver.py,
    # и дизайнер молча не получал заявку — ровно тот же сбой, что чинил M1.
    lines.append(f"<b>Что уже есть:</b> {_esc(have_text)}")

    deadline = DEADLINE_LABELS.get(payload.get("deadline"), "не указано")
    lines.append(f"<b>Когда нужно:</b> {deadline}")

    budget = BUDGET_LABELS.get(payload.get("budget"), "не указано")
    lines.append(f"<b>Бюджет:</b> {budget}")

    contact = _clip((payload.get("contact") or "").strip())
    if contact:
        lines.append(f"<b>Контакт:</b> {_esc(contact)}")

    if payload.get("attach_tz"):
        lines.append("<b>ТЗ:</b> клиент пришлёт файл следующим сообщением")

    tz_details = payload.get("tz_details")
    if tz_details:
        lines.append("")
        lines.append("<b>Техническое задание (от клиента):</b>")
        if tz_details.get("goal"):
            lines.append(f"— Цель: {_esc(_clip(tz_details['goal']))}")
        if tz_details.get("must_have"):
            lines.append(f"— Обязательно: {_esc(_clip(tz_details['must_have']))}")
        if tz_details.get("avoid"):
            lines.append(f"— Избегать: {_esc(_clip(tz_details['avoid']))}")
        if tz_details.get("references"):
            lines.append(f"— Референсы: {_esc(_clip(tz_details['references']))}")

    if calc and calc.valid:
        lines.append("")
        lines.append("<b>Расчёт из калькулятора:</b>")
        # service_name приходит из pricing.json, который дизайнер правит
        # через /admin — то есть тоже произвольный текст, способный
        # сломать HTML-разбор собственного уведомления.
        lines.append(f"— {_esc(calc.service_name)}: {calc.price_from:,} – {calc.price_to:,} ₽".replace(",", " "))
        lines.append(f"— срок: {_fmt_days(calc.term_from)}–{_fmt_days(calc.term_to)} дн.")
        if calc.selected_options:
            opts = ", ".join(
                f"{o['name']}" + (f" ×{o['qty']}" if o["qty"] > 1 else "") for o in calc.selected_options
            )
            lines.append(f"— опции: {opts}")
        flags = []
        if calc.urgent:
            flags.append("срочно")
        if calc.complex_:
            flags.append("высокая сложность")
        if flags:
            lines.append(f"— отметки: {', '.join(flags)}")

    lines.append("")
    username_part = f"@{username}" if username else "нет username"
    lines.append(f"<i>От пользователя {username_part}, id {from_user_id}</i>")

    # Финальная сетка поверх пофайловых потолков: сами по себе _clip выше
    # уже делают длину конечной, поэтому здесь это почти всегда no-op — но
    # полей много, они складываются, и превысить лимит суммой всё ещё
    # теоретически возможно. Раньше этой проверки не было вовсе: Telegram
    # отвергал слишком длинное сообщение, ошибка гасилась в webserver.py
    # (except Exception + logger), клиент получал HTTP 200, а дизайнер не
    # узнавал о заявке вообще.
    return _clamp_to_telegram_limit(lines)


def _fmt_days(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


# Ключи должны совпадать с тем, что шлёт webapp/js/app.js::submitSupplement
# (fields.comment/additional_requirements/references/contact) — намеренно
# НЕ весь набор полей Order Builder (service/deadline/budget/have/urgent/
# complex/calc), см. аудит: изменение состава заказа — это новая заявка,
# не дополнение.
SUPPLEMENT_FIELD_LABELS = {
    "comment": "Что добавить/изменить",
    "additional_requirements": "Дополнительные требования",
    "references": "Референсы",
    "contact": "Контакты",
}


def format_lead_supplement_message(lead_id: int, fields: dict) -> str:
    """Уведомление владельцу о дополнении к уже существующей заявке — НЕ
    формат "Новая заявка" (см. аудит: спутанное с созданием уведомление
    было одним из найденных багов), только номер заявки и то, что реально
    прислано в этом дополнении."""
    lines = [f"✏️ <b>Дополнение к заявке #{lead_id}</b>", ""]
    for key, label in SUPPLEMENT_FIELD_LABELS.items():
        value = (fields.get(key) or "").strip()
        if value:
            lines.append(f"<b>{label}:</b> {_esc(_clip(value))}")
    # Та же финальная сетка, что и у format_lead_message выше.
    return _clamp_to_telegram_limit(lines)


def format_material_message(lead_id: int) -> str:
    """Текст перед пересылкой файла (message.forward() не умеет добавлять
    caption — Bot API этого не поддерживает, поэтому номер заявки уходит
    отдельным сообщением непосредственно перед форвардом, см. аудит)."""
    return f"📎 <b>Материал к заявке #{lead_id}</b>"


STATUS_LABELS = {
    "NEW": "🆕 Новая",
    "VIEWED": "👀 Просмотрена",
    "IN_PROGRESS": "💬 В работе",
    "WAITING_CLIENT": "⏸ Ожидание клиента",
    "DONE": "✅ Завершена",
    "CANCELLED": "❌ Отменена",
}

# Отдельный словарь от STATUS_LABELS выше — тот для админа (карточка в
# /admin, более технические формулировки), этот — то, что видит клиент и в
# Telegram-уведомлении, и в Mini App ("Мои заявки"). Формулировки должны
# совпадать буква в букву с webapp/js/app.js::MY_LEAD_STATUS_LABELS —
# иначе текст уведомления разойдётся с тем, что клиент увидит, открыв
# Mini App (см. аудит про статусы/уведомления).
CLIENT_STATUS_LABELS = {
    "NEW": "🆕 Заявка получена",
    "VIEWED": "👀 На рассмотрении",
    "IN_PROGRESS": "💬 В работе",
    "WAITING_CLIENT": "⏸ Нужно ваше действие",
    "DONE": "✅ Завершено",
    "CANCELLED": "❌ Отменено",
}


def format_status_notification(service_name: str | None, status: str) -> str:
    """lead.id намеренно НЕ используется здесь — это глобальный сквозной
    счётчик по ВСЕМ заявкам от ВСЕХ клиентов (см. content_store.add_lead),
    а не персональный номер заказа клиента; показывать его в уведомлении
    значило бы невольно раскрывать общий объём заявок в системе (см.
    UX-аудит). service_name уже собран на этапе submit — короткий,
    человекочитаемый, всегда либо реальная услуга, либо явный "Не
    определился с услугой", так что практически никогда не пуст; пустая
    строка/None — запасной случай (повреждённые/очень старые данные), а не
    ожидаемый сценарий."""
    label = CLIENT_STATUS_LABELS.get(status, status)
    name = (service_name or "").strip() or "Ваша заявка"
    return f"Ваша заявка обновлена\n{name}\nСтатус: {label}"


def format_lead_admin_detail(lead: dict) -> str:
    """Карточка заявки для /admin -> Заявки -> конкретная заявка. В отличие
    от format_lead_message (мгновенное уведомление в момент отправки), эта
    версия строится из уже сохранённого lead (content_store.get_lead) и
    дополнительно показывает Telegram identity, статус и даты — то, чего
    в разовом уведомлении не было и не должно было быть."""
    payload = lead["payload"]
    telegram = lead.get("telegram") or {}
    calc = lead.get("calc_summary")

    lines = [f"📋 <b>Заявка #{lead['id']}</b> — {STATUS_LABELS.get(lead['status'], lead['status'])}", ""]

    lines.append("<b>Клиент</b>")
    full_name = " ".join(filter(None, [telegram.get("first_name"), telegram.get("last_name")])) or "не указано"
    lines.append(f"— Имя в Telegram: {_esc(full_name)}")
    username = telegram.get("username")
    lines.append(f"— @{_esc(username)}" if username else "— username не указан")
    if telegram.get("user_id"):
        lines.append(f"— Telegram ID: <code>{telegram['user_id']}</code>")
    contact = (payload.get("contact") or "").strip()
    if contact:
        lines.append(f"— Контакт из формы: {_esc(contact)}")

    lines.append("")
    lines.append("<b>Заказ</b>")
    lines.append(f"— Услуга: {_esc(payload.get('service_name') or 'не указана')}")
    source = payload.get("source")
    source_case_title = payload.get("source_case_title")
    if source == "case" and source_case_title:
        lines.append(f"— Источник: кейс «{_esc(source_case_title)}» — похожий проект")
    elif source in SOURCE_LABELS:
        lines.append(f"— Источник: {SOURCE_LABELS[source]}")
    else:
        lines.append("— Источник: прямой заход в заявку")

    if calc:
        lines.append(f"— Расчёт: {calc['price_from']:,} – {calc['price_to']:,} ₽".replace(",", " ") + f", срок {_fmt_days(calc['term_from'])}–{_fmt_days(calc['term_to'])} дн.")
        if calc.get("selected_options"):
            opts = ", ".join(f"{o['name']}" + (f" ×{o['qty']}" if o["qty"] > 1 else "") for o in calc["selected_options"])
            lines.append(f"— Опции: {opts}")
        flags = [f for f, v in (("срочно", calc.get("urgent")), ("высокая сложность", calc.get("complex_"))) if v]
        if flags:
            lines.append(f"— Отметки: {', '.join(flags)}")

    task = (payload.get("task_description") or "").strip()
    if task:
        lines.append(f"— Задача: {_esc(task)}")
    have = payload.get("have") or []
    if have:
        lines.append(f"— Что уже есть: {_esc(', '.join(HAVE_LABELS.get(h, h) for h in have))}")
    if payload.get("deadline"):
        # Здесь fallback — САМО значение клиента (в отличие от
        # format_lead_message, где подставляется константа "не указано"),
        # поэтому неизвестный ключ отдавал сырой клиентский текст прямо в
        # HTML-сообщение. Та же причина, что и у have выше.
        lines.append(f"— Когда нужно: {_esc(DEADLINE_LABELS.get(payload['deadline'], payload['deadline']))}")
    if payload.get("budget"):
        lines.append(f"— Бюджет: {_esc(BUDGET_LABELS.get(payload['budget'], payload['budget']))}")

    supplements = lead.get("supplements") or []
    if supplements:
        lines.append("")
        lines.append(f"<b>Дополнения ({len(supplements)})</b>")
        for s in supplements:
            ts = (s.get("created_at") or "")[:16].replace("T", " ")
            lines.append(f"— #{s['id']} ({ts}):")
            for key, label in SUPPLEMENT_FIELD_LABELS.items():
                value = (s.get("fields", {}).get(key) or "").strip()
                if value:
                    lines.append(f"   {label}: {_esc(value)}")

    materials = lead.get("materials") or []
    if materials:
        lines.append("")
        lines.append(f"<b>Материалы ({len(materials)})</b>")
        source_labels = {"new": "при создании", "supplement": "из дополнения"}
        for m in materials:
            ts = (m.get("received_at") or "")[:16].replace("T", " ")
            kind = MATERIAL_KIND_LABELS.get(m.get("kind"), m.get("kind"))
            source = source_labels.get(m.get("source"), m.get("source"))
            lines.append(f"— {kind}, {ts} ({source})")

    owner_messages = lead.get("owner_messages") or []
    if owner_messages:
        lines.append("")
        lines.append(f"<b>Ответы дизайнера ({len(owner_messages)})</b>")
        for m in reversed(owner_messages):  # последний сверху — самое актуальное видно сразу
            ts = (m.get("sent_at") or "")[:16].replace("T", " ")
            failed_note = " ⚠️ не доставлено" if m.get("delivery_status") == "failed" else ""
            lines.append(f"— #{m['id']} ({ts}){failed_note}: {_esc(m.get('text', ''))}")

    lines.append("")
    lines.append(f"<i>Создана: {lead['created_at'][:16].replace('T', ' ')}</i>")
    if lead.get("updated_at"):
        lines.append(f"<i>Обновлена: {lead['updated_at'][:16].replace('T', ' ')}</i>")

    return _clamp_to_telegram_limit(lines)


# Presentation-only defensive лимит (E2E MVP audit, Batch 4) — при большом
# накоплении supplements/materials/owner_messages итоговая карточка могла
# превысить Telegram-лимит на длину сообщения (4096 символов), и
# callback.message.edit_text() падал необработанным TelegramBadRequest.
# Ничего не удаляется из lead в storage — только из возвращаемой строки.
_MAX_DETAIL_LENGTH = 4000  # запас под саму пометку об обрезке ниже


def _clamp_to_telegram_limit(lines: list[str]) -> str:
    text = "\n".join(lines)
    if len(text) <= _MAX_DETAIL_LENGTH:
        return text
    # Обрезаем ЦЕЛЫМИ строками с конца, а не посередине строки — каждая
    # строка выше самодостаточна (открывающий и закрывающий HTML-тег всегда
    # в одной строке), поэтому такая обрезка не может разорвать тег и
    # сломать parse_mode="HTML". owner_messages уже добавлены в порядке
    # "новые сверху" — обрезка с конца в первую очередь убирает более
    # старые записи, т.е. по возможности сохраняет самую свежую информацию.
    notice = "\n\n<i>… часть истории скрыта, показаны не все записи (лимит длины сообщения)</i>"
    truncated = list(lines)
    while truncated and len("\n".join(truncated)) + len(notice) > _MAX_DETAIL_LENGTH:
        truncated.pop()
    return "\n".join(truncated) + notice
