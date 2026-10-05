from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import asyncio
import html
import json
import re

import aiohttp

from .core import BadgeInfo, CommentInfo, LOCAL_TZ, STEAMID_UK_API_BASE, STEAM_API_BASE, STEAM_COMMUNITY_BASE, STEAM_RETRY_DELAYS, logger
from .helpers import chunks, format_dt, plain_text_from_html, steamid64_to_account_id, trim


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


class SteamIdUkClient:
    def __init__(self, api_key: str, myid: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.myid = myid
        self.session = session
        self.access_warnings: Set[Tuple[Any, ...]] = set()

    def warn_access_once(self, key: Tuple[Any, ...], message: str, *args: Any) -> None:
        if key in self.access_warnings:
            return
        self.access_warnings.add(key)
        logger.warning(message, *args)

    async def _get_json(self, endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{STEAMID_UK_API_BASE}/{endpoint}.php"
        request_params = {
            "myid": self.myid,
            "apikey": self.api_key,
            **params,
        }
        for attempt in range(1, len(STEAM_RETRY_DELAYS) + 2):
            try:
                async with self.session.get(url, params=request_params, timeout=20) as response:
                    if response.status == 200:
                        data = await response.json(content_type=None)
                        auth = data.get("auth", {}) if isinstance(data, dict) else {}
                        if auth.get("auth") == "ok":
                            return data
                        error_id = str(auth.get("error") or auth.get("error_id") or "")
                        self.warn_access_once(
                            ("steamid-uk-auth", endpoint, error_id),
                            "SteamID.uk вернул ошибку авторизации/API для %s: %s",
                            endpoint,
                            auth or data,
                        )
                        return None
                    if response.status not in {429, 500, 502, 503, 504}:
                        logger.warning("SteamID.uk вернул HTTP %s для %s", response.status, endpoint)
                        return None
                    logger.warning("SteamID.uk вернул HTTP %s для %s, попытка %s", response.status, endpoint, attempt)
            except asyncio.TimeoutError:
                logger.warning("Таймаут запроса к SteamID.uk %s, попытка %s", endpoint, attempt)
            except (aiohttp.ClientError, OSError) as exc:
                logger.warning("Ошибка запроса к SteamID.uk %s: %s, попытка %s", endpoint, exc, attempt)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("SteamID.uk вернул не JSON для %s: %s", endpoint, exc)
                return None

            if attempt <= len(STEAM_RETRY_DELAYS):
                await asyncio.sleep(STEAM_RETRY_DELAYS[attempt - 1])
        return None

    async def get_profile(self, steam_id: str) -> Optional[Dict[str, Any]]:
        return await self._get_json("steamid", {"input": steam_id})

    async def add_to_watchlist(self, steam_id: str, watchlist_id: str) -> bool:
        data = await self._get_json(
            "watch",
            {
                "input": steam_id,
                "watchlist": watchlist_id,
                "action": "add",
            },
        )
        return bool(data and str(data.get("added", "")) == steam_id)
