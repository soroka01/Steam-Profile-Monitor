import asyncio
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command
from aiogram.types import Message
import aiohttp

from .clients import SteamApiClient, SteamIdUkClient
from .config import load_config
from .core import STEAM_COMMUNITY_BASE, logger
from .helpers import html_attr, html_text
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

        @dispatcher.message(Command("status"))
        async def status_command(message: Message) -> None:
            await monitor.send_private(message, monitor.format_status_report())

        @dispatcher.message(Command("accounts"))
        async def accounts_command(message: Message) -> None:
            lines = ["👥 <b>Отслеживаемые аккаунты</b>"]
            for account in config.accounts:
                profile_url = f"{STEAM_COMMUNITY_BASE}/profiles/{account.steam_id}"
                lines.append(
                    f"\n👤 <b>{html_text(account.label)}</b>\n"
                    f"🆔 <code>{html_text(account.steam_id)}</code>\n"
                    f"🔗 <a href=\"{html_attr(profile_url)}\">Steam profile</a>"
                )
            await monitor.send_private(message, "\n".join(lines))

        @dispatcher.message(Command("cs2today"))
        async def cs2today_command(message: Message) -> None:
            await monitor.send_private(message, monitor.format_cs2_daily_report())

        @dispatcher.message(Command("steamiduk"))
        async def steamiduk_command(message: Message) -> None:
            await monitor.refresh_all_steamid_uk_profiles(force=True)
            await monitor.send_private(message, monitor.format_steamid_uk_report())

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
