from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import logging


BASE_DIR = Path(__file__).resolve().parent.parent


CONFIG_PATH = BASE_DIR / "config.ini"


LOG_PATH = BASE_DIR / "steam_profile_monitor.log"


CHANGES_LOG_PATH = BASE_DIR / "steam_profile_changes.log"


ACCOUNT_LOG_DIR = BASE_DIR / "account_logs"


STATE_PATH = BASE_DIR / "steam_monitor_state.json"


STEAM_API_BASE = "https://api.steampowered.com"


STEAM_COMMUNITY_BASE = "https://steamcommunity.com"


STEAMID_UK_API_BASE = "https://steamidapi.uk/v2"


STEAMID64_ACCOUNT_ID_BASE = 76561197960265728


CS2_APP_ID = "730"


STEAM_RETRY_DELAYS = (2, 5, 10)


TELEGRAM_SEND_RETRY_DELAYS = (2, 5, 10)


MIN_POLL_INTERVAL_SECONDS = 10


COMMENT_EMPTY_CONFIRMATIONS_REQUIRED = 2


MINIPROFILE_APP_ID_KEYS = ("app_id", "appid", "appId", "game_id", "gameid")


MINIPROFILE_GAME_NAME_KEYS = ("name", "game_name", "gameName", "game", "title")


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
    steamid_uk_enabled: bool
    steamid_uk_api_key: str
    steamid_uk_myid: str
    steamid_uk_refresh_interval_seconds: int
    steamid_uk_sync_watchlist: bool
    steamid_uk_watchlist_id: str
    poll_interval_seconds: int
    status_reminder_interval_seconds: int
    persona_state_debounce_seconds: int
    notify_on_start: bool
    monitor_comments: bool
    monitor_friends: bool
    monitor_badges: bool
    monitor_rich_presence: bool
    monitor_cs2: bool
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
    persona_started_at: Optional[datetime] = None
    game_started_at: Optional[datetime] = None
    idle_started_at: Optional[datetime] = None
    offline_started_at: Optional[datetime] = None
    last_cs2_mode: str = ""
    last_cs2_map: str = ""
    last_cs2_score: str = ""
    last_reminder_at: Optional[datetime] = None
    last_reminder_key: str = ""
    pending_persona_state: Optional[int] = None
    pending_persona_since: Optional[datetime] = None


@dataclass
class CS2MatchRecord:
    completed_at: datetime
    mode: str
    map_name: str
    score: str
    result: str
    rich_presence: str = ""


@dataclass
class SteamIdUkProfile:
    fetched_at: datetime
    profile: Dict[str, Any] = field(default_factory=dict)
    profile_bans: Dict[str, Any] = field(default_factory=dict)
    private_notes: Dict[str, Any] = field(default_factory=dict)
    steamid_data: Dict[str, Any] = field(default_factory=dict)
    custom_watch_list: Dict[str, Any] = field(default_factory=dict)
    auth: Dict[str, Any] = field(default_factory=dict)
