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


def format_lead_message(payload: dict, calc: CalcResult | None, from_user_id: int, username: str | None) -> str:
    lines = ["🆕 <b>Новая заявка</b>", ""]

    service_name = payload.get("service_name") or "не указана"
    lines.append(f"<b>Услуга:</b> {_esc(service_name)}")

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
