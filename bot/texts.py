WELCOME = (
    "Привет! 👋 Я дизайнер, и это мой бот.\n\n"
    "Здесь можно посмотреть портфолио, узнать обо мне и оставить заявку — "
    "без лишних звонков и переписки.\n\n"
    "Кнопки ниже 👇 всегда под рукой."
)

OPEN_APP_BUTTON = "🚀 Открыть приложение"
ADMIN_BUTTON = "⚙️ Админ"
MENU_FAQ = "❓ Частые вопросы"
MENU_BRIEF = "✍️ Оставить заявку"
MAIN_MENU_BUTTON = "⌂ Главное меню"

MAIN_MENU_CONFIRM_TEXT = "Вернуться в главное меню?\nТекущее действие будет прервано."
MAIN_MENU_CONFIRM_YES = "✅ Да, продолжить"
MAIN_MENU_CONFIRM_NO = "↩️ Остаться"

# Bot API setMyShortDescription/setMyDescription (см. bot/main.py::
# _setup_bot_description) — видны пользователю ДО первого /start, в
# профиле бота и на экране "Запустить". Лимиты Telegram: short ≤120,
# description ≤512 символов (см. regression-тест на длину).
BOT_SHORT_DESCRIPTION = "Дизайн-помощник: портфолио, расчёт стоимости и заявка на проект."
BOT_DESCRIPTION = (
    "Design Assistant — бот-помощник по дизайну. Посмотрите портфолио, "
    "получите предварительную оценку стоимости проекта и отправьте заявку "
    "на дизайн. Нажмите «Запустить», чтобы начать."
)

FAQ_INTRO = "Выберите вопрос:"
FAQ_BACK = "◀️ Ко всем вопросам"
FAQ_BACK_TO_SERVICES = "◀️ К выбору услуги"
FAQ_PICK_SERVICE = "По какой услуге подсказать цену?"
FAQ_DISABLED = "Раздел FAQ сейчас недоступен."

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

TZ_FILE_UNSUPPORTED_TYPE = "Такой тип файла не поддерживается 🙁 Пришлите, пожалуйста, документом, фото, видео или GIF."

CASE_INTEREST_ACK = "Хорошо! Открываю заявку с указанием этой услуги 👇"

WEBAPP_OPEN_ERROR = (
    "Не получилось открыть мини-приложение. Проверьте, что WEBAPP_URL в .env "
    "указывает на рабочий HTTPS-адрес."
)
