"""Публикация обложек в Telegram-канал-архив (Media Archive).

Зачем: у бота нет способа получить browser-loadable URL картинки из
file_id — единственный download-URL Bot API содержит токен
(api.telegram.org/file/bot<TOKEN>/...) и потому непригоден для публичного
portfolio.json. Но у ПУБЛИЧНОГО поста есть веб-превью t.me, а у него —
tokenless CDN-ссылка cdn*.telesco.pe. Отсюда весь путь:

    фото от дизайнера (file_id)
    -> sendPhoto в публичный архив-канал
    -> message_id
    -> https://t.me/<канал>/<message_id>
    -> bot/telegram_media.py::resolve_cover_url
    -> tokenless cdn*.telesco.pe URL -> cover

Границы модуля (намеренно узкие):
- НЕ создаёт второй Telegram-клиент: работает тем же aiogram Bot, который
  уже крутится в bot/main.py, и тем же BOT_TOKEN. Никаких новых
  credentials;
- НЕ извлекает обложку сам — это дело telegram_media.py. Здесь только
  публикация и удаление;
- НЕ сканирует канал и НЕ пытается определить "чьё" сообщение: удаляется
  ровно тот message_id, который явно записан в source_ref конкретного
  кейса (см. parse_source_ref);
- НЕ знает про portfolio.json — вызывающий код решает, что и когда
  сохранять.

Про удаление — важное ограничение, проверенное вживую: после
deleteMessage пост в канале исчезает, но ранее выданный
cdn*.telesco.pe URL продолжает отдавать ту же картинку (HTTP 200, тот же
Content-Length). То есть удаление архивного сообщения — это уборка в
канале, а НЕ отзыв опубликованного медиа. Ничего другого обещать нельзя
ни в UI, ни в логах.
"""

import logging
import re
from typing import Any

from bot import config

logger = logging.getLogger(__name__)

_SOURCE_REF_PREFIX = "archive"

# archive:<chat>:<message_id>. chat может быть и "@username", и числовым
# id канала, поэтому разделителем последнего поля служит ПОСЛЕДНЕЕ
# двоеточие (числовые id каналов отрицательные, но двоеточий не содержат).
_SOURCE_REF_PATTERN = re.compile(rf"^{_SOURCE_REF_PREFIX}:(.+):(\d+)$")


class MediaArchiveError(Exception):
    """Не удалось опубликовать фото в архив (бот не админ, Telegram вернул
    ошибку). Вызывающий код обязан показать дизайнеру контролируемую
    ошибку и НЕ записывать cover — иначе кейс сослался бы на
    несуществующую картинку."""


class MediaArchiveNotConfigured(MediaArchiveError):
    """MEDIA_ARCHIVE_CHANNEL не задан.

    Отдельный тип, а не просто текст: это НЕ сбой выполнения, а
    незавершённая настройка окружения, и дизайнеру про неё нужно сказать
    иначе, чем про "попробуйте ещё раз". Молчаливого отката на прежний
    storage здесь сознательно нет: он создавал бы две разные production-
    архитектуры одновременно, и часть обложек незаметно уезжала бы в
    старое хранилище вместо архива."""


def is_configured() -> bool:
    return bool(config.MEDIA_ARCHIVE_CHANNEL)


def channel() -> str:
    return config.MEDIA_ARCHIVE_CHANNEL


def _channel_path() -> str:
    """Имя канала для публичного t.me-URL — без ведущего "@"."""
    return config.MEDIA_ARCHIVE_CHANNEL.lstrip("@")


def archive_post_url(message_id: int) -> str:
    return f"https://t.me/{_channel_path()}/{message_id}"


def build_source_ref(message_id: int) -> str:
    """Внутренняя ссылка на архивное сообщение — то, что кладётся в
    case["source_ref"]. Это НЕ публичный URL и не должно рендериться как
    кликабельная ссылка: "Смотреть подробнее" в Mini App живёт на
    external_url, который у upload-кейсов пуст (см. handlers/admin.py)."""
    return f"{_SOURCE_REF_PREFIX}:{config.MEDIA_ARCHIVE_CHANNEL}:{message_id}"


def parse_source_ref(source_ref: str | None) -> tuple[str, int] | None:
    """(chat, message_id) или None, если это не archive-ссылка.

    Строгий разбор — единственный источник message_id для удаления: без
    него пришлось бы угадывать, какое сообщение в канале принадлежит
    кейсу, а этого модуль делать не должен принципиально."""
    if not isinstance(source_ref, str):
        return None
    match = _SOURCE_REF_PATTERN.match(source_ref.strip())
    if not match:
        return None
    return match.group(1), int(match.group(2))


async def send_archive_photo(bot: Any, file_id: str) -> tuple[int, str]:
    """Публикует уже полученное ботом фото (по file_id) в архив-канал.

    Возвращает (message_id, public_post_url). Бросает MediaArchiveError на
    любой неудаче — тихо вернуть "как будто получилось" здесь нельзя.

    file_id, а не байты: файл уже лежит у Telegram, повторно скачивать его
    на Render и заливать обратно незачем — ни одного байта картинки через
    наш сервер не проходит, ровно как в Behance/Telegram-post путях."""
    if not is_configured():
        raise MediaArchiveNotConfigured("MEDIA_ARCHIVE_CHANNEL не задан")
    try:
        message = await bot.send_photo(chat_id=config.MEDIA_ARCHIVE_CHANNEL, photo=file_id)
    except Exception as e:
        # Текст ошибки Telegram (нет прав, канал не найден) полезен в
        # логах, но токена не содержит — сам токен сюда не попадает,
        # он живёт только внутри Bot-сессии.
        raise MediaArchiveError(f"sendPhoto в архив не удался: {type(e).__name__}: {e}") from e
    return message.message_id, archive_post_url(message.message_id)


async def delete_archive_message(bot: Any, source_ref: str | None) -> bool:
    """Best-effort уборка архивного сообщения по source_ref кейса.

    Никогда не бросает: удаление архива — уборка, а не часть основной
    операции дизайнера (тот же принцип, что у r2_storage.delete_image).
    Неудача логируется как ERROR, чтобы осиротевшее сообщение не прошло
    незамеченным, но не отменяет уже выполненное действие.

    Возвращает True, только если сообщение действительно удалено."""
    parsed = parse_source_ref(source_ref)
    if parsed is None:
        return False
    chat, message_id = parsed
    try:
        await bot.delete_message(chat_id=chat, message_id=message_id)
    except Exception as e:
        logger.error(
            "Не удалось удалить архивное сообщение %s из %s: %s "
            "(сообщение может остаться в канале)",
            message_id, chat, type(e).__name__,
        )
        return False
    return True
