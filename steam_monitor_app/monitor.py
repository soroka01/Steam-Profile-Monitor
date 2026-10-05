from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
import asyncio
import json
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Message

from .clients import SteamApiClient, SteamIdUkClient
from .core import ACCOUNT_LOG_DIR, AccountSnapshot, AccountTimeline, BadgeInfo, COMMENT_EMPTY_CONFIRMATIONS_REQUIRED, CS2MatchRecord, CS2_APP_ID, CommentInfo, FriendInfo, MINIPROFILE_APP_ID_KEYS, MINIPROFILE_GAME_NAME_KEYS, MonitorConfig, MonitoredAccount, PERSONA_STATES, STATE_PATH, STEAM_COMMUNITY_BASE, SteamIdUkProfile, TELEGRAM_SEND_RETRY_DELAYS, change_logger, log_formatter, logger
from .helpers import date_key, first_present, format_dt, format_duration, format_interval, format_optional_count, html_attr, html_text, normalize_game_name, now_local, parse_cs2_rich_presence, result_emoji, result_label, safe_filename, score_result, split_message
from .serialization import dt_to_json, match_from_json, match_to_json, snapshot_from_json, snapshot_to_json, steamid_uk_profile_from_api, steamid_uk_profile_from_json, steamid_uk_profile_to_json, timeline_from_json, timeline_to_json


