import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DESIGNER_CHAT_ID = os.getenv("DESIGNER_CHAT_ID", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

# Опционально: Upstash Redis (REST API) как персистентное хранилище для
# data/*.json — на Render (и большинстве бесплатных PaaS) файловая система
# эфемерна и сбрасывается на каждый redeploy/restart, теряя и заявки, и
# любые правки через /admin. Если не заданы — content_store читает/пишет
# локальные файлы как раньше (для локальной разработки не нужен вообще).
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# Опционально: Cloudflare R2 (S3-совместимое) как персистентное хранилище для
# байтов изображений портфолио/about (Batch 3) — Upstash выше хранит только
# JSON-метаданные, ссылки на файлы; сами картинки на бесплатном Render иначе
# живут только на эфемерном диске (см. bot/content_store.py::save_case_photo).
# Если не заданы — поведение как раньше, локальные файлы в webapp/img/
# (для локальной разработки R2-аккаунт не нужен). R2_PUBLIC_BASE_URL —
# публичный адрес, с которого отдаются загруженные объекты (r2.dev или
# кастомный домен) — намеренно отделён от самого R2_ACCOUNT_ID/API-эндпоинта,
# чтобы смену r2.dev на кастомный домен не требовала правок кода.
# Публичный канал-архив для обложек, загруженных дизайнером напрямую
# (см. bot/media_archive.py). Обложка публикуется туда одним сообщением,
# после чего её tokenless CDN-адрес берётся из веб-превью t.me — это
# единственный способ получить browser-loadable URL из file_id, не
# раскрывая BOT_TOKEN. Значение вида "@media_archive_da".
#
# Никаких отдельных credentials не требует — используется тот же BOT_TOKEN
# и тот же Bot-инстанс. Значение НЕ отдаётся фронтенду: /data/* отдаёт
# только PUBLIC_DATA_FILES (см. bot/webserver.py), config туда не попадает.
# Если не задан — прямая загрузка обложки продолжает работать через
# прежний storage-путь (см. bot/handlers/admin.py), ничего не ломается.
MEDIA_ARCHIVE_CHANNEL = os.getenv("MEDIA_ARCHIVE_CHANNEL", "").strip()

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Проверьте файл .env (см. .env.example).")

if not WEBAPP_URL:
    raise RuntimeError(
        "WEBAPP_URL не задан. Укажите публичный HTTPS-адрес Mini App в .env "
        "(см. .env.example и README.md)."
    )
