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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Проверьте файл .env (см. .env.example).")

if not WEBAPP_URL:
    raise RuntimeError(
        "WEBAPP_URL не задан. Укажите публичный HTTPS-адрес Mini App в .env "
        "(см. .env.example и README.md)."
    )
