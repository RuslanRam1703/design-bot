import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from bot import config
from bot.handlers import faq, start, webapp
from bot.webserver import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())

    # webapp — раньше faq/start, т.к. должен первым перехватывать web_app_data
    # и ответы в состоянии "ждём файл ТЗ".
    dp.include_router(webapp.router)
    dp.include_router(faq.router)
    dp.include_router(start.router)  # start — последним: содержит catch-all для текста

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.PORT)
    await site.start()
    logger.info("Mini App сервер запущен: http://0.0.0.0:%s (снаружи — %s)", config.PORT, config.WEBAPP_URL)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен в режиме polling")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
