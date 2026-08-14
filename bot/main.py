import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonWebApp,
    WebAppInfo,
)
from aiohttp import web

from bot import config
from bot.handlers import admin, faq, start, webapp
from bot.webserver import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

CLIENT_COMMANDS = [
    BotCommand(command="start", description="Начать / приветствие"),
    BotCommand(command="faq", description="Частые вопросы"),
    BotCommand(command="portfolio", description="Портфолио"),
    BotCommand(command="about", description="Обо мне"),
    BotCommand(command="brief", description="Оставить заявку"),
]

ADMIN_EXTRA_COMMANDS = [
    BotCommand(command="admin", description="Админ-меню: кейсы, FAQ, «Обо мне»"),
]


async def _setup_bot_commands(bot: Bot) -> None:
    # scope="default" — все клиенты. scope=chat(DESIGNER_CHAT_ID) — только
    # у дизайнера, Telegram отдаёт приоритет более специфичному scope для
    # конкретного чата, так что админ-команды больше никому не видны.
    await bot.set_my_commands(CLIENT_COMMANDS, scope=BotCommandScopeDefault())
    if config.DESIGNER_CHAT_ID:
        try:
            await bot.set_my_commands(
                CLIENT_COMMANDS + ADMIN_EXTRA_COMMANDS,
                scope=BotCommandScopeChat(chat_id=int(config.DESIGNER_CHAT_ID)),
            )
        except (ValueError, TypeError):
            logger.exception("DESIGNER_CHAT_ID=%r не похож на числовой chat_id — расширенное меню не задано", config.DESIGNER_CHAT_ID)


async def _setup_menu_button(bot: Bot) -> None:
    # Menu Button (кнопка рядом с полем ввода сообщения) — основная точка
    # входа в Mini App. В отличие от reply-клавиатуры (KeyboardButton.web_app,
    # больше не используется, см. bot/keyboards.py), Menu Button
    # гарантированно передаёт Telegram.WebApp.initData — по документации
    # Telegram работает так же, как inline-кнопка (это уже подтверждено
    # production-тестами для /portfolio, /about, /brief). chat_id не задан —
    # это дефолтная кнопка для всех чатов с ботом, включая DESIGNER_CHAT_ID
    # (админу это не мешает: /admin и остальные команды продолжают работать
    # независимо от Menu Button). WEBAPP_URL тот же, новый Mini App не нужен.
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Открыть приложение", web_app=WebAppInfo(url=config.WEBAPP_URL))
    )


async def main() -> None:
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())

    # admin — первым: /admin не должен перехватываться catch-all из start.
    # webapp — тоже раньше faq/start, т.к. должен первым перехватывать
    # web_app_data и ответы в состоянии "ждём файл ТЗ".
    dp.include_router(admin.router)
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
    await _setup_bot_commands(bot)
    await _setup_menu_button(bot)
    logger.info("Бот запущен в режиме polling")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
