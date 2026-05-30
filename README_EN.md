# Steam Profile Monitor

[Русская версия](README.md)

Telegram bot for monitoring public Steam profiles. The bot periodically polls Steam Web API and Steam Community, stores the latest local snapshot, and sends Telegram notifications when a monitored account changes state.

## Features

- Notifications when an account goes online or offline.
- Notifications when an account starts or stops playing a game.
- Steam persona state tracking: online, offline, away, busy, and other states.
- CS2 rich presence tracking:
  - Premier or Competitive mode;
  - map;
  - match score;
  - completed match detection;
  - daily win/loss/draw summary.
- Notifications for added and removed friends.
- Notifications for new and removed badges.
- Notifications for new and removed profile comments.
- Telegram commands for manual status checks.
- Local state persistence between restarts.
- General logs and per-account logs.
- Local system timezone formatting for timestamps.

## How It Works

The bot polls SteamID64 accounts from `config.ini` using the configured interval. On every cycle it builds a fresh profile snapshot, compares it with the previous one, and sends Telegram messages only for detected changes.

The first run creates a baseline snapshot. The bot does not send historical events that already existed before it started.

Some data comes from public Steam Community endpoints. If the profile, friends list, badges, or comments are hidden by privacy settings, the bot cannot read them and will write a warning to the log.

## Requirements

- Python 3.10 or newer.
- Telegram bot token from [@BotFather](https://t.me/BotFather).
- Steam Web API key: <https://steamcommunity.com/dev/apikey>.
- SteamID64 values for the accounts you want to monitor.
- A Telegram chat where the bot can send messages.

Project dependencies:

```text
aiogram
aiohttp
aiohttp-socks
```

## Quick Start

1. Clone the repository:

```bash
git clone https://github.com/soroka01/Steam-Profile-Monitor.git
cd Steam-Profile-Monitor
```

2. Create a virtual environment:

```bash
python -m venv .venv
```

3. Activate the environment.

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Windows cmd:

```bat
.venv\Scripts\activate
```

Linux or macOS:

```bash
source .venv/bin/activate
```

4. Install dependencies:

```bash
pip install -r requirements.txt
```

5. Create a local config:

```bash
cp config.ini.example config.ini
```

Windows cmd:

```bat
copy config.ini.example config.ini
```

6. Fill in `config.ini`.

7. Run the bot:

```bash
python SteamProfileMonitor.py
```

On Windows you can also use:

```bat
start_monitor.bat
```

## Telegram Configuration

Create a bot with [@BotFather](https://t.me/BotFather), then add it to the target chat or channel.

In `config.ini`, fill in:

```ini
[telegram]
bot_token = YOUR_TELEGRAM_BOT_TOKEN
chat_id = YOUR_TELEGRAM_CHAT_ID
allowed_user_id = YOUR_TELEGRAM_USER_ID
```

Fields:

| Field | Description |
| --- | --- |
| `bot_token` | Telegram bot token from BotFather. |
| `chat_id` | Chat, group, or channel ID where notifications will be sent. |
| `allowed_user_id` | Telegram user ID allowed to use private bot commands. |
| `proxy` | Optional HTTP or SOCKS5 proxy for Telegram API access. |

Proxy examples:

```ini
proxy = http://127.0.0.1:8080
proxy = socks5://127.0.0.1:1080
```

## Steam Configuration

Fill the `[steam]` section in `config.ini`:

```ini
[steam]
api_key = YOUR_STEAM_WEB_API_KEY
poll_interval_seconds = 10
status_reminder_interval_seconds = 3600
notify_on_start = true
monitor_comments = true
monitor_friends = true
monitor_badges = true
monitor_rich_presence = true
```

Fields:

| Field | Description |
| --- | --- |
| `api_key` | Steam Web API key. |
| `poll_interval_seconds` | Steam polling interval. The code enforces a minimum of 10 seconds. |
| `status_reminder_interval_seconds` | Interval for intermediate reminders for active online/game states. `0` disables reminders. Offline reminders are not sent. |
| `notify_on_start` | Whether to send baseline status messages on startup. |
| `monitor_comments` | Track profile comments. |
| `monitor_friends` | Track the friends list. |
| `monitor_badges` | Track badges. |
| `monitor_rich_presence` | Track game rich presence, including CS2 details. |

## Monitored Accounts

The simplest format:

```ini
[accounts]
76561198000000001 = Player One
76561198000000002 = Player Two
```

The left side is SteamID64. The right side is the display label used in Telegram messages.

Separate account sections are also supported:

```ini
[account:main]
steam_id = 76561198000000001
label = Main Account
```

## Telegram Commands

| Command | Description |
| --- | --- |
| `/start` | Checks access and shows a short help message. |
| `/status` | Shows the current status of all monitored accounts. |
| `/accounts` | Shows the SteamID64 list currently being monitored. |
| `/cs2today` | Shows completed CS2 matches for the current day. |

Commands are available only to `allowed_user_id` when that field is set.

## Notification Types

Examples of notifications:

- `went online`;
- `went offline`;
- `started playing`;
- `stopped playing`;
- `CS2 status changed`;
- `CS2 match completed`;
- `friends list changed`;
- `new badge received`;
- `new profile comment`;
- `profile comment disappeared`;
- `intermediate status`.

Messages include:

- local event time;
- account label and SteamID64;
- Steam profile link;
- current status;
- observed, online, or game duration;
- event details.

## CS2

For Counter-Strike 2, the bot can parse rich presence values like:

```text
Competitive - Inferno [ 6 : 12 ]
Premier - Mirage [ 13 : 9 ]
```

From this status the bot extracts:

- mode;
- map;
- score;
- match result;
- daily statistics.

A match is treated as completed when the account leaves CS2, moves from a scored match to lobby/in game, or the rich presence no longer contains a score.

Steam may hide rich presence for private profiles or because of privacy settings.

## Local Files

The bot creates local runtime files:

| File or folder | Purpose |
| --- | --- |
| `config.ini` | Your tokens, keys, and account list. |
| `steam_monitor_state.json` | Last saved state snapshot. |
| `steam_profile_monitor.log` | General runtime log. |
| `steam_profile_changes.log` | Change log. |
| `account_logs/` | Per-account logs. |

These files should not be committed to a public repository. They are already included in `.gitignore`.

## Security

Do not publish:

- `config.ini`;
- Telegram bot token;
- Steam Web API key;
- real production logs;
- `steam_monitor_state.json` if it contains private monitoring history.

If a token or API key is accidentally published, revoke and regenerate it immediately.

## Limitations

- Steam Web API and Steam Community may hide data for private profiles.
- Comments, friends, and badges are available only if Steam exposes them publicly or via your API key.
- Rich presence may appear late or disappear depending on the Steam client and privacy settings.
- This bot is not an official Valve or Telegram tool.

## Troubleshooting

If the bot does not start:

1. Make sure the virtual environment exists and is activated.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Check that `config.ini` exists.
4. Check the Telegram token and `chat_id`.
5. Check the Steam Web API key.
6. Read `steam_profile_monitor.log`.

If the bot runs but does not see friends, badges, comments, or game status, check the Steam profile privacy settings.

## Windows Startup

For regular manual startup, use:

```bat
start_monitor.bat
```

The file expects these items next to it:

- `.venv`;
- `config.ini`;
- `SteamProfileMonitor.py`.

For long-running usage, run the bot through Windows Task Scheduler, NSSM, systemd on Linux, or any process manager that can restart a Python script.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
