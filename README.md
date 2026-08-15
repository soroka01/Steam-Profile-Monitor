# 🎮 Steam Profile Monitor

> Telegram-бот для отслеживания изменений публичных Steam-профилей, игровой активности и CS2 с локальным состоянием и уведомлениями.

🌐 **Язык:** [Русский](README.md) · [English](README_EN.md)

![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-Telegram_bot-26A5E4?logo=telegram&logoColor=white)
![Steam Web API](https://img.shields.io/badge/Steam-Web_API-171A21?logo=steam&logoColor=white)
![MIT License](https://img.shields.io/badge/License-MIT-2EA44F.svg)

## ✨ Обзор

Steam Profile Monitor периодически опрашивает Steam Web API и публичные страницы Steam Community, сравнивает новый снимок профиля с предыдущим и отправляет изменения в Telegram. Состояние и журналы остаются на вашем компьютере, поэтому монитор продолжает работу после перезапуска и не требует отдельной базы данных.

Проект рассчитан на личный мониторинг небольшого списка публичных SteamID64. Он не обходит настройки приватности Steam и не является официальным продуктом Valve или Telegram.

## 🚀 Возможности

- уведомления о входе в сеть и выходе из сети;
- уведомления о запуске игры, смене игры и выходе из игры;
- отслеживание имени и URL профиля;
- текущие persona states Steam в снимках, `/status` и журналах;
- изменения списка друзей;
- новые, удалённые и обновлённые бейджи;
- новые и исчезнувшие комментарии профиля;
- CS2 rich presence: режим, карта, счёт и завершение матча;
- дневная статистика CS2 по победам, поражениям и ничьим;
- периодические напоминания для активного online/game состояния;
- локальное состояние, общий журнал, журнал изменений и отдельные журналы аккаунтов;
- опциональные сведения SteamID.uk: баны, история, watch list и private notes summary.

**Важно:** изменения persona state между `online`, `busy`, `away` и похожими состояниями сейчас фиксируются в состоянии и журналах, но не создают отдельное push-уведомление.

## 🏗️ Как это работает

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

При первом успешном чтении нового аккаунта монитор сохраняет baseline и отправляет его в Telegram. При следующих проверках отправляются только обнаруженные изменения и настроенные напоминания. Если state-файл уже содержит аккаунт, работа продолжается с сохранённого снимка.

## 📋 Требования

- Python 3.14 или новее (рекомендуется актуальный патч 3.14.6);
- pip 26.1.2, setuptools 84.0.0 и wheel 0.48.0 (launcher обновляет их автоматически);
- Telegram bot token от [@BotFather](https://t.me/BotFather);
- Steam Web API key со страницы <https://steamcommunity.com/dev/apikey>;
- SteamID64 публичных аккаунтов;
- Telegram-чат, группа или канал, куда бот может отправлять сообщения.

Основные зависимости:

| Пакет | Назначение |
| --- | --- |
| `aiogram` | Telegram Bot API и команды |
| `aiohttp` | асинхронные запросы к Steam и SteamID.uk |
| `aiohttp-socks` | HTTP/SOCKS proxy для Telegram |

## ⚙️ Установка и запуск

### 1. Клонируйте репозиторий

```bash
git clone https://github.com/soroka01/Steam-Profile-Monitor.git
cd Steam-Profile-Monitor
```

### 2. Создайте конфигурацию

Windows PowerShell:

```powershell
Copy-Item config.ini.example config.ini
```

Linux или macOS:

```bash
cp config.ini.example config.ini
```

Заполните `config.ini` до запуска.

### 3. Запустите на Windows

```bat
start_monitor.bat
```

`start.bat` и `start_monitor.bat` сейчас эквивалентны: они создают локальную `.venv`, устанавливают зависимости и запускают монитор. Для автоматического запуска без интерактивного `pause` используйте прямой запуск Python через планировщик задач или process manager.

### 4. Или запустите вручную

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python SteamProfileMonitor.py
```

Linux или macOS:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python SteamProfileMonitor.py
```

## ⚙️ Конфигурация

### Telegram

```ini
[telegram]
bot_token = YOUR_TELEGRAM_BOT_TOKEN
chat_id = YOUR_TELEGRAM_CHAT_ID
allowed_user_id = YOUR_TELEGRAM_USER_ID
; proxy = socks5://127.0.0.1:1080
```

| Поле | Назначение |
| --- | --- |
| `bot_token` | Токен бота от BotFather |
| `chat_id` | Получатель автоматических уведомлений |
| `allowed_user_id` | Пользователь, которому разрешены команды; настоятельно рекомендуется указать |
| `proxy` | Необязательный HTTP или SOCKS5 proxy для Telegram API |

Placeholder `YOUR_TELEGRAM_USER_ID` считается пустым значением. Если `allowed_user_id` не задан, команды бота не ограничены одним пользователем.

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

| Поле | Назначение |
| --- | --- |
| `api_key` | Steam Web API key |
| `poll_interval_seconds` | Интервал проверки; код применяет минимум 10 секунд |
| `status_reminder_interval_seconds` | Интервал напоминаний для online/game; `0` отключает |
| `persona_state_debounce_seconds` | Сколько новый persona state в online-состоянии без игры должен сохраняться перед принятием в snapshot; отдельное уведомление об этом не отправляется |
| `notify_on_start` | Совместимый ключ конфигурации; в текущей версии не меняет поведение baseline |
| `monitor_comments` | Читать последние комментарии профиля |
| `monitor_friends` | Читать список друзей |
| `monitor_badges` | Читать бейджи и их уровни |
| `monitor_rich_presence` | Читать rich presence активной игры |
| `monitor_cs2` | Разбирать CS2 status, сохранять матчи и включать `/cs2today` |

### Аккаунты

```ini
[accounts]
76561198000000001 = Main
76561198000000002 = Alt
```

Также поддерживаются отдельные секции:

```ini
[account:second]
steam_id = 76561198000000003
label = Another account
```

Одинаковый SteamID64, указанный несколько раз, будет дедуплицирован; последнее описание получит приоритет.

### SteamID.uk — опционально

```ini
[steamid_uk]
enabled = false
api_key = YOUR_STEAMID_UK_API_KEY
myid = YOUR_STEAMID64
refresh_interval_seconds = 21600
sync_watchlist = false
watchlist_id = 1
```

Интеграция включается только при `enabled = true` в `config.ini` и наличии `api_key` и `myid`. Вместо хранения credentials в файле можно использовать:

```text
STEAMID_UK_API_KEY
STEAMID_UK_MYID
```

Переменные окружения только передают credentials и сами по себе не включают интеграцию: в `config.ini` всё равно требуется `enabled = true`.

| Поле | Назначение |
| --- | --- |
| `enabled` | Включить SteamID.uk enrichment |
| `api_key` | API key SteamID.uk |
| `myid` | Ваш SteamID64 для API-запросов |
| `refresh_interval_seconds` | Период обновления; минимум 300 секунд |
| `sync_watchlist` | Добавлять отслеживаемые SteamID в удалённый watch list |
| `watchlist_id` | ID watch list для синхронизации |

`sync_watchlist = true` изменяет ваш watch list в SteamID.uk. Включайте его только намеренно.

## 🤖 Команды Telegram

| Команда | Действие |
| --- | --- |
| `/start` | Краткая справка и проверка доступа |
| `/status` | Текущий статус и длительности по аккаунтам |
| `/accounts` | Список отслеживаемых SteamID64 |
| `/cs2today` | Завершённые матчи CS2 за текущий локальный день |
| `/steamiduk` | Принудительно обновить и показать SteamID.uk report |

`/cs2today` полезен только при `monitor_cs2 = true`. `/steamiduk` сообщает, что интеграция выключена, если credentials не настроены.

## 💾 Локальные данные

| Файл или каталог | Содержимое |
| --- | --- |
| `config.ini` | Токены, API keys и список аккаунтов |
| `steam_monitor_state.json` | Последние snapshots, timelines, CS2 matches и SteamID.uk cache |
| `steam_profile_monitor.log` | Технический журнал и snapshots |
| `steam_profile_changes.log` | История обнаруженных изменений |
| `account_logs/` | Отдельный журнал каждого аккаунта |

Эти пути исключены из Git. Не публикуйте их вместе с архивом проекта или диагностикой.

## 🔐 Безопасность

- Никогда не коммитьте `config.ini`, bot token, Steam API key или SteamID.uk credentials.
- Укажите `allowed_user_id`, особенно если бот имеет публичный username.
- State и logs могут раскрывать историю активности, друзей, комментарии и SteamID.
- После случайной публикации немедленно отзовите и перевыпустите затронутый token или key.
- Перед включением SteamID.uk watchlist sync учитывайте, что это внешнее изменение данных.

## ⚠️ Ограничения

- Закрытые профили и отдельные приватные разделы могут выглядеть пустыми или offline.
- Монитор читает только первые 10 комментариев из ответа Steam Community.
- Rich presence зависит от клиента Steam и может появляться или исчезать с задержкой.
- Завершение CS2-матча определяется эвристически по rich presence, а не официальному match history.
- Persona-state transitions не создают отдельного push-уведомления.
- `notify_on_start` сейчас не управляет baseline: новый аккаунт получает baseline, существующий state продолжается без него.
- Тексты Telegram и runtime logs преимущественно русскоязычные.

## 🧪 Проверка и диагностика

В репозитории пока нет автоматических тестов и CI. Без реальных credentials можно проверить синтаксис и конфигурацию, но полноценная проверка требует доступа к Telegram и Steam.

Если монитор не запускается:

1. Проверьте наличие и синтаксис `config.ini`.
2. Установите зависимости: `python -m pip install -r requirements.txt`.
3. Проверьте bot token, `chat_id` и Steam Web API key.
4. Убедитесь, что SteamID64 начинается с корректного диапазона `765...`.
5. Изучите `steam_profile_monitor.log`.

Если не видны друзья, бейджи, комментарии или игра, сначала проверьте приватность соответствующих разделов Steam.

## 📄 Лицензия

Проект распространяется по лицензии [MIT](LICENSE).

---

Сделано для личного мониторинга с уважением к настройкам приватности Steam и Telegram.
