from pathlib import Path
from typing import Dict, List
import configparser
import os

from .core import CONFIG_PATH, MIN_POLL_INTERVAL_SECONDS, MonitorConfig, MonitoredAccount


def _get_bool(config: configparser.ConfigParser, section: str, option: str, fallback: bool) -> bool:
    if not config.has_option(section, option):
        return fallback
    return config.getboolean(section, option)


def _get_int(config: configparser.ConfigParser, section: str, option: str, fallback: int) -> int:
    if not config.has_option(section, option):
        return fallback
    return config.getint(section, option)


def _get_optional_config_value(
    config: configparser.ConfigParser,
    section: str,
    option: str,
    fallback: str = "",
) -> str:
    if not config.has_option(section, option):
        return fallback
    value = config.get(section, option).strip()
    if not value or value.startswith("YOUR_"):
        return fallback
    return value


def load_config(path: Path = CONFIG_PATH) -> MonitorConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден конфиг {path}. Скопируйте steam_monitor/config.ini.example в steam_monitor/config.ini"
        )

    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")

    accounts = load_accounts(config)
    if not accounts:
        raise ValueError("В config.ini не указаны аккаунты для мониторинга.")

    allowed_user_id = None
    if config.has_option("telegram", "allowed_user_id"):
        raw_allowed_user_id = config.get("telegram", "allowed_user_id").strip()
        if raw_allowed_user_id and not raw_allowed_user_id.startswith("YOUR_"):
            allowed_user_id = int(raw_allowed_user_id)

    telegram_proxy = None
    if config.has_option("telegram", "proxy"):
        raw_proxy = config.get("telegram", "proxy").strip()
        if raw_proxy and not raw_proxy.startswith("YOUR_"):
            telegram_proxy = raw_proxy

    steamid_uk_api_key = _get_optional_config_value(
        config,
        "steamid_uk",
        "api_key",
        os.environ.get("STEAMID_UK_API_KEY", ""),
    )
    steamid_uk_myid = _get_optional_config_value(
        config,
        "steamid_uk",
        "myid",
        os.environ.get("STEAMID_UK_MYID", ""),
    )
    steamid_uk_enabled = (
        _get_bool(config, "steamid_uk", "enabled", bool(steamid_uk_api_key and steamid_uk_myid))
        and bool(steamid_uk_api_key and steamid_uk_myid)
    )

    return MonitorConfig(
        bot_token=config.get("telegram", "bot_token").strip(),
        chat_id=int(config.get("telegram", "chat_id").strip()),
        allowed_user_id=allowed_user_id,
        telegram_proxy=telegram_proxy,
        steam_api_key=config.get("steam", "api_key").strip(),
        steamid_uk_enabled=steamid_uk_enabled,
        steamid_uk_api_key=steamid_uk_api_key,
        steamid_uk_myid=steamid_uk_myid,
        steamid_uk_refresh_interval_seconds=max(
            300,
            _get_int(config, "steamid_uk", "refresh_interval_seconds", 21600),
        ),
        steamid_uk_sync_watchlist=_get_bool(config, "steamid_uk", "sync_watchlist", False),
        steamid_uk_watchlist_id=_get_optional_config_value(config, "steamid_uk", "watchlist_id", "1"),
        poll_interval_seconds=max(MIN_POLL_INTERVAL_SECONDS, config.getint("steam", "poll_interval_seconds", fallback=60)),
        status_reminder_interval_seconds=max(0, config.getint("steam", "status_reminder_interval_seconds", fallback=3600)),
        persona_state_debounce_seconds=max(0, config.getint("steam", "persona_state_debounce_seconds", fallback=120)),
        notify_on_start=_get_bool(config, "steam", "notify_on_start", False),
        monitor_comments=_get_bool(config, "steam", "monitor_comments", True),
        monitor_friends=_get_bool(config, "steam", "monitor_friends", True),
        monitor_badges=_get_bool(config, "steam", "monitor_badges", True),
        monitor_rich_presence=_get_bool(config, "steam", "monitor_rich_presence", True),
        monitor_cs2=_get_bool(config, "steam", "monitor_cs2", True),
        accounts=accounts,
    )


def load_accounts(config: configparser.ConfigParser) -> List[MonitoredAccount]:
    accounts: List[MonitoredAccount] = []

    if config.has_section("accounts"):
        for steam_id, label in config.items("accounts"):
            steam_id = steam_id.strip()
            label = label.strip() or steam_id
            if steam_id and not steam_id.startswith("YOUR_"):
                accounts.append(MonitoredAccount(steam_id=steam_id, label=label))

    for section in config.sections():
        if not section.lower().startswith("account:"):
            continue
        steam_id = config.get(section, "steam_id", fallback="").strip()
        label = config.get(section, "label", fallback=section.split(":", 1)[1]).strip()
        if steam_id and not steam_id.startswith("YOUR_"):
            accounts.append(MonitoredAccount(steam_id=steam_id, label=label or steam_id))

    unique: Dict[str, MonitoredAccount] = {}
    for account in accounts:
        unique[account.steam_id] = account
    return list(unique.values())