class SteamProfileMonitor:
    def __init__(
        self,
        config: MonitorConfig,
        bot: Bot,
        steam: SteamApiClient,
        steamid_uk: Optional[SteamIdUkClient] = None,
    ):
        self.config = config
        self.bot = bot
        self.steam = steam
        self.steamid_uk = steamid_uk
        self.snapshots: Dict[str, AccountSnapshot] = {}
        self.timelines: Dict[str, AccountTimeline] = {}
        self.cs2_daily_matches: Dict[str, Dict[str, List[CS2MatchRecord]]] = {}
        self.steamid_uk_profiles: Dict[str, SteamIdUkProfile] = {}
        self.steamid_uk_watchlist_synced: Set[str] = set()
        self.account_loggers: Dict[str, logging.Logger] = {}
        self.account_by_id = {account.steam_id: account for account in config.accounts}
        self.visibility_warnings: Set[str] = set()
        self.comment_empty_observations: Dict[str, int] = {}
        self.load_state()

    def command_list_text(self) -> str:
        commands = ["/status", "/accounts"]
        if self.config.monitor_cs2:
            commands.append("/cs2today")
        commands.append("/steamiduk")
        return ", ".join(commands)

    def start_help_text(self) -> str:
        lines = [
            "🟢 <b>Steam Profile Monitor работает</b>",
            "",
            "📊 /status — подробный статус и длительности",
            "👥 /accounts — отслеживаемые SteamID",
        ]
        if self.config.monitor_cs2:
            lines.append("🎯 /cs2today — матчи CS2 за сегодня")
        lines.append("🧩 /steamiduk — данные SteamID.uk")
        return "\n".join(lines)

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
        if self.steamid_uk and self.config.steamid_uk_sync_watchlist:
            await self.sync_steamid_uk_watchlist()
        await self.send(
            "🟢 <b>Steam Profile Monitor запущен</b>\n"
            f"🕒 <code>{html_text(format_dt(started_at))}</code>\n\n"
            f"👥 Аккаунтов: <b>{len(self.config.accounts)}</b>\n"
            f"🔁 Проверка: <b>{self.config.poll_interval_seconds} сек.</b>\n"
            f"⏰ Напоминания: <b>{html_text(format_interval(self.config.status_reminder_interval_seconds))}</b>"
            f"{' (кроме статуса «не в сети»)' if self.config.status_reminder_interval_seconds > 0 else ''}\n\n"
            f"⏳ Фильтр Steam-статуса: <b>{html_text(format_interval(self.config.persona_state_debounce_seconds))}</b>\n\n"
            f"🎯 CS2 функции: <b>{'включены' if self.config.monitor_cs2 else 'выключены'}</b>\n"
            f"🧩 SteamID.uk: <b>{'включен' if self.steamid_uk else 'выключен'}</b>\n\n"
            f"Команды: {html_text(self.command_list_text())}"
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
            await self.refresh_steamid_uk_profile_if_due(account, checked_at)
            new_snapshot = await self.build_snapshot(account, player)
            if old_snapshot is not None:
                self.suppress_transient_empty_comments(account, old_snapshot, new_snapshot, checked_at)
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
            effective_snapshot = self.debounced_snapshot(account, old_snapshot, new_snapshot, timeline, checked_at)
            events = await self.compare_snapshots(account, old_snapshot, effective_snapshot, timeline, checked_at)
            updated_timeline = self.next_timeline(timeline, old_snapshot, effective_snapshot, checked_at)
            self.timelines[account.steam_id] = updated_timeline
            self.snapshots[account.steam_id] = effective_snapshot

            for event in events:
                await self.send(event)

            reminder = self.maybe_status_reminder(account, effective_snapshot, updated_timeline, checked_at)
            if reminder:
                await self.send(reminder)

            self.save_state()

    async def sync_steamid_uk_watchlist(self) -> None:
        if not self.steamid_uk:
            return
        watchlist_id = self.config.steamid_uk_watchlist_id
        for account in self.config.accounts:
            if account.steam_id in self.steamid_uk_watchlist_synced:
                continue
            added = await self.steamid_uk.add_to_watchlist(account.steam_id, watchlist_id)
            if added:
                self.steamid_uk_watchlist_synced.add(account.steam_id)
                self.log_account(
                    account,
                    logging.INFO,
                    "%s | steamid_uk_watchlist_synced | watchlist_id=%s",
                    format_dt(now_local()),
                    watchlist_id,
                )

    async def refresh_all_steamid_uk_profiles(self, force: bool = False) -> None:
        checked_at = now_local()
        for account in self.config.accounts:
            await self.refresh_steamid_uk_profile_if_due(account, checked_at, force=force)
        self.save_state()

    async def refresh_steamid_uk_profile_if_due(
        self,
        account: MonitoredAccount,
        checked_at: datetime,
        force: bool = False,
    ) -> None:
        if not self.steamid_uk:
            return
        cached = self.steamid_uk_profiles.get(account.steam_id)
        if cached and not force:
            age = (checked_at - cached.fetched_at).total_seconds()
            if age < self.config.steamid_uk_refresh_interval_seconds:
                return

        data = await self.steamid_uk.get_profile(account.steam_id)
        if not data:
            return

        profile = steamid_uk_profile_from_api(data, checked_at)
        self.steamid_uk_profiles[account.steam_id] = profile
        daily_count = profile.auth.get("daily_count", "?")
        daily_limit = profile.auth.get("daily_limit", "?")
        self.log_account(
            account,
            logging.INFO,
            "%s | steamid_uk_refreshed | daily=%s/%s",
            format_dt(checked_at),
            daily_count,
            daily_limit,
        )

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
        steamid_uk_profiles = data.get("steamid_uk_profiles", {})
        steamid_uk_watchlist_synced = data.get("steamid_uk_watchlist_synced", [])

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

        for steam_id, profile_data in steamid_uk_profiles.items():
            if steam_id not in account_ids or not isinstance(profile_data, dict):
                continue
            self.steamid_uk_profiles[steam_id] = steamid_uk_profile_from_json(profile_data, loaded_at)

        if isinstance(steamid_uk_watchlist_synced, list):
            self.steamid_uk_watchlist_synced = {
                str(steam_id)
                for steam_id in steamid_uk_watchlist_synced
                if str(steam_id) in account_ids
            }

        logger.info("Загружено сохраненное состояние: снимков=%s, файл=%s", len(self.snapshots), STATE_PATH)

    def save_state(self) -> None:
        data = {
            "version": 2,
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
            "steamid_uk_profiles": {
                steam_id: steamid_uk_profile_to_json(profile)
                for steam_id, profile in self.steamid_uk_profiles.items()
            },
            "steamid_uk_watchlist_synced": sorted(self.steamid_uk_watchlist_synced),
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
            persona_started_at=checked_at if snapshot.online else None,
            game_started_at=checked_at if snapshot.game_id else None,
            idle_started_at=checked_at if snapshot.online and not snapshot.game_id else None,
            offline_started_at=checked_at if not snapshot.online else None,
            last_cs2_mode=snapshot.cs2_mode,
            last_cs2_map=snapshot.cs2_map,
            last_cs2_score=snapshot.cs2_score,
            last_reminder_key=self.state_key(snapshot),
        )

    def debounced_snapshot(
        self,
        account: MonitoredAccount,
        old: AccountSnapshot,
        new: AccountSnapshot,
        timeline: AccountTimeline,
        checked_at: datetime,
    ) -> AccountSnapshot:
        interval = self.config.persona_state_debounce_seconds
        if interval <= 0:
            timeline.pending_persona_state = None
            timeline.pending_persona_since = None
            return new

        if not old.online or not new.online or old.game_id != new.game_id or old.game_id or old.persona_state == new.persona_state:
            timeline.pending_persona_state = None
            timeline.pending_persona_since = None
            return new

        if timeline.pending_persona_state != new.persona_state:
            timeline.pending_persona_state = new.persona_state
            timeline.pending_persona_since = checked_at
            old_status = PERSONA_STATES.get(old.persona_state, str(old.persona_state))
            new_status = PERSONA_STATES.get(new.persona_state, str(new.persona_state))
            self.log_change(
                checked_at,
                account,
                "persona_state_pending",
                f"{old_status} -> {new_status}; debounce={interval} sec",
            )
            return replace(new, persona_state=old.persona_state)

        pending_since = timeline.pending_persona_since or checked_at
        if (checked_at - pending_since).total_seconds() < interval:
            return replace(new, persona_state=old.persona_state)

        return new

    def next_timeline(
        self,
        timeline: AccountTimeline,
        old: AccountSnapshot,
        new: AccountSnapshot,
        checked_at: datetime,
    ) -> AccountTimeline:
        online_started_at = timeline.online_started_at if old.online and new.online else checked_at if new.online else None
        persona_started_at = timeline.persona_started_at if old.online and new.online and old.persona_state == new.persona_state else checked_at if new.online else None
        game_started_at = timeline.game_started_at if old.game_id and old.game_id == new.game_id else checked_at if new.game_id else None
        idle_started_at = timeline.idle_started_at if old.online and not old.game_id and new.online and not new.game_id else checked_at if new.online and not new.game_id else None
        offline_started_at = timeline.offline_started_at if not old.online and not new.online else checked_at if not new.online else None
        old_key = self.state_key(old)
        new_key = self.state_key(new)
        last_reminder_at = timeline.last_reminder_at if old_key == new_key else None
        if (
            old.online
            and new.online
            and old.persona_state != new.persona_state
            and timeline.pending_persona_state == new.persona_state
            and timeline.pending_persona_since
        ):
            persona_started_at = timeline.pending_persona_since
        pending_persona_state = timeline.pending_persona_state
        pending_persona_since = timeline.pending_persona_since
        if old_key != new_key:
            pending_persona_state = None
            pending_persona_since = None
        last_cs2_mode = timeline.last_cs2_mode
        last_cs2_map = timeline.last_cs2_map
        last_cs2_score = timeline.last_cs2_score
        if self.config.monitor_cs2 and new.game_id == CS2_APP_ID and new.cs2_score:
            last_cs2_mode = new.cs2_mode
            last_cs2_map = new.cs2_map
            last_cs2_score = new.cs2_score
        elif not self.config.monitor_cs2 or (old.game_id != CS2_APP_ID and new.game_id != CS2_APP_ID):
            last_cs2_mode = ""
            last_cs2_map = ""
            last_cs2_score = ""

        return AccountTimeline(
            observed_since=timeline.observed_since,
            online_started_at=online_started_at,
            persona_started_at=persona_started_at,
            game_started_at=game_started_at,
            idle_started_at=idle_started_at,
            offline_started_at=offline_started_at,
            last_cs2_mode=last_cs2_mode,
            last_cs2_map=last_cs2_map,
            last_cs2_score=last_cs2_score,
            last_reminder_at=last_reminder_at,
            last_reminder_key=new_key,
            pending_persona_state=pending_persona_state,
            pending_persona_since=pending_persona_since,
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
        if snapshot.online:
            return timeline.idle_started_at or timeline.persona_started_at or timeline.online_started_at or timeline.observed_since
        return timeline.offline_started_at or timeline.observed_since

    def steam_status_started_at(self, snapshot: AccountSnapshot, timeline: AccountTimeline) -> datetime:
        if snapshot.online:
            return timeline.persona_started_at or timeline.online_started_at or timeline.observed_since
        return timeline.offline_started_at or timeline.observed_since

    def should_send_status_reminder(self, snapshot: AccountSnapshot) -> bool:
        if not snapshot.online:
            return False
        if not snapshot.game_id and snapshot.persona_state in {1, 3}:
            return False
        return True

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

        if not self.should_send_status_reminder(snapshot):
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
            f"cs2={(self.cs2_summary(snapshot) or '-') if self.config.monitor_cs2 else 'disabled'}; "
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

    def suppress_transient_empty_comments(
        self,
        account: MonitoredAccount,
        old: AccountSnapshot,
        new: AccountSnapshot,
        checked_at: datetime,
    ) -> None:
        if new.comments is None:
            self.comment_empty_observations.pop(account.steam_id, None)
            return

        if not old.comments:
            if new.comments:
                self.comment_empty_observations.pop(account.steam_id, None)
            return

        if new.comments:
            self.comment_empty_observations.pop(account.steam_id, None)
            return

        observations = self.comment_empty_observations.get(account.steam_id, 0) + 1
        self.comment_empty_observations[account.steam_id] = observations
        if observations >= COMMENT_EMPTY_CONFIRMATIONS_REQUIRED:
            self.comment_empty_observations.pop(account.steam_id, None)
            self.log_account(
                account,
                logging.WARNING,
                "%s | comments_empty_confirmed | Steam вернул пустой список комментариев %s раза подряд, принимаю удаление. Было=%s",
                format_dt(checked_at),
                observations,
                len(old.comments),
            )
            return

        new.comments = set(old.comments)
        new.known_comments = dict(old.known_comments)
        self.log_account(
            account,
            logging.WARNING,
            "%s | comments_empty_ignored | Steam вернул пустой список комментариев после %s; жду подтверждения перед удалением.",
            format_dt(checked_at),
            len(old.comments),
        )

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
        if not self.config.monitor_cs2:
            return "\n".join(
                [
                    "🎯 <b>CS2 функции отключены</b>",
                    f"🕒 <code>{html_text(format_dt(checked_at))}</code>",
                    "",
                    "Включите <code>monitor_cs2 = true</code> в <code>config.ini</code>, чтобы бот снова разбирал CS2 rich presence и записывал матчи.",
                ]
            )

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
            rich_presence = (
                self.cs2_summary(snapshot)
                if self.config.monitor_cs2 and snapshot.game_id == CS2_APP_ID
                else snapshot.rich_presence
            )
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
            if self.config.monitor_cs2 and snapshot.game_id == CS2_APP_ID and not snapshot.cs2_score:
                last_score = self.last_cs2_score_text(timeline)
                if last_score:
                    lines.append(f"🏁 Последний счёт: <b>{html_text(last_score)}</b>")
            return lines
        if snapshot.online:
            return [
                f"⏳ В статусе Steam: <b>{html_text(format_duration(self.steam_status_started_at(snapshot, timeline), checked_at))}</b>",
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
                "⏰ <b>Промежуточный статус</b>",
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

    def badge_level_detail_html(self, badge_key: str, old_badge: BadgeInfo, new_badge: BadgeInfo) -> str:
        name = new_badge.name or old_badge.name or badge_key
        old_level = "нет" if old_badge.level is None else str(old_badge.level)
        new_level = "нет" if new_badge.level is None else str(new_badge.level)
        level_label = "игр приобретено" if badge_key.startswith("13:") else "уровень"
        parts = [
            f"🏅 <b>{html_text(name)}</b>",
            f"{html_text(level_label)} <b>{html_text(old_level)}</b> → <b>{html_text(new_level)}</b>",
        ]
        app_id = new_badge.app_id if new_badge.app_id is not None else old_badge.app_id
        if app_id is not None:
            parts.append(f'<a href="https://store.steampowered.com/app/{app_id}/">app {app_id}</a>')
        parts.append(f"key <code>{html_text(badge_key)}</code>")
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

    def steamid_uk_flag(self, value: Any) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    def steamid_uk_value(self, value: Any, fallback: str = "0") -> str:
        value = str(value).strip() if value is not None else ""
        return value if value else fallback

    def steamid_uk_summary_html(self, account: MonitoredAccount, compact: bool = False) -> List[str]:
        if not self.steamid_uk:
            return []

        profile = self.steamid_uk_profiles.get(account.steam_id)
        if not profile:
            return ["🧩 SteamID.uk: <b>нет данных в кэше</b>"]

        bans = profile.profile_bans
        data = profile.steamid_data
        watch = profile.custom_watch_list
        notes = profile.private_notes
        fetched_age = format_duration(profile.fetched_at, now_local())

        lines = [
            f"🧩 SteamID.uk: <b>обновлено {html_text(fetched_age)} назад</b>",
            "🚫 Баны: "
            f"VAC <b>{html_text(self.steamid_uk_value(bans.get('vac')))}</b> · "
            f"Game <b>{html_text(self.steamid_uk_value(bans.get('amount_game_bans')))}</b> · "
            f"Trade <b>{html_text(self.steamid_uk_value(bans.get('tradeban')))}</b> · "
            f"Community <b>{html_text(self.steamid_uk_value(bans.get('communityban')))}</b>",
            "⚠️ Друзья с банами: "
            f"VAC <b>{html_text(self.steamid_uk_value(data.get('vac_banned_friends')))}</b> · "
            f"Game <b>{html_text(self.steamid_uk_value(data.get('game_banned_friends')))}</b> · "
            f"Trade <b>{html_text(self.steamid_uk_value(data.get('trade_banned_friends')))}</b> · "
            f"Community <b>{html_text(self.steamid_uk_value(data.get('community_banned_friends')))}</b>",
        ]

        if compact:
            return lines

        lines.append(
            "📚 История: "
            f"друзей <b>{html_text(self.steamid_uk_value(data.get('friend_history_count')))}</b> · "
            f"имён <b>{html_text(self.steamid_uk_value(data.get('name_history_count')))}</b> · "
            f"URL <b>{html_text(self.steamid_uk_value(data.get('url_changes')))}</b>"
        )
        friend_count = self.steamid_uk_value(data.get("friend_count"), "")
        if friend_count:
            lines.append(f"👥 Друзей по базе SteamID.uk: <b>{html_text(friend_count)}</b>")
        if self.steamid_uk_flag(data.get("steamid_optout")):
            lines.append("🔒 SteamID.uk opt-out: <b>да</b>")
        if self.steamid_uk_flag(bans.get("rusthackreport")):
            days = self.steamid_uk_value(bans.get("rusthackreport_days_old"), "?")
            url = bans.get("rusthackreport_url")
            if url:
                lines.append(f'⚠️ RustHackReport: <a href="{html_attr(url)}">есть запись</a>, {html_text(days)} дн. назад')
            else:
                lines.append(f"⚠️ RustHackReport: <b>есть запись</b>, {html_text(days)} дн. назад")
        if self.steamid_uk_flag(watch.get("watch_result")):
            category = watch.get("category") or watch.get("id") or "watch list"
            lines.append(f"👁 Watch list: <b>{html_text(category)}</b>")
        note_count = self.steamid_uk_value(notes.get("private_note_count"), "")
        if note_count:
            lines.append(f"📝 Private notes: <b>{html_text(note_count)}</b>")
        return lines

    def format_steamid_uk_report(self) -> str:
        if not self.steamid_uk:
            return "🧩 <b>SteamID.uk выключен.</b>\nДобавьте секцию <code>[steamid_uk]</code> в config.ini."

        lines = [
            "🧩 <b>SteamID.uk</b>",
            f"🔁 Автообновление: <b>{html_text(format_interval(self.config.steamid_uk_refresh_interval_seconds))}</b>",
        ]
        for account in self.config.accounts:
            snapshot = self.snapshots.get(account.steam_id)
            display_name = snapshot.display_name if snapshot else account.label
            profile_url = snapshot.profile_url if snapshot else f"{STEAM_COMMUNITY_BASE}/profiles/{account.steam_id}"
            lines.extend(
                [
                    "",
                    f'👤 <a href="{html_attr(profile_url)}">{html_text(account.label)}</a> / {html_text(display_name)}',
                    f"🆔 <code>{html_text(account.steam_id)}</code>",
                    *self.steamid_uk_summary_html(account, compact=False),
                ]
            )
        return "\n".join(lines)

    async def baseline_detail_html(self, account: MonitoredAccount, snapshot: AccountSnapshot) -> List[str]:
        details = [
            f"👥 Друзья: <b>{html_text(format_optional_count(snapshot.friends))}</b>",
            f"🏅 Бейджи: <b>{html_text(format_optional_count(snapshot.badges))}</b>",
            f"💬 Комментарии из ответа Steam: <b>{html_text(format_optional_count(snapshot.comments))}</b>",
            *self.steamid_uk_summary_html(account, compact=False),
        ]

        if snapshot.friends:
            friends = await self.resolve_friend_infos(sorted(snapshot.friends))
            details.append("<b>Друзья</b>")
            details.extend(self.friend_detail_html(friend) for friend in friends)

        if snapshot.badges:
            details.append("<b>Бейджи</b>")
            details.extend(self.badge_detail_html(badge_key, snapshot.known_badges) for badge_key in sorted(snapshot.badges))

        if self.config.monitor_cs2 and snapshot.game_id == CS2_APP_ID and snapshot.rich_presence:
            details.append(f"🎯 CS2 rich presence: <b>{html_text(self.cs2_summary(snapshot) or snapshot.rich_presence)}</b>")

        if snapshot.comments:
            details.append("<b>Последние комментарии</b>")
            for comment_id in sorted(snapshot.comments):
                details.extend(self.comment_detail_html(comment_id, snapshot.known_comments.get(comment_id)))

        return details

    def miniprofile_matches_current_game(self, snapshot: AccountSnapshot, in_game: Any) -> bool:
        if not snapshot.game_id or not isinstance(in_game, dict):
            return False

        app_id = first_present(in_game, MINIPROFILE_APP_ID_KEYS)
        if app_id is not None and str(app_id).strip() == snapshot.game_id:
            return True

        miniprofile_name = first_present(in_game, MINIPROFILE_GAME_NAME_KEYS)
        if miniprofile_name and snapshot.game_name:
            return normalize_game_name(miniprofile_name) == normalize_game_name(snapshot.game_name)

        return snapshot.game_id == CS2_APP_ID

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
            if rich_presence and self.miniprofile_matches_current_game(snapshot, in_game):
                snapshot.rich_presence = rich_presence
            if self.config.monitor_cs2 and snapshot.game_id == CS2_APP_ID and snapshot.rich_presence:
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
            self.config.monitor_cs2
            and old.game_id == CS2_APP_ID
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
            self.log_change(checked_at, account, "persona_state_changed", f"{old_status} -> {new_status}")

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

        if self.config.monitor_cs2 and old.game_id == CS2_APP_ID and new.game_id == CS2_APP_ID and old.rich_presence != new.rich_presence:
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
            if self.config.monitor_cs2 and old.game_id == CS2_APP_ID:
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
            if self.config.monitor_cs2 and new.game_id == CS2_APP_ID and new.rich_presence:
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

            unchanged_badges = sorted(old.badges & new.badges)
            for badge_key in unchanged_badges:
                old_badge = old.known_badges.get(badge_key)
                new_badge = new.known_badges.get(badge_key)
                if not old_badge or not new_badge or old_badge.level == new_badge.level:
                    continue

                badge_name = new_badge.name or old_badge.name or badge_key
                level_label = "игр приобретено" if badge_key.startswith("13:") else "уровень"
                self.log_change(
                    checked_at,
                    account,
                    "badge_level_changed",
                    f"{badge_name}; {level_label}: {old_badge.level} -> {new_badge.level}",
                )
                events.append(
                    self.format_event_message(
                        account,
                        new,
                        display_timeline,
                        checked_at,
                        "обновился бейдж",
                        html_details=[self.badge_level_detail_html(badge_key, old_badge, new_badge)],
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
        html_details = await self.baseline_detail_html(account, snapshot)
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
                *self.steamid_uk_summary_html(account, compact=True),
            ]
            lines.extend(block)
        return "\n".join(lines)
