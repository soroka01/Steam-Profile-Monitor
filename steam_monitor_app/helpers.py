from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import html
import re

from .core import LOCAL_TZ, STEAMID64_ACCOUNT_ID_BASE


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


def normalize_game_name(value: Any) -> str:
    value = html.unescape(str(value or "")).casefold()
    value = re.sub(r"[\u2122\u00ae\u00a9]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def first_present(data: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


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
