import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonWebApp,
    WebAppInfo,
)
from aiohttp import web

from bot import config, content_store, texts
from bot.handlers import admin, faq, start, webapp
from bot.webserver import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# /portfolio, /about, /brief намеренно НЕ входят в видимый список — они
# полностью покрыты навигацией внутри Mini App (webapp/js/app.js::
# TAB_SCREENS), куда теперь ведёт прямой запуск через Menu Button (см.
# _setup_menu_button ниже). Handlers команд остаются рабочими как legacy/
# deep-link — см. bot/handlers/start.py::cmd_portfolio/cmd_about/cmd_brief,
# просто больше не рекламируются в командном меню.
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
    # уже открытого Mini App, см. webapp/js/app.js::TAB_SCREENS). Один тап
    # сразу открывает Mini App, без промежуточного сообщения-кнопки.
    #
    # MenuButtonWebApp и MenuButtonCommands взаимоисключающие — Telegram
    # хранит только одно значение за бота, поэтому список команд
    # (CLIENT_COMMANDS выше) сокращён до /start, /faq: то, что раньше было
    # только в командах (/portfolio, /about, /brief), теперь полностью
    # доступно через сам Mini App — ничего не становится недостижимым.
    # Вызов явный при каждом старте: предыдущее значение само не откатится.
    #
    # Постоянная reply-клавиатура (bot/keyboards.py::main_reply_keyboard)
    # больше не содержит отдельной кнопки-триггера запуска — она дублировала
    # бы эту же кнопку. Inline "🚀 Открыть приложение" в уже отправленных
    # сообщениях и legacy-команды /portfolio, /about, /brief остаются
    # рабочими как fallback/contextual launch (см. bot/handlers/start.py).
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Открыть приложение", web_app=WebAppInfo(url=config.WEBAPP_URL))
    )


async def _setup_bot_description(bot: Bot) -> None:
    # setMyDescription/setMyShortDescription (Bot API) — то, что пользователь
    # видит ДО первого /start (профиль бота, экран с системной кнопкой
    # "Запустить" — её саму мы не задаём, это Telegram). Держим в коде
    # (bot/texts.py::BOT_DESCRIPTION/BOT_SHORT_DESCRIPTION), а не только в
    # BotFather, по тому же принципу, что и CLIENT_COMMANDS/menu_button
    # выше — версионируется вместе с кодом, не полагается на то, что кто-то
    # не забудет продублировать это вручную в BotFather после деплоя.
    await bot.set_my_short_description(short_description=texts.BOT_SHORT_DESCRIPTION)
    await bot.set_my_description(description=texts.BOT_DESCRIPTION)


async def main() -> None:
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    # events_isolation=SimpleEventIsolation() — штатный aiogram-механизм,
    # сериализует обработку апдейтов per StorageKey (per чат). Без него
    # default — DisabledEventIsolation (no-op lock) + polling по умолчанию
    # обрабатывает каждый update как независимый asyncio.Task
    # (Dispatcher._polling(handle_as_tasks=True)) — два быстрых подряд
    # нажатия одной кнопки в одном чате могли выполняться параллельно и
    # обе гонки проходили check "nav anchor отсутствует" до того, как
    # первая успевала записать результат (см. UX-аудит: race condition,
    # приводивший к дублированию NAV anchor при повторных "Главное меню").
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())

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
    await content_store.ensure_storage_initialized()

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
    await _setup_bot_description(bot)
    logger.info("Бот запущен в режиме polling")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
