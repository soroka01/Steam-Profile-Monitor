from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from .core import AccountSnapshot, AccountTimeline, BadgeInfo, CS2MatchRecord, CommentInfo, LOCAL_TZ, SteamIdUkProfile


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
        "persona_started_at": dt_to_json(timeline.persona_started_at),
        "game_started_at": dt_to_json(timeline.game_started_at),
        "idle_started_at": dt_to_json(timeline.idle_started_at),
        "offline_started_at": dt_to_json(timeline.offline_started_at),
        "last_cs2_mode": timeline.last_cs2_mode,
        "last_cs2_map": timeline.last_cs2_map,
        "last_cs2_score": timeline.last_cs2_score,
        "last_reminder_at": dt_to_json(timeline.last_reminder_at),
        "last_reminder_key": timeline.last_reminder_key,
        "pending_persona_state": timeline.pending_persona_state,
        "pending_persona_since": dt_to_json(timeline.pending_persona_since),
    }


def timeline_from_json(data: Dict[str, Any], fallback: datetime) -> AccountTimeline:
    return AccountTimeline(
        observed_since=parse_dt(data.get("observed_since")) or fallback,
        online_started_at=parse_dt(data.get("online_started_at")),
        persona_started_at=parse_dt(data.get("persona_started_at")) or parse_dt(data.get("online_started_at")),
        game_started_at=parse_dt(data.get("game_started_at")),
        idle_started_at=parse_dt(data.get("idle_started_at")),
        offline_started_at=parse_dt(data.get("offline_started_at")),
        last_cs2_mode=data.get("last_cs2_mode", ""),
        last_cs2_map=data.get("last_cs2_map", ""),
        last_cs2_score=data.get("last_cs2_score", ""),
        last_reminder_at=parse_dt(data.get("last_reminder_at")),
        last_reminder_key=data.get("last_reminder_key", ""),
        pending_persona_state=int(data["pending_persona_state"])
        if data.get("pending_persona_state") is not None
        else None,
        pending_persona_since=parse_dt(data.get("pending_persona_since")),
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


def steamid_uk_profile_to_json(profile: SteamIdUkProfile) -> Dict[str, Any]:
    return {
        "fetched_at": dt_to_json(profile.fetched_at),
        "profile": profile.profile,
        "profile_bans": profile.profile_bans,
        "private_notes": profile.private_notes,
        "steamid_data": profile.steamid_data,
        "custom_watch_list": profile.custom_watch_list,
        "auth": profile.auth,
    }


def steamid_uk_profile_from_json(data: Dict[str, Any], fallback: datetime) -> SteamIdUkProfile:
    return SteamIdUkProfile(
        fetched_at=parse_dt(data.get("fetched_at")) or fallback,
        profile=data.get("profile", {}) if isinstance(data.get("profile"), dict) else {},
        profile_bans=data.get("profile_bans", {}) if isinstance(data.get("profile_bans"), dict) else {},
        private_notes=data.get("private_notes", {}) if isinstance(data.get("private_notes"), dict) else {},
        steamid_data=data.get("steamid_data", {}) if isinstance(data.get("steamid_data"), dict) else {},
        custom_watch_list=data.get("custom_watch_list", {}) if isinstance(data.get("custom_watch_list"), dict) else {},
        auth=data.get("auth", {}) if isinstance(data.get("auth"), dict) else {},
    )


def steamid_uk_profile_from_api(data: Dict[str, Any], fetched_at: datetime) -> SteamIdUkProfile:
    return SteamIdUkProfile(
        fetched_at=fetched_at,
        profile=data.get("profile", {}) if isinstance(data.get("profile"), dict) else {},
        profile_bans=data.get("profile_bans", {}) if isinstance(data.get("profile_bans"), dict) else {},
        private_notes=data.get("private_notes", {}) if isinstance(data.get("private_notes"), dict) else {},
        steamid_data=data.get("steamid_data", {}) if isinstance(data.get("steamid_data"), dict) else {},
        custom_watch_list=data.get("custom_watch_list", {}) if isinstance(data.get("custom_watch_list"), dict) else {},
        auth=data.get("auth", {}) if isinstance(data.get("auth"), dict) else {},
    )
