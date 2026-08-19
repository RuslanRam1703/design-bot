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

# "direct" (обычный заход в заявку) не показываем — это большинство заявок,
# и указывать источник имеет смысл только там, где он несёт сигнал: клиент
# пришёл "разогретым" с конкретного кейса/расчёта, а не просто открыл бриф.
SOURCE_LABELS = {
    "calculator": "через калькулятор",
    "about": "со страницы «Обо мне»",
}


def format_lead_message(payload: dict, calc: CalcResult | None, lead_id: int, from_user_id: int, username: str | None) -> str:
    lines = [f"🆕 <b>Новая заявка #{lead_id}</b>", ""]

    service_name = payload.get("service_name") or "не указана"
    lines.append(f"<b>Услуга:</b> {_esc(service_name)}")

    source = payload.get("source")
    source_case_title = payload.get("source_case_title")
    if source == "case" and source_case_title:
        lines.append(f"<b>Источник:</b> кейс «{_esc(source_case_title)}» — похожий проект")
    elif source in SOURCE_LABELS:
        lines.append(f"<b>Источник:</b> {SOURCE_LABELS[source]}")

    task = (payload.get("task_description") or "").strip()
    if task:
        lines.append(f"<b>Задача:</b> {_esc(task)}")

    have = payload.get("have") or []
    have_text = ", ".join(HAVE_LABELS.get(h, h) for h in have) or "не указано"
    lines.append(f"<b>Что уже есть:</b> {have_text}")

    deadline = DEADLINE_LABELS.get(payload.get("deadline"), "не указано")
    lines.append(f"<b>Когда нужно:</b> {deadline}")

    budget = BUDGET_LABELS.get(payload.get("budget"), "не указано")
    lines.append(f"<b>Бюджет:</b> {budget}")

    contact = (payload.get("contact") or "").strip()
    if contact:
        lines.append(f"<b>Контакт:</b> {_esc(contact)}")

    if payload.get("attach_tz"):
        lines.append("<b>ТЗ:</b> клиент пришлёт файл следующим сообщением")

    tz_details = payload.get("tz_details")
    if tz_details:
        lines.append("")
        lines.append("<b>Техническое задание (от клиента):</b>")
        if tz_details.get("goal"):
            lines.append(f"— Цель: {_esc(tz_details['goal'])}")
        if tz_details.get("must_have"):
            lines.append(f"— Обязательно: {_esc(tz_details['must_have'])}")
        if tz_details.get("avoid"):
            lines.append(f"— Избегать: {_esc(tz_details['avoid'])}")
        if tz_details.get("references"):
            lines.append(f"— Референсы: {_esc(tz_details['references'])}")

    if calc and calc.valid:
        lines.append("")
        lines.append("<b>Расчёт из калькулятора:</b>")
        lines.append(f"— {calc.service_name}: {calc.price_from:,} – {calc.price_to:,} ₽".replace(",", " "))
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

    return "\n".join(lines)


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
            lines.append(f"<b>{label}:</b> {_esc(value)}")
    return "\n".join(lines)


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
        lines.append(f"— Что уже есть: {', '.join(HAVE_LABELS.get(h, h) for h in have)}")
    if payload.get("deadline"):
        lines.append(f"— Когда нужно: {DEADLINE_LABELS.get(payload['deadline'], payload['deadline'])}")
    if payload.get("budget"):
        lines.append(f"— Бюджет: {BUDGET_LABELS.get(payload['budget'], payload['budget'])}")

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
        kind_labels = {"document": "файл", "photo": "фото"}
        source_labels = {"new": "при создании", "supplement": "из дополнения"}
        for m in materials:
            ts = (m.get("received_at") or "")[:16].replace("T", " ")
            kind = kind_labels.get(m.get("kind"), m.get("kind"))
            source = source_labels.get(m.get("source"), m.get("source"))
            lines.append(f"— {kind}, {ts} ({source})")

    lines.append("")
    lines.append(f"<i>Создана: {lead['created_at'][:16].replace('T', ' ')}</i>")
    if lead.get("updated_at"):
        lines.append(f"<i>Обновлена: {lead['updated_at'][:16].replace('T', ' ')}</i>")

    return "\n".join(lines)
