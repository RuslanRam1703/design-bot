import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DESIGNER_CHAT_ID = os.getenv("DESIGNER_CHAT_ID", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Проверьте файл .env (см. .env.example).")

if not WEBAPP_URL:
    raise RuntimeError(
        "WEBAPP_URL не задан. Укажите публичный HTTPS-адрес Mini App в .env "
        "(см. .env.example и README.md)."
    )
