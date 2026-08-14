WELCOME = (
    "Привет! 👋 Я дизайнер, и это мой бот.\n\n"
    "Здесь можно посмотреть портфолио, узнать обо мне и оставить заявку — "
    "без лишних звонков и переписки. Частые вопросы — командой /faq.\n\n"
    "Откройте приложение кнопкой ниже 👇 — оно же всегда доступно через "
    "кнопку рядом с полем ввода сообщения."
)

MENU_FAQ = "❓ Частые вопросы"
MENU_BRIEF = "✍️ Оставить заявку"

FAQ_INTRO = "Выберите вопрос:"
FAQ_BACK = "◀️ Ко всем вопросам"
FAQ_BACK_TO_SERVICES = "◀️ К выбору услуги"
FAQ_PICK_SERVICE = "По какой услуге подсказать цену?"

MY_ID_TEMPLATE = (
    "Ваш chat_id: `{chat_id}`\n\n"
    "Если вы дизайнер — вставьте это значение в переменную окружения "
    "DESIGNER_CHAT_ID, чтобы получать сюда заявки клиентов."
)

BRIEF_WEBAPP_HINT = (
    "Заявка собирается в мини-приложении — нажмите кнопку ниже, чтобы открыть форму 👇"
)

def _lead_ack_header(lead_id: int, service_name: str | None, price_range: str | None) -> list[str]:
    """Общая шапка подтверждения — номер заявки, услуга, предварительная
    цена (см. Part 7 ТЗ: клиент должен видеть номер заявки, услугу и
    предварительную стоимость сразу после отправки, а не только "спасибо")."""
    lines = [f"Спасибо! Заявка №{lead_id} отправлена 🙌"]
    if service_name:
        lines.append(f"Услуга: {service_name}")
    if price_range:
        lines.append(f"Предварительная стоимость: {price_range}")
    return lines


def lead_received_ack(lead_id: int, service_name: str | None, price_range: str | None) -> str:
    lines = _lead_ack_header(lead_id, service_name, price_range)
    lines.append("")
    lines.append("Я свяжусь с вами в ближайшее время.")
    return "\n".join(lines)


def lead_ack_ask_file(lead_id: int, service_name: str | None, price_range: str | None) -> str:
    lines = _lead_ack_header(lead_id, service_name, price_range)
    lines.append("")
    lines.append("Вы отметили, что есть подробное ТЗ — пришлите файл следующим сообщением, я его посмотрю.")
    return "\n".join(lines)

TZ_FILE_FORWARDED = "Файл получен, спасибо! Я его изучу."
TZ_FILE_EXPECTED = "Жду файл с ТЗ 📎 (или отправьте /cancel, если передумали)"

CASE_INTEREST_ACK = "Хорошо! Открываю заявку с указанием этой услуги 👇"

WEBAPP_OPEN_ERROR = (
    "Не получилось открыть мини-приложение. Проверьте, что WEBAPP_URL в .env "
    "указывает на рабочий HTTPS-адрес."
)
