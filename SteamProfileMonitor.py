import asyncio
import configparser
import html
import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import Message


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
LOG_PATH = BASE_DIR / "steam_profile_monitor.log"
CHANGES_LOG_PATH = BASE_DIR / "steam_profile_changes.log"
ACCOUNT_LOG_DIR = BASE_DIR / "account_logs"
STATE_PATH = BASE_DIR / "steam_monitor_state.json"

STEAM_API_BASE = "https://api.steampowered.com"
STEAM_COMMUNITY_BASE = "https://steamcommunity.com"
STEAMID64_ACCOUNT_ID_BASE = 76561197960265728
CS2_APP_ID = "730"
STEAM_RETRY_DELAYS = (2, 5, 10)
TELEGRAM_SEND_RETRY_DELAYS = (2, 5, 10)


def local_timezone() -> timezone:
    offset = datetime.now().astimezone().utcoffset() or timedelta()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    label = f"UTC{sign}{hours}" if minutes == 0 else f"UTC{sign}{hours}:{minutes:02d}"
    return timezone(offset, label)


LOCAL_TZ = local_timezone()

PERSONA_STATES = {
    0: "не в сети",
    1: "в сети",
    2: "занят",
    3: "отошел",
    4: "спит",
    5: "хочет обменяться",
    6: "хочет играть",
}


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
        value = datetime.fromtimestamp(record.created, LOCAL_TZ)
        return value.strftime(datefmt or f"%Y-%m-%d %H:%M:%S {LOCAL_TZ.tzname(None)}")


log_formatter = LocalTimeFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)
file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8", delay=True)
file_handler.setFormatter(log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[stream_handler, file_handler])
logger = logging.getLogger("steam-profile-monitor")

change_logger = logging.getLogger("steam-profile-monitor.changes")
change_logger.setLevel(logging.INFO)
change_logger.propagate = False
if not change_logger.handlers:
    change_file_handler = logging.FileHandler(CHANGES_LOG_PATH, encoding="utf-8", delay=True)
    change_file_handler.setFormatter(log_formatter)
    change_logger.addHandler(change_file_handler)


@dataclass(frozen=True)
class MonitoredAccount:
    steam_id: str
    label: str


@dataclass
class MonitorConfig:
    bot_token: str
    chat_id: int
    allowed_user_id: Optional[int]
    telegram_proxy: Optional[str]
    steam_api_key: str
    poll_interval_seconds: int
    status_reminder_interval_seconds: int
    notify_on_start: bool
    monitor_comments: bool
    monitor_friends: bool
    monitor_badges: bool
    monitor_rich_presence: bool
    accounts: List[MonitoredAccount]


@dataclass
class CommentInfo:
    comment_id: str
    author: str = "неизвестный автор"
    author_steam_id: str = ""
    author_profile_url: str = ""
    created_at: str = ""
    text: str = ""


@dataclass
class BadgeInfo:
    badge_key: str
    name: str
    level: Optional[int] = None
    app_id: Optional[int] = None


@dataclass(frozen=True)
class FriendInfo:
    steam_id: str
    name: str

    @property
    def profile_url(self) -> str:
        return f"{STEAM_COMMUNITY_BASE}/profiles/{self.steam_id}"


@dataclass
class AccountSnapshot:
    persona_state: int = 0
    game_id: Optional[str] = None
    game_name: Optional[str] = None
    rich_presence: str = ""
    cs2_mode: str = ""
    cs2_map: str = ""
    cs2_score: str = ""
    friends: Optional[Set[str]] = None
    badges: Optional[Set[str]] = None
    comments: Optional[Set[str]] = None
    known_comments: Dict[str, CommentInfo] = field(default_factory=dict)
    known_badges: Dict[str, BadgeInfo] = field(default_factory=dict)
    display_name: str = ""
    profile_url: str = ""

    @property
    def online(self) -> bool:
        return self.persona_state != 0


@dataclass
class AccountTimeline:
    observed_since: datetime
    online_started_at: Optional[datetime] = None
    game_started_at: Optional[datetime] = None
    idle_started_at: Optional[datetime] = None
    offline_started_at: Optional[datetime] = None
    last_cs2_mode: str = ""
    last_cs2_map: str = ""
    last_cs2_score: str = ""
    last_reminder_at: Optional[datetime] = None
    last_reminder_key: str = ""


@dataclass
class CS2MatchRecord:
    completed_at: datetime
    mode: str
    map_name: str
    score: str
    result: str
    rich_presence: str = ""


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(LOCAL_TZ)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(LOCAL_TZ)


