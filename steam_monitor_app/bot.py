import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, Message
import aiohttp

from .clients import SteamApiClient, SteamIdUkClient
from .config import load_config
from .core import logger
from .monitor import SteamProfileMonitor


async def main() -> None:
    config = load_config()

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        telegram_session = AiohttpSession(proxy=config.telegram_proxy, timeout=30)
        bot = Bot(token=config.bot_token, session=telegram_session)
        try:
            bot_info = await bot.get_me()
        except TelegramUnauthorizedError:
            logger.error("Telegram bot token is invalid. Check bot_token in config.ini.")
            print("Telegram bot token is invalid. Check bot_token in config.ini.")
            await bot.session.close()
            return

        logger.info("Telegram bot authorized as @%s", bot_info.username or bot_info.id)
        steam = SteamApiClient(config.steam_api_key, session)
        steamid_uk = (
            SteamIdUkClient(config.steamid_uk_api_key, config.steamid_uk_myid, session)
            if config.steamid_uk_enabled
            else None
        )
        monitor = SteamProfileMonitor(config, bot, steam, steamid_uk)
        dispatcher = Dispatcher()

        @dispatcher.message(Command("start"))
        async def start_command(message: Message) -> None:
            await monitor.send_private(message, monitor.start_help_text())

        def register_command(action: str) -> None:
            @dispatcher.message(Command(action))
            async def command_handler(message: Message) -> None:
                await monitor.handle_menu_command(message, action)

        for action, _, _ in monitor.available_actions():
            register_command(action)

        @dispatcher.callback_query(F.data.startswith("menu:"))
        async def menu_callback(callback: CallbackQuery) -> None:
            await monitor.handle_menu_callback(callback)

        try:
            await bot.set_my_commands(
                [BotCommand(command="start", description="Меню")]
                + [BotCommand(command=action, description=desc) for action, _, desc in monitor.available_actions()]
            )
        except TelegramNetworkError as exc:
            logger.warning("Не удалось обновить меню команд Telegram: %s", exc)

        monitor_task = asyncio.create_task(monitor.run_forever())
        try:
            retry_delay = 5
            while True:
                try:
                    await dispatcher.start_polling(bot, close_bot_session=False)
                    break
                except TelegramUnauthorizedError:
                    logger.error("Telegram bot token is invalid. Check bot_token in config.ini.")
                    print("Telegram bot token is invalid. Check bot_token in config.ini.")
                    break
                except TelegramNetworkError as exc:
                    logger.warning("Нет соединения с Telegram API: %s", exc)
                    logger.info("Повторное подключение к Telegram через %s сек.", retry_delay)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 300)
        finally:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
