# 🎮 Steam Profile Monitor

> A Telegram bot that tracks changes to public Steam profiles, gaming activity, and CS2 with local state and notifications.

🌐 **Language:** [Русский](README.md) · [English](README_EN.md)

![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-Telegram_bot-26A5E4?logo=telegram&logoColor=white)
![Steam Web API](https://img.shields.io/badge/Steam-Web_API-171A21?logo=steam&logoColor=white)
![MIT License](https://img.shields.io/badge/License-MIT-2EA44F.svg)

## ✨ Overview

Steam Profile Monitor periodically polls the Steam Web API and public Steam Community pages, compares each new profile snapshot with the previous one, and sends changes to Telegram. State and logs stay on your computer, so monitoring survives restarts without a separate database.

The project is intended for personal monitoring of a small list of public SteamID64 accounts. It does not bypass Steam privacy settings and is not an official Valve or Telegram product.

## 🚀 Features

- online and offline notifications;
- game start, game change, and game exit notifications;
- display name and profile URL tracking;
- current Steam persona states in snapshots, `/status`, and logs;
- friends list changes;
- added, removed, and updated badges;
- new and disappearing profile comments;
- CS2 rich presence parsing for mode, map, score, and match completion;
- daily CS2 win, loss, and draw statistics;
- periodic reminders for active online or in-game states;
- local state, a general log, a change log, and per-account logs;
- optional SteamID.uk data for bans, history, watch lists, and private-note summaries.

**Important:** transitions between `online`, `busy`, `away`, and similar persona states are currently recorded in state and logs but do not produce a dedicated push notification.

## 🏗️ How It Works

```text
config.ini
    │
    ▼
SteamProfileMonitor.py
    ├── Steam Web API ─────── profile, game, friends, badges
    ├── Steam Community ───── rich presence, comments
    └── SteamID.uk (optional) bans, history, watch list
    │
    ▼
snapshot + diff
    ├── Telegram notifications and commands
    ├── steam_monitor_state.json
    └── runtime and per-account logs
```

The first successful read of a new account stores and sends a baseline. Later checks send only detected changes and configured reminders. If the state file already contains the account, monitoring continues from the saved snapshot.

## 📋 Requirements

- Python 3.14 or newer (the latest 3.14.6 patch is recommended);
- pip 26.1.2, setuptools 84.0.0, and wheel 0.48.0 (the launchers upgrade them automatically);
- a Telegram bot token from [@BotFather](https://t.me/BotFather);
- a Steam Web API key from <https://steamcommunity.com/dev/apikey>;
- SteamID64 values for public accounts;
- a Telegram chat, group, or channel where the bot can send messages.

Main dependencies:

| Package | Purpose |
| --- | --- |
| `aiogram` | Telegram Bot API and commands |
| `aiohttp` | Asynchronous Steam and SteamID.uk requests |
| `aiohttp-socks` | HTTP/SOCKS proxy support for Telegram |

## ⚙️ Installation and Running

### 1. Clone the repository

```bash
git clone https://github.com/soroka01/Steam-Profile-Monitor.git
cd Steam-Profile-Monitor
```

### 2. Create the configuration

Windows PowerShell:

```powershell
Copy-Item config.ini.example config.ini
```

Linux or macOS:

```bash
cp config.ini.example config.ini
```

Fill in `config.ini` before starting the monitor.

### 3. Run on Windows

```bat
start_monitor.bat
```

`start.bat` and `start_monitor.bat` are currently equivalent. They create a local `.venv`, install dependencies, and start the monitor. For unattended startup without an interactive `pause`, run Python directly through Task Scheduler or another process manager.

### 4. Or run manually

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python SteamProfileMonitor.py
```

Linux or macOS:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python SteamProfileMonitor.py
```

## ⚙️ Configuration

### Telegram

```ini
[telegram]
bot_token = YOUR_TELEGRAM_BOT_TOKEN
chat_id = YOUR_TELEGRAM_CHAT_ID
allowed_user_id = YOUR_TELEGRAM_USER_ID
; proxy = socks5://127.0.0.1:1080
```

| Field | Purpose |
| --- | --- |
| `bot_token` | Bot token from BotFather |
| `chat_id` | Recipient of automatic notifications |
| `allowed_user_id` | User allowed to run commands; strongly recommended |
| `proxy` | Optional HTTP or SOCKS5 proxy for the Telegram API |

The `YOUR_TELEGRAM_USER_ID` placeholder is treated as an empty value. If `allowed_user_id` is not set, bot commands are not restricted to one user.

### Steam

```ini
[steam]
api_key = YOUR_STEAM_WEB_API_KEY
poll_interval_seconds = 10
status_reminder_interval_seconds = 3600
persona_state_debounce_seconds = 120
notify_on_start = true
monitor_comments = true
monitor_friends = true
monitor_badges = true
monitor_rich_presence = true
monitor_cs2 = true
```

| Field | Purpose |
| --- | --- |
| `api_key` | Steam Web API key |
| `poll_interval_seconds` | Polling interval; the code enforces a 10-second minimum |
| `status_reminder_interval_seconds` | Reminder interval for online/game states; `0` disables reminders |
| `persona_state_debounce_seconds` | How long a new persona state while online without a game must persist before being accepted into the snapshot; it does not send a dedicated alert |
| `notify_on_start` | Compatibility configuration key; it currently does not change baseline behavior |
| `monitor_comments` | Read recent profile comments |
| `monitor_friends` | Read the friends list |
| `monitor_badges` | Read badges and levels |
| `monitor_rich_presence` | Read active-game rich presence |
| `monitor_cs2` | Parse CS2 status, record matches, and enable `/cs2today` |

### Accounts

```ini
[accounts]
76561198000000001 = Main
76561198000000002 = Alt
```

Separate sections are also supported:

```ini
[account:second]
steam_id = 76561198000000003
label = Another account
```

Duplicate SteamID64 values are deduplicated, with the last description taking precedence.

### SteamID.uk — Optional

```ini
[steamid_uk]
enabled = false
api_key = YOUR_STEAMID_UK_API_KEY
myid = YOUR_STEAMID64
refresh_interval_seconds = 21600
sync_watchlist = false
watchlist_id = 1
```

The integration is enabled only when `enabled = true` is set in `config.ini` and both `api_key` and `myid` are available. Instead of storing the credentials in the file, you can use:

```text
STEAMID_UK_API_KEY
STEAMID_UK_MYID
```

Environment variables only supply the credentials and do not enable the integration by themselves: `config.ini` still needs `enabled = true`.

| Field | Purpose |
| --- | --- |
| `enabled` | Enable SteamID.uk enrichment |
| `api_key` | SteamID.uk API key |
| `myid` | Your SteamID64 for API requests |
| `refresh_interval_seconds` | Refresh period; minimum 300 seconds |
| `sync_watchlist` | Add monitored SteamIDs to a remote watch list |
| `watchlist_id` | Watch list ID used for synchronization |

`sync_watchlist = true` changes your SteamID.uk watch list. Enable it only intentionally.

## 🤖 Telegram Commands

| Command | Action |
| --- | --- |
| `/start` | Short help and access check |
| `/status` | Current status and durations for all accounts |
| `/accounts` | List monitored SteamID64 values |
| `/cs2today` | Completed CS2 matches for the current local day |
| `/steamiduk` | Force-refresh and display the SteamID.uk report |

`/cs2today` is useful only with `monitor_cs2 = true`. `/steamiduk` reports that the integration is disabled when credentials are not configured.

## 💾 Local Data

| File or directory | Contents |
| --- | --- |
| `config.ini` | Tokens, API keys, and the account list |
| `steam_monitor_state.json` | Latest snapshots, timelines, CS2 matches, and SteamID.uk cache |
| `steam_profile_monitor.log` | Technical runtime log and snapshots |
| `steam_profile_changes.log` | Detected change history |
| `account_logs/` | A separate log for each account |

These paths are excluded from Git. Do not publish them in project archives or diagnostic reports.

## 🔐 Security

- Never commit `config.ini`, bot tokens, Steam API keys, or SteamID.uk credentials.
- Set `allowed_user_id`, especially when the bot has a public username.
- State and logs may reveal activity history, friends, comments, and SteamIDs.
- Revoke and regenerate any token or key that is accidentally published.
- Remember that SteamID.uk watch-list synchronization changes remote data.

## ⚠️ Limitations

- Private profiles and separately hidden sections may appear empty or offline.
- The monitor reads only the first 10 comments returned by Steam Community.
- Rich presence depends on the Steam client and may appear or disappear late.
- CS2 match completion is inferred from rich presence rather than official match history.
- Persona-state transitions do not generate a dedicated push notification.
- `notify_on_start` currently does not control baselines: new accounts receive one, while existing state continues without one.
- Telegram messages and runtime logs are primarily in Russian.

## 🧪 Testing and Troubleshooting

The repository currently has no automated tests or CI. Syntax and configuration can be checked without credentials, but end-to-end verification requires live Telegram and Steam access.

If the monitor does not start:

1. Check that `config.ini` exists and is valid.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Verify the bot token, `chat_id`, and Steam Web API key.
4. Make sure SteamID64 examples use the valid `765...` range.
5. Read `steam_profile_monitor.log`.

If friends, badges, comments, or game activity are missing, check the corresponding Steam privacy settings first.

## 📄 License

This project is distributed under the [MIT License](LICENSE).

---

Built for personal monitoring with respect for Steam and Telegram privacy settings.