def dt_to_json(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(LOCAL_TZ).isoformat()


def set_to_json(value: Optional[Set[str]]) -> Optional[List[str]]:
    if value is None:
        return None
    return sorted(value)


def json_to_set(value: Any) -> Optional[Set[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value}


def snapshot_to_json(snapshot: AccountSnapshot) -> Dict[str, Any]:
    return {
        "persona_state": snapshot.persona_state,
        "game_id": snapshot.game_id,
        "game_name": snapshot.game_name,
        "rich_presence": snapshot.rich_presence,
        "cs2_mode": snapshot.cs2_mode,
        "cs2_map": snapshot.cs2_map,
        "cs2_score": snapshot.cs2_score,
        "friends": set_to_json(snapshot.friends),
        "badges": set_to_json(snapshot.badges),
        "comments": set_to_json(snapshot.comments),
        "known_comments": {
            key: {
                "comment_id": value.comment_id,
                "author": value.author,
                "author_steam_id": value.author_steam_id,
                "author_profile_url": value.author_profile_url,
                "created_at": value.created_at,
                "text": value.text,
            }
            for key, value in snapshot.known_comments.items()
        },
        "known_badges": {
            key: {
                "badge_key": value.badge_key,
                "name": value.name,
                "level": value.level,
                "app_id": value.app_id,
            }
            for key, value in snapshot.known_badges.items()
        },
        "display_name": snapshot.display_name,
        "profile_url": snapshot.profile_url,
    }


def snapshot_from_json(data: Dict[str, Any]) -> AccountSnapshot:
    return AccountSnapshot(
        persona_state=int(data.get("persona_state", 0) or 0),
        game_id=data.get("game_id"),
        game_name=data.get("game_name"),
        rich_presence=data.get("rich_presence", ""),
        cs2_mode=data.get("cs2_mode", ""),
        cs2_map=data.get("cs2_map", ""),
        cs2_score=data.get("cs2_score", ""),
        friends=json_to_set(data.get("friends")),
        badges=json_to_set(data.get("badges")),
        comments=json_to_set(data.get("comments")),
        known_comments={
            str(key): CommentInfo(
                comment_id=str(value.get("comment_id", key)),
                author=value.get("author", "неизвестный автор"),
                author_steam_id=value.get("author_steam_id", ""),
                author_profile_url=value.get("author_profile_url", ""),
                created_at=value.get("created_at", ""),
                text=value.get("text", ""),
            )
            for key, value in data.get("known_comments", {}).items()
            if isinstance(value, dict)
        },
        known_badges={
            str(key): BadgeInfo(
                badge_key=str(value.get("badge_key", key)),
                name=value.get("name", str(key)),
                level=value.get("level"),
                app_id=value.get("app_id"),
            )
            for key, value in data.get("known_badges", {}).items()
            if isinstance(value, dict)
        },
        display_name=data.get("display_name", ""),
        profile_url=data.get("profile_url", ""),
    )


def timeline_to_json(timeline: AccountTimeline) -> Dict[str, Any]:
    return {
        "observed_since": dt_to_json(timeline.observed_since),
        "online_started_at": dt_to_json(timeline.online_started_at),
        "game_started_at": dt_to_json(timeline.game_started_at),
        "idle_started_at": dt_to_json(timeline.idle_started_at),
        "offline_started_at": dt_to_json(timeline.offline_started_at),
        "last_cs2_mode": timeline.last_cs2_mode,
        "last_cs2_map": timeline.last_cs2_map,
        "last_cs2_score": timeline.last_cs2_score,
        "last_reminder_at": dt_to_json(timeline.last_reminder_at),
        "last_reminder_key": timeline.last_reminder_key,
    }


def timeline_from_json(data: Dict[str, Any], fallback: datetime) -> AccountTimeline:
    return AccountTimeline(
        observed_since=parse_dt(data.get("observed_since")) or fallback,
        online_started_at=parse_dt(data.get("online_started_at")),
        game_started_at=parse_dt(data.get("game_started_at")),
        idle_started_at=parse_dt(data.get("idle_started_at")),
        offline_started_at=parse_dt(data.get("offline_started_at")),
        last_cs2_mode=data.get("last_cs2_mode", ""),
        last_cs2_map=data.get("last_cs2_map", ""),
        last_cs2_score=data.get("last_cs2_score", ""),
        last_reminder_at=parse_dt(data.get("last_reminder_at")),
        last_reminder_key=data.get("last_reminder_key", ""),
    )


def match_to_json(match: CS2MatchRecord) -> Dict[str, Any]:
    return {
        "completed_at": dt_to_json(match.completed_at),
        "mode": match.mode,
        "map_name": match.map_name,
        "score": match.score,
        "result": match.result,
        "rich_presence": match.rich_presence,
    }


def match_from_json(data: Dict[str, Any]) -> Optional[CS2MatchRecord]:
    completed_at = parse_dt(data.get("completed_at"))
    if not completed_at:
        return None
    return CS2MatchRecord(
        completed_at=completed_at,
        mode=data.get("mode", ""),
        map_name=data.get("map_name", ""),
        score=data.get("score", ""),
        result=data.get("result", ""),
        rich_presence=data.get("rich_presence", ""),
    )


def _get_bool(config: configparser.ConfigParser, section: str, option: str, fallback: bool) -> bool:
    if not config.has_option(section, option):
        return fallback
    return config.getboolean(section, option)


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

    return MonitorConfig(
        bot_token=config.get("telegram", "bot_token").strip(),
        chat_id=int(config.get("telegram", "chat_id").strip()),
        allowed_user_id=allowed_user_id,
        telegram_proxy=telegram_proxy,
        steam_api_key=config.get("steam", "api_key").strip(),
        poll_interval_seconds=max(15, config.getint("steam", "poll_interval_seconds", fallback=60)),
        status_reminder_interval_seconds=max(0, config.getint("steam", "status_reminder_interval_seconds", fallback=3600)),
        notify_on_start=_get_bool(config, "steam", "notify_on_start", False),
        monitor_comments=_get_bool(config, "steam", "monitor_comments", True),
        monitor_friends=_get_bool(config, "steam", "monitor_friends", True),
        monitor_badges=_get_bool(config, "steam", "monitor_badges", True),
        monitor_rich_presence=_get_bool(config, "steam", "monitor_rich_presence", True),
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


def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def plain_text_from_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def trim(value: str, max_len: int = 260) -> str:
    value = " ".join(value.split())
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "..."


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def format_dt(value: datetime) -> str:
    return value.astimezone(LOCAL_TZ).strftime(f"%d.%m.%Y %H:%M:%S {LOCAL_TZ.tzname(None)}")


def format_duration(start: Optional[datetime], end: datetime) -> str:
    if start is None:
        return "только что"

    total_seconds = max(0, int((end - start).total_seconds()))
    if total_seconds < 5:
        return "только что"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts: List[str] = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if not parts:
        parts.append(f"{seconds} сек")
    return " ".join(parts[:3])


def format_optional_count(value: Optional[Set[str]]) -> str:
    return "недоступно" if value is None else str(len(value))


def format_interval(seconds: int) -> str:
    if seconds <= 0:
        return "выключены"
    start = now_local()
    return format_duration(start, start + timedelta(seconds=seconds))


def html_text(value: Any) -> str:
    return html.escape(str(value), quote=False)


def html_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def safe_filename(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return value.strip("._") or "account"


def steamid64_to_account_id(steam_id: str) -> Optional[int]:
    try:
        return int(steam_id) - STEAMID64_ACCOUNT_ID_BASE
    except ValueError:
        return None


def parse_cs2_rich_presence(value: str) -> Tuple[str, str, str]:
    value = " ".join(value.split())
    match = re.search(r"^(Premier|Competitive)\s*-\s*(.*?)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]$", value, flags=re.IGNORECASE)
    if match:
        mode = match.group(1).title()
        map_name = match.group(2).strip()
        score = f"{match.group(3)}:{match.group(4)}"
        return mode, map_name, score

    if value.lower() in {"lobby", "in lobby"}:
        return "Lobby", "", ""
    if value:
        return value, "", ""
    return "", "", ""


def score_result(score: str) -> str:
    match = re.match(r"^\s*(\d+)\s*:\s*(\d+)\s*$", score)
    if not match:
        return "unknown"
    left = int(match.group(1))
    right = int(match.group(2))
    if left > right:
        return "win"
    if left < right:
        return "loss"
    return "draw"


def result_label(result: str) -> str:
    return {
        "win": "победа",
        "loss": "поражение",
        "draw": "ничья",
    }.get(result, "неизвестно")


def result_emoji(result: str) -> str:
    return {
        "win": "✅",
        "loss": "❌",
        "draw": "➖",
    }.get(result, "❔")


def date_key(value: datetime) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def split_message(text: str, max_len: int = 3500) -> List[str]:
    if len(text) <= max_len:
        return [text]

    chunks_out: List[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks_out.append(current)
        current = line
        while len(current) > max_len:
            chunks_out.append(current[:max_len])
            current = current[max_len:]
    if current:
        chunks_out.append(current)
    return chunks_out


class SteamApiClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.session = session
        self.access_warnings: Set[Tuple[Any, ...]] = set()

    def warn_access_once(self, key: Tuple[Any, ...], message: str, *args: Any) -> None:
        if key in self.access_warnings:
            return
        self.access_warnings.add(key)
        logger.warning(message, *args)

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for attempt in range(1, len(STEAM_RETRY_DELAYS) + 2):
            try:
                async with self.session.get(url, params=params, timeout=20) as response:
                    if response.status == 200:
                        return await response.json(content_type=None)
                    if response.status in {401, 403}:
                        steam_id = str(params.get("steamid") or params.get("steamids") or "")
                        self.warn_access_once(
                            ("steam-api-access", url, steam_id, response.status),
                            "Steam вернул HTTP %s для %s%s. Проверьте Steam Web API key и приватность профиля.",
                            response.status,
                            url,
                            f" steamid={steam_id}" if steam_id else "",
                        )
                        return None
                    if response.status not in {429, 500, 502, 503, 504}:
                        logger.warning("Steam вернул HTTP %s для %s", response.status, url)
                        return None
                    logger.warning("Steam вернул HTTP %s для %s, попытка %s", response.status, url, attempt)
            except asyncio.TimeoutError:
                logger.warning("Таймаут запроса к Steam: %s, попытка %s", url, attempt)
            except (aiohttp.ClientError, OSError) as exc:
                logger.warning("Ошибка запроса к Steam %s: %s, попытка %s", url, exc, attempt)

            if attempt <= len(STEAM_RETRY_DELAYS):
                await asyncio.sleep(STEAM_RETRY_DELAYS[attempt - 1])
        return None

    async def get_player_summaries(self, steam_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        players: Dict[str, Dict[str, Any]] = {}
        url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v0002/"

        for chunk in chunks(steam_ids, 100):
            data = await self._get_json(
                url,
                {
                    "key": self.api_key,
                    "steamids": ",".join(chunk),
                },
            )
            for player in data.get("response", {}).get("players", []) if data else []:
                players[str(player.get("steamid"))] = player
        return players

    async def get_friend_ids(self, steam_id: str) -> Optional[Set[str]]:
        url = f"{STEAM_API_BASE}/ISteamUser/GetFriendList/v0001/"
        data = await self._get_json(
            url,
            {
                "key": self.api_key,
                "steamid": steam_id,
                "relationship": "friend",
            },
        )
        if not data or "friendslist" not in data:
            return None
        return {str(friend.get("steamid")) for friend in data["friendslist"].get("friends", []) if friend.get("steamid")}

    async def get_badges(self, steam_id: str) -> Tuple[Optional[Set[str]], Dict[str, BadgeInfo]]:
        url = f"{STEAM_API_BASE}/IPlayerService/GetBadges/v1/"
        data = await self._get_json(
            url,
            {
                "key": self.api_key,
                "steamid": steam_id,
            },
        )
        if not data or "response" not in data:
            return None, {}

        badges: Set[str] = set()
        info: Dict[str, BadgeInfo] = {}
        for badge in data["response"].get("badges", []):
            badge_id = badge.get("badgeid")
            app_id = badge.get("appid")
            community_item_id = badge.get("communityitemid")
            level = badge.get("level")
            border_color = badge.get("border_color")
            badge_key = f"{badge_id}:{app_id or 0}:{community_item_id or 0}:{border_color or 0}"
            name = f"Badge {badge_id}"
            if app_id:
                name += f" для app {app_id}"
            badges.add(badge_key)
            info[badge_key] = BadgeInfo(
                badge_key=badge_key,
                name=name,
                level=int(level) if isinstance(level, int) else None,
                app_id=int(app_id) if isinstance(app_id, int) else None,
            )
        return badges, info

    async def get_miniprofile(self, steam_id: str) -> Optional[Dict[str, Any]]:
        account_id = steamid64_to_account_id(steam_id)
        if account_id is None or account_id <= 0:
            return None

        url = f"{STEAM_COMMUNITY_BASE}/miniprofile/{account_id}/json"
        for attempt in range(1, len(STEAM_RETRY_DELAYS) + 2):
            try:
                async with self.session.get(url, timeout=20) as response:
                    if response.status == 200:
                        return await response.json(content_type=None)
                    if response.status in {401, 403, 404}:
                        self.warn_access_once(
                            ("steam-miniprofile-access", steam_id, response.status),
                            "Steam miniprofile вернул HTTP %s для %s. Rich presence может быть недоступен.",
                            response.status,
                            steam_id,
                        )
                        return None
                    if response.status not in {429, 500, 502, 503, 504}:
                        logger.warning("Steam miniprofile вернул HTTP %s для %s", response.status, steam_id)
                        return None
                    logger.warning("Steam miniprofile вернул HTTP %s для %s, попытка %s", response.status, steam_id, attempt)
            except asyncio.TimeoutError:
                logger.warning("Таймаут запроса Steam miniprofile %s, попытка %s", steam_id, attempt)
            except (aiohttp.ClientError, OSError) as exc:
                logger.warning("Ошибка запроса Steam miniprofile %s: %s, попытка %s", steam_id, exc, attempt)

            if attempt <= len(STEAM_RETRY_DELAYS):
                await asyncio.sleep(STEAM_RETRY_DELAYS[attempt - 1])
        return None

    async def get_profile_comments(self, steam_id: str) -> Tuple[Optional[Set[str]], Dict[str, CommentInfo]]:
        url = f"{STEAM_COMMUNITY_BASE}/comment/Profile/render/{steam_id}/-1/"
        params = {
            "start": 0,
            "count": 10,
            "feature2": -1,
        }

        raw = None
        for attempt in range(1, len(STEAM_RETRY_DELAYS) + 2):
            try:
                async with self.session.get(url, params=params, timeout=20) as response:
                    if response.status == 200:
                        raw = await response.text()
                        break
                    if response.status in {401, 403}:
                        self.warn_access_once(
                            ("steam-community-comments-access", steam_id, response.status),
                            "Steam Community вернул HTTP %s для comments %s. Проверьте приватность профиля.",
                            response.status,
                            steam_id,
                        )
                        return None, {}
                    if response.status not in {429, 500, 502, 503, 504}:
                        logger.warning("Steam Community вернул HTTP %s для comments %s", response.status, steam_id)
                        return None, {}
                    logger.warning("Steam Community вернул HTTP %s для comments %s, попытка %s", response.status, steam_id, attempt)
            except asyncio.TimeoutError:
                logger.warning("Таймаут запроса комментариев профиля %s, попытка %s", steam_id, attempt)
            except (aiohttp.ClientError, OSError) as exc:
                logger.warning("Ошибка запроса комментариев профиля %s: %s, попытка %s", steam_id, exc, attempt)

            if attempt <= len(STEAM_RETRY_DELAYS):
                await asyncio.sleep(STEAM_RETRY_DELAYS[attempt - 1])

        if raw is None:
            return None, {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Steam Community вернул не JSON для comments %s", steam_id)
            return None, {}

        comments_html = data.get("comments_html") or ""
        comment_ids = set(re.findall(r'id="comment_(\d+)"', comments_html))
        comments: Dict[str, CommentInfo] = {}

        for comment_id in comment_ids:
            block_match = re.search(
                rf'id="comment_{re.escape(comment_id)}".*?(?=id="comment_\d+"|$)',
                comments_html,
                flags=re.DOTALL,
            )
            block = block_match.group(0) if block_match else ""
            author = "неизвестный автор"
            author_steam_id = ""
            author_profile_url = ""
            created_at = ""
            text = ""

            author_match = re.search(
                r'<a\b(?=[^>]*commentthread_author_link)([^>]*)>(.*?)</a>',
                block,
                flags=re.DOTALL,
            )
            if author_match:
                author_attrs = author_match.group(1)
                author = plain_text_from_html(author_match.group(2)) or author
                href_match = re.search(r'href="([^"]+)"', author_attrs)
                if href_match:
                    author_profile_url = html.unescape(href_match.group(1))
                    steam_id_match = re.search(r"/profiles/(\d+)", author_profile_url)
                    if steam_id_match:
                        author_steam_id = steam_id_match.group(1)

            timestamp_match = re.search(
                r'<(?P<tag>\w+)\b(?=[^>]*commentthread_comment_timestamp)([^>]*)>(.*?)</(?P=tag)>',
                block,
                flags=re.DOTALL,
            )
            if timestamp_match:
                timestamp_attrs = timestamp_match.group(2)
                unix_match = re.search(r'data-timestamp="(\d+)"', timestamp_attrs)
                title_match = re.search(r'title="([^"]+)"', timestamp_attrs)
                if unix_match:
                    created_at = format_dt(datetime.fromtimestamp(int(unix_match.group(1)), LOCAL_TZ))
                elif title_match:
                    created_at = html.unescape(title_match.group(1)).strip()
                else:
                    created_at = plain_text_from_html(timestamp_match.group(3))

            text_match = re.search(r'<div\b(?=[^>]*commentthread_comment_text)[^>]*>(.*?)</div>', block, flags=re.DOTALL)
            if text_match:
                text = trim(plain_text_from_html(text_match.group(1)))

            comments[comment_id] = CommentInfo(
                comment_id=comment_id,
                author=author,
                author_steam_id=author_steam_id,
                author_profile_url=author_profile_url,
                created_at=created_at,
                text=text,
            )

        return comment_ids, comments


class SteamProfileMonitor:
    def __init__(self, config: MonitorConfig, bot: Bot, steam: SteamApiClient):
        self.config = config
        self.bot = bot
        self.steam = steam
        self.snapshots: Dict[str, AccountSnapshot] = {}
        self.timelines: Dict[str, AccountTimeline] = {}
        self.cs2_daily_matches: Dict[str, Dict[str, List[CS2MatchRecord]]] = {}
        self.account_loggers: Dict[str, logging.Logger] = {}
        self.account_by_id = {account.steam_id: account for account in config.accounts}
        self.visibility_warnings: Set[str] = set()
        self.load_state()

    async def send(self, text: str) -> None:
        for attempt in range(1, len(TELEGRAM_SEND_RETRY_DELAYS) + 2):
            try:
                for chunk in split_message(text):
                    await self.bot.send_message(
                        self.config.chat_id,
                        chunk,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                return
            except TelegramNetworkError as exc:
                logger.warning("Не удалось отправить сообщение в Telegram: %s, попытка %s", exc, attempt)

            if attempt <= len(TELEGRAM_SEND_RETRY_DELAYS):
                await asyncio.sleep(TELEGRAM_SEND_RETRY_DELAYS[attempt - 1])

    async def send_private(self, message: Message, text: str) -> None:
        if self.config.allowed_user_id and message.from_user and message.from_user.id != self.config.allowed_user_id:
            await message.answer("🔒 <b>Доступ ограничен.</b>", parse_mode="HTML")
            return
        for chunk in split_message(text):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)

    async def run_forever(self) -> None:
        logger.info("Мониторинг запущен. Аккаунтов: %s", len(self.config.accounts))
        started_at = now_local()
        for account in self.config.accounts:
            self.log_account(
                account,
                logging.INFO,
                "%s | monitor_started | Аккаунт подключен к мониторингу. Интервал=%s сек.",
                format_dt(started_at),
                self.config.poll_interval_seconds,
            )
        await self.send(
            "🟢 <b>Steam Profile Monitor запущен</b>\n"
            f"🕒 <code>{html_text(format_dt(started_at))}</code>\n\n"
            f"👥 Аккаунтов: <b>{len(self.config.accounts)}</b>\n"
            f"🔁 Проверка: <b>{self.config.poll_interval_seconds} сек.</b>\n"
            f"⏰ Напоминания: <b>{html_text(format_interval(self.config.status_reminder_interval_seconds))}</b>"
            f"{' (кроме статуса «не в сети»)' if self.config.status_reminder_interval_seconds > 0 else ''}\n\n"
            "Команды: /status, /accounts, /cs2today"
        )

        first_run = True
        while True:
            try:
                await self.poll_once(send_initial=self.config.notify_on_start and first_run)
                first_run = False
            except Exception:
                logger.exception("Ошибка цикла мониторинга")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def poll_once(self, send_initial: bool = False) -> None:
        checked_at = now_local()
        steam_ids = [account.steam_id for account in self.config.accounts]
        summaries = await self.steam.get_player_summaries(steam_ids)
        logger.info("Проверка Steam: %s, аккаунтов=%s, получено=%s", format_dt(checked_at), len(steam_ids), len(summaries))

        for account in self.config.accounts:
            old_snapshot = self.snapshots.get(account.steam_id)
            if account.steam_id not in summaries:
                logger.warning(
                    "Нет данных GetPlayerSummaries для %s (%s). Предыдущий снимок сохранен, изменений не фиксирую.",
                    account.label,
                    account.steam_id,
                )
                self.log_account(
                    account,
                    logging.WARNING,
                    "%s | summary_missing | Нет данных GetPlayerSummaries. Предыдущий снимок сохранен.",
                    format_dt(checked_at),
                )
                continue

            player = summaries.get(account.steam_id, {})
            new_snapshot = await self.build_snapshot(account, player)
            self.log_snapshot(account, new_snapshot, checked_at)

            if old_snapshot is None:
                timeline = self.timeline_for_initial_snapshot(new_snapshot, checked_at)
                self.snapshots[account.steam_id] = new_snapshot
                self.timelines[account.steam_id] = timeline
                self.log_change(checked_at, account, "initial", self.snapshot_log_details(new_snapshot))
                await self.send(await self.format_initial_status(account, new_snapshot, timeline, checked_at, is_new_account=True))
                reminder = self.maybe_status_reminder(account, new_snapshot, timeline, checked_at)
                if reminder:
                    await self.send(reminder)
                self.save_state()
                continue

            timeline = self.timelines.setdefault(
                account.steam_id,
                self.timeline_for_initial_snapshot(old_snapshot, checked_at),
            )
            events = await self.compare_snapshots(account, old_snapshot, new_snapshot, timeline, checked_at)
            updated_timeline = self.next_timeline(timeline, old_snapshot, new_snapshot, checked_at)
            self.timelines[account.steam_id] = updated_timeline
            self.snapshots[account.steam_id] = new_snapshot

            for event in events:
                await self.send(event)

            reminder = self.maybe_status_reminder(account, new_snapshot, updated_timeline, checked_at)
            if reminder:
                await self.send(reminder)

            self.save_state()

    def account_logger(self, account: MonitoredAccount) -> logging.Logger:
        existing = self.account_loggers.get(account.steam_id)
        if existing:
            return existing

        ACCOUNT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_name = f"{safe_filename(account.label)}_{safe_filename(account.steam_id)}.log"
        account_logger = logging.getLogger(f"steam-profile-monitor.account.{account.steam_id}")
        account_logger.setLevel(logging.INFO)
        account_logger.propagate = False

        if not account_logger.handlers:
            handler = logging.FileHandler(ACCOUNT_LOG_DIR / file_name, encoding="utf-8", delay=True)
            handler.setFormatter(log_formatter)
            account_logger.addHandler(handler)

        self.account_loggers[account.steam_id] = account_logger
        return account_logger

    def log_account(self, account: MonitoredAccount, level: int, message: str, *args: Any) -> None:
        self.account_logger(account).log(level, message, *args)

    def load_state(self) -> None:
        if not STATE_PATH.exists():
            logger.info("Файл состояния не найден, стартую без сохраненных снимков: %s", STATE_PATH)
            return

        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать файл состояния %s: %s", STATE_PATH, exc)
            return

        loaded_at = now_local()
        account_ids = set(self.account_by_id)
        snapshots = data.get("snapshots", {})
        timelines = data.get("timelines", {})
        cs2_daily_matches = data.get("cs2_daily_matches", {})

        for steam_id, snapshot_data in snapshots.items():
            if steam_id not in account_ids or not isinstance(snapshot_data, dict):
                continue
            self.snapshots[steam_id] = snapshot_from_json(snapshot_data)

        for steam_id, timeline_data in timelines.items():
            if steam_id not in account_ids or not isinstance(timeline_data, dict):
                continue
            self.timelines[steam_id] = timeline_from_json(timeline_data, loaded_at)

        for steam_id, by_day in cs2_daily_matches.items():
            if steam_id not in account_ids or not isinstance(by_day, dict):
                continue
            self.cs2_daily_matches[steam_id] = {}
            for day, matches_data in by_day.items():
                if not isinstance(matches_data, list):
                    continue
                matches = [match_from_json(item) for item in matches_data if isinstance(item, dict)]
                self.cs2_daily_matches[steam_id][day] = [match for match in matches if match is not None]

        logger.info("Загружено сохраненное состояние: снимков=%s, файл=%s", len(self.snapshots), STATE_PATH)

    def save_state(self) -> None:
        data = {
            "version": 1,
            "saved_at": dt_to_json(now_local()),
            "snapshots": {steam_id: snapshot_to_json(snapshot) for steam_id, snapshot in self.snapshots.items()},
            "timelines": {steam_id: timeline_to_json(timeline) for steam_id, timeline in self.timelines.items()},
            "cs2_daily_matches": {
                steam_id: {
                    day: [match_to_json(match) for match in matches]
                    for day, matches in by_day.items()
                }
                for steam_id, by_day in self.cs2_daily_matches.items()
            },
        }
        tmp_path = STATE_PATH.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(STATE_PATH)
        except OSError as exc:
            logger.warning("Не удалось сохранить состояние %s: %s", STATE_PATH, exc)

    def timeline_for_initial_snapshot(self, snapshot: AccountSnapshot, checked_at: datetime) -> AccountTimeline:
        return AccountTimeline(
            observed_since=checked_at,
            online_started_at=checked_at if snapshot.online else None,
            game_started_at=checked_at if snapshot.game_id else None,
            idle_started_at=checked_at if snapshot.online and not snapshot.game_id else None,
            offline_started_at=checked_at if not snapshot.online else None,
            last_cs2_mode=snapshot.cs2_mode,
            last_cs2_map=snapshot.cs2_map,
            last_cs2_score=snapshot.cs2_score,
            last_reminder_key=self.state_key(snapshot),
        )

    def next_timeline(
        self,
        timeline: AccountTimeline,
        old: AccountSnapshot,
        new: AccountSnapshot,
        checked_at: datetime,
    ) -> AccountTimeline:
        online_started_at = timeline.online_started_at if old.online and new.online else checked_at if new.online else None
        game_started_at = timeline.game_started_at if old.game_id and old.game_id == new.game_id else checked_at if new.game_id else None
        idle_started_at = timeline.idle_started_at if old.online and not old.game_id and new.online and not new.game_id else checked_at if new.online and not new.game_id else None
        offline_started_at = timeline.offline_started_at if not old.online and not new.online else checked_at if not new.online else None
        old_key = self.state_key(old)
        new_key = self.state_key(new)
        last_reminder_at = timeline.last_reminder_at if old_key == new_key else None
        last_cs2_mode = timeline.last_cs2_mode
        last_cs2_map = timeline.last_cs2_map
        last_cs2_score = timeline.last_cs2_score
        if new.game_id == CS2_APP_ID and new.cs2_score:
            last_cs2_mode = new.cs2_mode
            last_cs2_map = new.cs2_map
            last_cs2_score = new.cs2_score
        elif old.game_id != CS2_APP_ID and new.game_id != CS2_APP_ID:
            last_cs2_mode = ""
            last_cs2_map = ""
            last_cs2_score = ""

        return AccountTimeline(
            observed_since=timeline.observed_since,
            online_started_at=online_started_at,
            game_started_at=game_started_at,
            idle_started_at=idle_started_at,
            offline_started_at=offline_started_at,
            last_cs2_mode=last_cs2_mode,
            last_cs2_map=last_cs2_map,
            last_cs2_score=last_cs2_score,
            last_reminder_at=last_reminder_at,
            last_reminder_key=new_key,
        )

    def state_key(self, snapshot: AccountSnapshot) -> str:
        if snapshot.game_id:
            return f"game:{snapshot.game_id}"
        if snapshot.online:
            return f"online:{snapshot.persona_state}"
        return "offline"

    def state_started_at(self, snapshot: AccountSnapshot, timeline: AccountTimeline) -> datetime:
        if snapshot.game_id and timeline.game_started_at:
            return timeline.game_started_at
        if snapshot.online and timeline.idle_started_at:
            return timeline.idle_started_at
        if snapshot.online and timeline.online_started_at:
            return timeline.online_started_at
        return timeline.offline_started_at or timeline.observed_since

    def maybe_status_reminder(
        self,
        account: MonitoredAccount,
        snapshot: AccountSnapshot,
        timeline: AccountTimeline,
        checked_at: datetime,
    ) -> Optional[str]:
        interval = self.config.status_reminder_interval_seconds
        if interval <= 0:
            return None

        if not snapshot.online:
            return None

        state_started_at = self.state_started_at(snapshot, timeline)
        if (checked_at - state_started_at).total_seconds() < interval:
            return None

        if timeline.last_reminder_at and (checked_at - timeline.last_reminder_at).total_seconds() < interval:
            return None

        timeline.last_reminder_at = checked_at
        timeline.last_reminder_key = self.state_key(snapshot)
        self.log_change(
            checked_at,
            account,
            "status_reminder",
            f"{self.current_state_text(snapshot)}; duration={format_duration(state_started_at, checked_at)}",
        )
        return self.format_status_reminder(account, snapshot, timeline, checked_at)

    def snapshot_log_details(self, snapshot: AccountSnapshot) -> str:
        game = f"{snapshot.game_name or snapshot.game_id} ({snapshot.game_id})" if snapshot.game_id else "нет"
        return (
            f"display={snapshot.display_name or '-'}; "
            f"state={PERSONA_STATES.get(snapshot.persona_state, snapshot.persona_state)}; "
            f"game={game}; "
            f"rich_presence={snapshot.rich_presence or '-'}; "
            f"cs2={self.cs2_summary(snapshot) or '-'}; "
            f"friends={format_optional_count(snapshot.friends)}; "
            f"badges={format_optional_count(snapshot.badges)}; "
            f"comments={format_optional_count(snapshot.comments)}; "
            f"profile={snapshot.profile_url}"
        )

    def log_snapshot(self, account: MonitoredAccount, snapshot: AccountSnapshot, checked_at: datetime) -> None:
        message = f"SNAPSHOT {format_dt(checked_at)} | {account.label} ({account.steam_id}) | {self.snapshot_log_details(snapshot)}"
        logger.info(
            message,
        )
        self.log_account(account, logging.INFO, message)

    def log_change(self, checked_at: datetime, account: MonitoredAccount, kind: str, details: str) -> None:
        line = f"{format_dt(checked_at)} | {account.label} | {account.steam_id} | {kind} | {details}"
        logger.info("CHANGE %s", line)
        change_logger.info(line)
        self.log_account(account, logging.INFO, "CHANGE %s", line)

    def record_cs2_match(
        self,
        account: MonitoredAccount,
        completed_at: datetime,
        mode: str,
        map_name: str,
        score: str,
        rich_presence: str,
    ) -> CS2MatchRecord:
        match = CS2MatchRecord(
            completed_at=completed_at,
            mode=mode,
            map_name=map_name,
            score=score,
            result=score_result(score),
            rich_presence=rich_presence,
        )
        day = date_key(completed_at)
        matches = self.cs2_daily_matches.setdefault(account.steam_id, {}).setdefault(day, [])
        matches.append(match)
        self.log_change(
            completed_at,
            account,
            "cs2_match_completed",
            f"{mode}; map={map_name or '-'}; score={score}; result={match.result}; day_matches={len(matches)}",
        )
        return match

    def cs2_day_matches(self, account: MonitoredAccount, checked_at: datetime) -> List[CS2MatchRecord]:
        return self.cs2_daily_matches.get(account.steam_id, {}).get(date_key(checked_at), [])

    def cs2_daily_summary_html(self, account: MonitoredAccount, checked_at: datetime) -> List[str]:
        matches = self.cs2_day_matches(account, checked_at)
        wins = sum(1 for match in matches if match.result == "win")
        losses = sum(1 for match in matches if match.result == "loss")
        draws = sum(1 for match in matches if match.result == "draw")
        lines = [
            f"📅 <b>Матчи за {html_text(checked_at.strftime('%d.%m.%Y'))}</b>",
            f"Итого: <b>{len(matches)}</b> · ✅ {wins} · ❌ {losses} · ➖ {draws}",
        ]
        for index, match in enumerate(matches, 1):
            map_part = f" · {html_text(match.map_name)}" if match.map_name else ""
            lines.append(
                f"{index}. {result_emoji(match.result)} "
                f"<code>{html_text(match.completed_at.strftime('%H:%M'))}</code> "
                f"{html_text(match.mode)}{map_part} · "
                f"<b>{html_text(match.score)}</b> · {html_text(result_label(match.result))}"
            )
        return lines

    def format_cs2_daily_report(self) -> str:
        checked_at = now_local()
        lines = [
            "📅 <b>CS2 матчи за сегодня</b>",
            f"🕒 <code>{html_text(format_dt(checked_at))}</code>",
        ]
        any_matches = False
        for account in self.config.accounts:
            matches = self.cs2_day_matches(account, checked_at)
            lines.append(f"\n👤 <b>{html_text(account.label)}</b>")
            if not matches:
                lines.append("Пока завершённых матчей нет.")
                continue
            any_matches = True
            lines.extend(self.cs2_daily_summary_html(account, checked_at))
        if not any_matches:
            lines.append("\nЗавершённые Premier/Competitive матчи появятся здесь после ухода CS2 rich presence в Lobby/In Game или выхода из игры.")
        return "\n".join(lines)

    def current_state_text(self, snapshot: AccountSnapshot) -> str:
        status = PERSONA_STATES.get(snapshot.persona_state, str(snapshot.persona_state))
        if snapshot.game_id:
            rich_presence = self.cs2_summary(snapshot) if snapshot.game_id == CS2_APP_ID else snapshot.rich_presence
            suffix = f" — {rich_presence}" if rich_presence else ""
            return f"в игре: {snapshot.game_name or snapshot.game_id}{suffix}"
        if snapshot.online:
            return f"{status}, без игры"
        return "не в сети"

    def cs2_summary(self, snapshot: AccountSnapshot) -> str:
        if snapshot.game_id != CS2_APP_ID:
            return ""
        if snapshot.cs2_mode in {"Premier", "Competitive"} and snapshot.cs2_score:
            map_part = f" · {snapshot.cs2_map}" if snapshot.cs2_map else ""
            return f"{snapshot.cs2_mode}{map_part} · счет {snapshot.cs2_score}"
        return snapshot.rich_presence or snapshot.cs2_mode

    def last_cs2_score_text(self, timeline: AccountTimeline) -> str:
        if not timeline.last_cs2_score:
            return ""
        parts = []
        if timeline.last_cs2_mode:
            parts.append(timeline.last_cs2_mode)
        if timeline.last_cs2_map:
            parts.append(timeline.last_cs2_map)
        parts.append(f"счет {timeline.last_cs2_score}")
        return " · ".join(parts)

    def state_emoji(self, snapshot: AccountSnapshot) -> str:
        if snapshot.game_id:
            return "🎮"
        if snapshot.online:
            return "🟢"
        return "⚫"

    def duration_lines(self, snapshot: AccountSnapshot, timeline: AccountTimeline, checked_at: datetime) -> List[str]:
        if snapshot.game_id:
            lines = [
                f"🟢 В сети: <b>{html_text(format_duration(timeline.online_started_at, checked_at))}</b>",
                f"🎮 В игре: <b>{html_text(format_duration(timeline.game_started_at, checked_at))}</b>",
            ]
            if snapshot.game_id == CS2_APP_ID and not snapshot.cs2_score:
                last_score = self.last_cs2_score_text(timeline)
                if last_score:
                    lines.append(f"🏁 Последний счёт: <b>{html_text(last_score)}</b>")
            return lines
        if snapshot.online:
            return [
                f"🟢 В сети: <b>{html_text(format_duration(timeline.online_started_at, checked_at))}</b>",
                f"☕ Без игры: <b>{html_text(format_duration(timeline.idle_started_at, checked_at))}</b>",
            ]
        return [
            f"⚫ Не в сети: <b>{html_text(format_duration(timeline.offline_started_at, checked_at))}</b>",
            f"👁 Наблюдается: <b>{html_text(format_duration(timeline.observed_since, checked_at))}</b>",
        ]

    def format_detail_lines(
        self,
        details: Optional[List[str]] = None,
        html_details: Optional[List[str]] = None,
    ) -> List[str]:
        if not details and not html_details:
            return []
        lines = ["", "<b>Детали</b>"]
        if details:
            lines.extend(f"• {html_text(detail)}" for detail in details)
        if html_details:
            lines.extend(f"• {detail}" for detail in html_details)
        return lines

    def format_event_message(
        self,
        account: MonitoredAccount,
        snapshot: AccountSnapshot,
        timeline: AccountTimeline,
        checked_at: datetime,
        event: str,
        details: Optional[List[str]] = None,
        html_details: Optional[List[str]] = None,
    ) -> str:
        lines = [
            f"🕒 <code>{html_text(format_dt(checked_at))}</code>",
            self.account_title(account, snapshot),
            "",
            f"🔔 <b>{html_text(event)}</b>",
            f"{self.state_emoji(snapshot)} Сейчас: <b>{html_text(self.current_state_text(snapshot))}</b>",
            *self.duration_lines(snapshot, timeline, checked_at),
            *self.format_detail_lines(details, html_details),
        ]
        return "\n".join(lines)

    def format_status_reminder(
        self,
        account: MonitoredAccount,
        snapshot: AccountSnapshot,
        timeline: AccountTimeline,
        checked_at: datetime,
    ) -> str:
        state_started_at = self.state_started_at(snapshot, timeline)
        return "\n".join(
            [
                f"⏰ <b>Промежуточный статус</b>",
                f"🕒 <code>{html_text(format_dt(checked_at))}</code>",
                self.account_title(account, snapshot),
                "",
                f"{self.state_emoji(snapshot)} Сейчас: <b>{html_text(self.current_state_text(snapshot))}</b>",
                f"⏳ В этом состоянии: <b>{html_text(format_duration(state_started_at, checked_at))}</b>",
                *self.duration_lines(snapshot, timeline, checked_at),
            ]
        )

    def badge_label(self, badge_key: str, badges: Dict[str, BadgeInfo]) -> str:
        badge = badges.get(badge_key)
        if not badge:
            return badge_key
        level = f", уровень {badge.level}" if badge.level is not None else ""
        return f"{badge.name}{level}"

    def badge_detail_html(self, badge_key: str, badges: Dict[str, BadgeInfo]) -> str:
        badge = badges.get(badge_key)
        if not badge:
            return f"🏅 <code>{html_text(badge_key)}</code>"

        parts = [f"🏅 <b>{html_text(badge.name)}</b>"]
        if badge.level is not None:
            parts.append(f"уровень <b>{badge.level}</b>")
        if badge.app_id is not None:
            parts.append(f'<a href="https://store.steampowered.com/app/{badge.app_id}/">app {badge.app_id}</a>')
        parts.append(f"key <code>{html_text(badge.badge_key)}</code>")
        return " · ".join(parts)

    def friend_detail_html(self, friend: FriendInfo) -> str:
        return (
            f'👥 <a href="{html_attr(friend.profile_url)}">{html_text(friend.name)}</a> '
            f"<code>{html_text(friend.steam_id)}</code>"
        )

    def comment_detail_html(self, comment_id: str, comment: Optional[CommentInfo]) -> List[str]:
        if not comment:
            return [f"💬 ID комментария: <code>{html_text(comment_id)}</code>"]

        author = html_text(comment.author)
        if comment.author_profile_url:
            author = f'<a href="{html_attr(comment.author_profile_url)}">{author}</a>'

        lines = [f"💬 Автор: {author}"]
        if comment.author_steam_id:
            lines.append(f"🆔 Автор SteamID: <code>{html_text(comment.author_steam_id)}</code>")
        if comment.created_at:
            lines.append(f"🕒 Написан: <code>{html_text(comment.created_at)}</code>")
        if comment.text:
            lines.append(f"📝 Текст: {html_text(comment.text)}")
        lines.append(f"Комментарий ID: <code>{html_text(comment_id)}</code>")
        return lines

    async def baseline_detail_html(self, snapshot: AccountSnapshot) -> List[str]:
        details = [
            f"👥 Друзья: <b>{html_text(format_optional_count(snapshot.friends))}</b>",
            f"🏅 Бейджи: <b>{html_text(format_optional_count(snapshot.badges))}</b>",
            f"💬 Комментарии из ответа Steam: <b>{html_text(format_optional_count(snapshot.comments))}</b>",
        ]

        if snapshot.friends:
            friends = await self.resolve_friend_infos(sorted(snapshot.friends))
            details.append("<b>Друзья</b>")
            details.extend(self.friend_detail_html(friend) for friend in friends)

        if snapshot.badges:
            details.append("<b>Бейджи</b>")
            details.extend(self.badge_detail_html(badge_key, snapshot.known_badges) for badge_key in sorted(snapshot.badges))

        if snapshot.game_id == CS2_APP_ID and snapshot.rich_presence:
            details.append(f"🎯 CS2 rich presence: <b>{html_text(self.cs2_summary(snapshot) or snapshot.rich_presence)}</b>")

        if snapshot.comments:
            details.append("<b>Последние комментарии</b>")
            for comment_id in sorted(snapshot.comments):
                details.extend(self.comment_detail_html(comment_id, snapshot.known_comments.get(comment_id)))

        return details

    async def build_snapshot(self, account: MonitoredAccount, player: Dict[str, Any]) -> AccountSnapshot:
        visibility_state = int(player.get("communityvisibilitystate", 0) or 0)
        if visibility_state and visibility_state != 3 and account.steam_id not in self.visibility_warnings:
            self.visibility_warnings.add(account.steam_id)
            logger.warning(
                "Профиль %s (%s) не public для Steam Web API: communityvisibilitystate=%s. "
                "Steam может скрывать игру и показывать статус offline.",
                account.label,
                account.steam_id,
                visibility_state,
            )
            self.log_account(
                account,
                logging.WARNING,
                "%s | visibility_warning | communityvisibilitystate=%s; Steam может скрывать игру и показывать offline.",
                format_dt(now_local()),
                visibility_state,
            )

        persona_state = int(player.get("personastate", 0) or 0)
        snapshot = AccountSnapshot(
            persona_state=persona_state,
            game_id=str(player.get("gameid")) if player.get("gameid") else None,
            game_name=player.get("gameextrainfo"),
            display_name=player.get("personaname") or account.label,
            profile_url=player.get("profileurl") or f"{STEAM_COMMUNITY_BASE}/profiles/{account.steam_id}",
        )

        if self.config.monitor_rich_presence and snapshot.game_id:
            miniprofile = await self.steam.get_miniprofile(account.steam_id)
            in_game = miniprofile.get("in_game", {}) if isinstance(miniprofile, dict) else {}
            rich_presence = str(in_game.get("rich_presence") or "").strip() if isinstance(in_game, dict) else ""
            snapshot.rich_presence = rich_presence
            if snapshot.game_id == CS2_APP_ID and rich_presence:
                snapshot.cs2_mode, snapshot.cs2_map, snapshot.cs2_score = parse_cs2_rich_presence(rich_presence)

        if self.config.monitor_friends:
            snapshot.friends = await self.steam.get_friend_ids(account.steam_id)

        if self.config.monitor_badges:
            snapshot.badges, snapshot.known_badges = await self.steam.get_badges(account.steam_id)

        if self.config.monitor_comments:
            snapshot.comments, snapshot.known_comments = await self.steam.get_profile_comments(account.steam_id)

        return snapshot

    async def compare_snapshots(
        self,
        account: MonitoredAccount,
        old: AccountSnapshot,
        new: AccountSnapshot,
        timeline: AccountTimeline,
        checked_at: datetime,
    ) -> List[str]:
        events: List[str] = []
        display_timeline = self.next_timeline(timeline, old, new, checked_at)
        cs2_match_completed = bool(
            old.game_id == CS2_APP_ID
            and old.cs2_mode in {"Premier", "Competitive"}
            and old.cs2_score
            and (new.game_id != CS2_APP_ID or not new.cs2_score)
        )

        if old.display_name and new.display_name and old.display_name != new.display_name:
            details = [f"Было: {old.display_name}", f"Стало: {new.display_name}"]
            self.log_change(checked_at, account, "display_name_changed", f"{old.display_name} -> {new.display_name}")
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "изменилось имя профиля", details))

        if old.profile_url and new.profile_url and old.profile_url != new.profile_url:
            details = [f"Было: {old.profile_url}", f"Стало: {new.profile_url}"]
            self.log_change(checked_at, account, "profile_url_changed", f"{old.profile_url} -> {new.profile_url}")
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "изменилась ссылка профиля", details))

        if old.online and new.online and old.persona_state != new.persona_state:
            old_status = PERSONA_STATES.get(old.persona_state, str(old.persona_state))
            new_status = PERSONA_STATES.get(new.persona_state, str(new.persona_state))
            details = [f"Было: {old_status}", f"Стало: {new_status}"]
            self.log_change(checked_at, account, "persona_state_changed", f"{old_status} -> {new_status}")
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "изменился статус Steam", details))

        if old.game_id and new.game_id and old.game_id == new.game_id and old.game_name != new.game_name:
            details = [f"GameID: {new.game_id}", f"Было: {old.game_name or old.game_id}", f"Стало: {new.game_name or new.game_id}"]
            self.log_change(checked_at, account, "game_name_changed", f"{old.game_name or old.game_id} -> {new.game_name or new.game_id}; game_id={new.game_id}")
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "изменилось название игры в API", details))

        if cs2_match_completed:
            match = self.record_cs2_match(
                account,
                checked_at,
                old.cs2_mode,
                old.cs2_map,
                old.cs2_score,
                old.rich_presence,
            )
            map_part = f" · {match.map_name}" if match.map_name else ""
            html_details = [
                f"🎯 Режим: <b>{html_text(match.mode)}</b>",
                f"🗺 Карта: <b>{html_text(match.map_name or 'неизвестно')}</b>",
                f"🏁 Финальный счёт: <b>{html_text(match.score)}</b>",
                f"{result_emoji(match.result)} Итог: <b>{html_text(result_label(match.result))}</b>",
                f"🧾 Было в Steam: <code>{html_text(match.rich_presence)}</code>",
                *self.cs2_daily_summary_html(account, checked_at),
            ]
            events.append(
                self.format_event_message(
                    account,
                    new,
                    display_timeline,
                    checked_at,
                    f"завершён матч CS2: {match.mode}{map_part} · {match.score}",
                    html_details=html_details,
                )
            )

        if old.game_id == CS2_APP_ID and new.game_id == CS2_APP_ID and old.rich_presence != new.rich_presence:
            if old.cs2_score and new.cs2_score:
                self.log_change(
                    checked_at,
                    account,
                    "cs2_score_changed",
                    f"{old.rich_presence or '-'} -> {new.rich_presence or '-'}; parsed={self.cs2_summary(new) or '-'}",
                )
            elif cs2_match_completed:
                self.log_change(
                    checked_at,
                    account,
                    "cs2_rich_presence_after_match",
                    f"{old.rich_presence or '-'} -> {new.rich_presence or '-'}; last_score={self.last_cs2_score_text(display_timeline) or '-'}",
                )
            else:
                details = [
                    f"Было: {old.rich_presence or 'нет rich presence'}",
                    f"Стало: {new.rich_presence or 'нет rich presence'}",
                ]
                if new.cs2_mode:
                    details.append(f"Режим: {new.cs2_mode}")
                if new.cs2_map:
                    details.append(f"Карта: {new.cs2_map}")
                if new.cs2_score:
                    details.append(f"Счёт: {new.cs2_score}")
                elif self.last_cs2_score_text(display_timeline):
                    details.append(f"Последний счёт матча: {self.last_cs2_score_text(display_timeline)}")
                self.log_change(
                    checked_at,
                    account,
                    "cs2_rich_presence_changed",
                    f"{old.rich_presence or '-'} -> {new.rich_presence or '-'}; parsed={self.cs2_summary(new) or '-'}; last_score={self.last_cs2_score_text(display_timeline) or '-'}",
                )
                events.append(self.format_event_message(account, new, display_timeline, checked_at, "изменился статус CS2", details))

        if not old.online and new.online:
            details = ["Начало онлайн-сессии: сейчас"]
            self.log_change(checked_at, account, "online_started", self.current_state_text(new))
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "зашел в сеть", details))

        if old.game_id and old.game_id != new.game_id:
            details = [
                f"Игра: {old.game_name or old.game_id}",
                f"Время в игре: {format_duration(timeline.game_started_at, checked_at)}",
            ]
            if old.game_id == CS2_APP_ID:
                if old.rich_presence:
                    details.append(f"Последний статус CS2: {old.rich_presence}")
                if self.last_cs2_score_text(display_timeline):
                    details.append(f"Последний счёт матча: {self.last_cs2_score_text(display_timeline)}")
            self.log_change(
                checked_at,
                account,
                "game_stopped",
                f"game={old.game_name or old.game_id}; game_id={old.game_id}; duration={format_duration(timeline.game_started_at, checked_at)}",
            )
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "вышел из игры", details))

        if new.game_id and old.game_id != new.game_id:
            details = [f"Игра: {new.game_name or new.game_id}"]
            if new.game_id == CS2_APP_ID and new.rich_presence:
                details.append(f"Статус CS2: {self.cs2_summary(new) or new.rich_presence}")
            if old.online and not old.game_id:
                details.append(f"До этого был в сети без игры: {format_duration(timeline.idle_started_at, checked_at)}")
            self.log_change(
                checked_at,
                account,
                "game_started",
                f"game={new.game_name or new.game_id}; game_id={new.game_id}",
            )
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "зашел в игру", details))

        if old.online and not new.online:
            details = [f"Время в сети: {format_duration(timeline.online_started_at, checked_at)}"]
            if old.game_id:
                details.append(f"Время в последней игре: {format_duration(timeline.game_started_at, checked_at)}")
            elif timeline.idle_started_at:
                details.append(f"В сети без игры: {format_duration(timeline.idle_started_at, checked_at)}")
            self.log_change(
                checked_at,
                account,
                "offline_started",
                f"online_duration={format_duration(timeline.online_started_at, checked_at)}",
            )
            events.append(self.format_event_message(account, new, display_timeline, checked_at, "вышел из сети", details))

        if old.friends is not None and new.friends is not None:
            added_friends = sorted(new.friends - old.friends)
            if added_friends:
                friends = await self.resolve_friend_infos(added_friends)
                for friend in friends:
                    self.log_change(checked_at, account, "friend_added", f"{friend.name} ({friend.steam_id}) {friend.profile_url}")
                visible = friends[:10]
                html_details = [f"Добавлен: {self.friend_detail_html(friend)}" for friend in visible]
                html_details.append(f"Всего друзей: <b>{len(new.friends)}</b>")
                if len(friends) > len(visible):
                    html_details.append(f"Еще добавлено: <b>{len(friends) - len(visible)}</b>")
                events.append(self.format_event_message(account, new, display_timeline, checked_at, "изменился список друзей", html_details=html_details))

            removed_friends = sorted(old.friends - new.friends)
            if removed_friends:
                friends = await self.resolve_friend_infos(removed_friends)
                for friend in friends:
                    self.log_change(checked_at, account, "friend_removed", f"{friend.name} ({friend.steam_id}) {friend.profile_url}")
                visible = friends[:10]
                html_details = [f"Удален: {self.friend_detail_html(friend)}" for friend in visible]
                html_details.append(f"Всего друзей: <b>{len(new.friends)}</b>")
                if len(friends) > len(visible):
                    html_details.append(f"Еще удалено: <b>{len(friends) - len(visible)}</b>")
                events.append(self.format_event_message(account, new, display_timeline, checked_at, "изменился список друзей", html_details=html_details))

        if old.badges is not None and new.badges is not None:
            added_badges = sorted(new.badges - old.badges)
            for badge_key in added_badges:
                badge_name = self.badge_label(badge_key, new.known_badges)
                self.log_change(checked_at, account, "badge_added", badge_name)
                events.append(
                    self.format_event_message(
                        account,
                        new,
                        display_timeline,
                        checked_at,
                        "получен новый бейдж",
                        html_details=[self.badge_detail_html(badge_key, new.known_badges)],
                    )
                )

            removed_badges = sorted(old.badges - new.badges)
            for badge_key in removed_badges:
                badge_name = self.badge_label(badge_key, old.known_badges)
                self.log_change(checked_at, account, "badge_removed", badge_name)
                events.append(
                    self.format_event_message(
                        account,
                        new,
                        display_timeline,
                        checked_at,
                        "бейдж пропал из API",
                        html_details=[self.badge_detail_html(badge_key, old.known_badges)],
                    )
                )

        if old.comments is not None and new.comments is not None:
            added_comments = sorted(new.comments - old.comments)
            for comment_id in added_comments:
                comment = new.known_comments.get(comment_id)
                if comment and comment.text:
                    comment_time = f"; created_at={comment.created_at}" if comment.created_at else ""
                    self.log_change(checked_at, account, "comment_added", f"{comment.author}: {comment.text}{comment_time}")
                elif comment:
                    comment_time = f"; created_at={comment.created_at}" if comment.created_at else ""
                    self.log_change(checked_at, account, "comment_added", f"{comment.author}{comment_time}")
                else:
                    self.log_change(checked_at, account, "comment_added", comment_id)
                events.append(
                    self.format_event_message(
                        account,
                        new,
                        display_timeline,
                        checked_at,
                        "новый комментарий в профиле",
                        html_details=self.comment_detail_html(comment_id, comment),
                    )
                )

            removed_comments = sorted(old.comments - new.comments)
            for comment_id in removed_comments:
                comment = old.known_comments.get(comment_id)
                if comment and comment.text:
                    comment_time = f"; created_at={comment.created_at}" if comment.created_at else ""
                    self.log_change(checked_at, account, "comment_removed", f"{comment.author}: {comment.text}{comment_time}")
                elif comment:
                    comment_time = f"; created_at={comment.created_at}" if comment.created_at else ""
                    self.log_change(checked_at, account, "comment_removed", f"{comment.author}{comment_time}")
                else:
                    self.log_change(checked_at, account, "comment_removed", comment_id)
                events.append(
                    self.format_event_message(
                        account,
                        new,
                        display_timeline,
                        checked_at,
                        "комментарий исчез из профиля",
                        html_details=self.comment_detail_html(comment_id, comment),
                    )
                )

        return events

    async def resolve_friend_infos(self, steam_ids: List[str]) -> List[FriendInfo]:
        summaries = await self.steam.get_player_summaries(steam_ids)
        friends: List[FriendInfo] = []
        for steam_id in steam_ids:
            player = summaries.get(steam_id)
            if player:
                friends.append(FriendInfo(steam_id=steam_id, name=player.get("personaname") or steam_id))
            else:
                friends.append(FriendInfo(steam_id=steam_id, name=steam_id))
        return friends

    async def resolve_friend_names(self, steam_ids: List[str]) -> List[str]:
        friends = await self.resolve_friend_infos(steam_ids)
        return [f"{friend.name} ({friend.steam_id})" for friend in friends]

    def account_title(self, account: MonitoredAccount, snapshot: AccountSnapshot) -> str:
        display_name = snapshot.display_name or account.label
        profile_url = snapshot.profile_url or f"{STEAM_COMMUNITY_BASE}/profiles/{account.steam_id}"
        return (
            f"👤 <b>{html_text(account.label)}</b> / {html_text(display_name)}\n"
            f"🔗 <a href=\"{html_attr(profile_url)}\">Steam profile</a>\n"
            f"🆔 <code>{html_text(account.steam_id)}</code>"
        )

    async def format_initial_status(
        self,
        account: MonitoredAccount,
        snapshot: AccountSnapshot,
        timeline: AccountTimeline,
        checked_at: datetime,
        is_new_account: bool = False,
    ) -> str:
        html_details = await self.baseline_detail_html(snapshot)
        title = "Новый аккаунт добавлен в мониторинг" if is_new_account else "Первый снимок после запуска"
        lines = [
            f"📌 <b>{html_text(title)}</b>",
            f"🕒 <code>{html_text(format_dt(checked_at))}</code>",
            self.account_title(account, snapshot),
            "",
            f"{self.state_emoji(snapshot)} Сейчас: <b>{html_text(self.current_state_text(snapshot))}</b>",
            *self.duration_lines(snapshot, timeline, checked_at),
            *self.format_detail_lines(html_details=html_details),
        ]
        return "\n".join(lines)

    def format_status_report(self) -> str:
        if not self.snapshots:
            return "⏳ <b>Снимков состояния еще нет.</b>\nПодождите первую проверку Steam."

        checked_at = now_local()
        lines = [
            "📊 <b>Статус аккаунтов</b>",
            f"🕒 <code>{html_text(format_dt(checked_at))}</code>",
        ]
        for account in self.config.accounts:
            snapshot = self.snapshots.get(account.steam_id)
            if not snapshot:
                lines.append(f"\n👤 <b>{html_text(account.label)}</b>: еще не проверен")
                continue
            timeline = self.timelines.get(account.steam_id, self.timeline_for_initial_snapshot(snapshot, checked_at))
            block = [
                "",
                self.account_title(account, snapshot),
                f"{self.state_emoji(snapshot)} Сейчас: <b>{html_text(self.current_state_text(snapshot))}</b>",
                *self.duration_lines(snapshot, timeline, checked_at),
            ]
            lines.extend(block)
        return "\n".join(lines)


async def main() -> None:
    config = load_config()

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        telegram_session = AiohttpSession(proxy=config.telegram_proxy, timeout=30)
        bot = Bot(token=config.bot_token, session=telegram_session)
        steam = SteamApiClient(config.steam_api_key, session)
        monitor = SteamProfileMonitor(config, bot, steam)
        dispatcher = Dispatcher()

        @dispatcher.message(Command("start"))
        async def start_command(message: Message) -> None:
            await monitor.send_private(
                message,
                "🟢 <b>Steam Profile Monitor работает</b>\n\n"
                "📊 /status — подробный статус и длительности\n"
                "👥 /accounts — отслеживаемые SteamID\n"
                "🎯 /cs2today — матчи CS2 за сегодня",
            )

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

        monitor_task = asyncio.create_task(monitor.run_forever())
        try:
            retry_delay = 5
            while True:
                try:
                    await dispatcher.start_polling(bot, close_bot_session=False)
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
