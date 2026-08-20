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

from bot import config, content_store
from bot.handlers import admin, faq, start, webapp
from bot.webserver import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

CLIENT_COMMANDS = [
    BotCommand(command="start", description="Начать / приветствие"),
    BotCommand(command="faq", description="Частые вопросы"),
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
    # Системное Menu Telegram (иконка рядом с полем ввода) — прямой запуск
    # Mini App (MenuButtonWebApp), корень WEBAPP_URL (без /portfolio и т.п.
    # — портфолио/about/заказ/"Мои заявки" теперь только навигация ВНУТРИ
    # уже открытого Mini App, см. webapp/js/app.js::TAB_SCREENS).
    #
    # РАНЕЕ здесь уже стоял MenuButtonWebApp и был откачен (commit ffe52ae,
    # после регресса из commit ac09080) — по ДРУГОЙ причине: тогда список
    # команд (/portfolio, /about, /brief) ещё оставался единственным
    # быстрым доступом к этим разделам, и MenuButtonWebApp его вытеснял —
    # клиенты теряли доступ к разделам, а не только к кнопке. Сейчас это
    # осознанно другое решение: CLIENT_COMMANDS сокращён до /start, /faq —
    # portfolio/about/brief остаются доступны (и НЕ удалены как handlers,
    # см. bot/handlers/start.py::cmd_portfolio/cmd_about/cmd_brief), просто
    # их основной путь теперь через сам Mini App, а не отдельные команды.
    # MenuButtonCommands() и MenuButtonWebApp() взаимоисключающие (Telegram
    # хранит только одно значение) — вызов явный при каждом старте, само
    # предыдущее значение не откатится без него.
    #
    # inline "🚀 Открыть приложение" (bot/handlers/start.py::open_app_button)
    # остаётся как fallback/contextual launch — не убран.
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

    # До webserver.start()/polling — чтобы ни один HTTP-запрос Mini App и ни
    # один апдейт от Telegram не мог попасть на ещё не проинициализированное
    # хранилище (см. production-hardening аудит, P0-1). При выключенном
    # Upstash — no-op (см. ensure_storage_initialized).
    content_store.ensure_storage_initialized()

    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.PORT)
    await site.start()
    logger.info("Mini App сервер запущен: http://0.0.0.0:%s (снаружи — %s)", config.PORT, config.WEBAPP_URL)

    # drop_pending_updates=False — на free-plan Render rolling-деплой новый
    # инстанс на короткое время перекрывается со старым (TelegramConflictError
    # в этом окне — ожидаемое, безопасное поведение, Telegram сам разруливает
    # конфликт polling-запросов). Но drop_pending_updates=True безусловно
    # отбрасывал бы любые апдейты, накопившиеся именно в этом окне — реальные
    # сообщения/файлы/callback-и клиентов, а не мусор (см. production-hardening
    # аудит). Оставляем их дожидаться нового инстанса вместо потери: повторная
    # обработка уже сущ. апдейта — не проблема (draft_id upsert/append-only/
    # status-idempotency уже переживают повтор без дублей).
    await bot.delete_webhook(drop_pending_updates=False)
    await _setup_bot_commands(bot)
    await _setup_menu_button(bot)
    logger.info("Бот запущен в режиме polling")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
