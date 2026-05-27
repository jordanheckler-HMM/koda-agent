# Koda 🐻

**Your personal AI agent. Runs locally. Works everywhere.**

Koda is a terminal-based personal AI agent that lives on your machine and actually gets things done. It has a real-time TUI, a built-in cron scheduler, Telegram delivery, and full filesystem and shell access. On macOS it also reads your iMessages, calendar, reminders, notes, and contacts.

---

## What Koda does

- **Chat with context** — Koda knows your name, preferences, and prior instructions
- **Schedule automations** — morning briefs, SMS triage, anything on a cron schedule
- **Telegram delivery** — get cron results on your phone
- **Full local access** — reads/writes files and runs shell commands
- **Custom slash commands** — Koda can create its own `/skills` you define in conversation
- **macOS native tools** — iMessages, Reminders, Calendar, Notes, Contacts (macOS only)
- **Cross-platform core** — all features except Apple tools work on Linux and Windows

---

## Install

```bash
bash <(curl -sSL https://raw.githubusercontent.com/jheckler/koda-agent/main/install.sh)
```

Or clone and run locally:

```bash
git clone https://github.com/jheckler/koda-agent
cd koda-agent
bash install.sh
```

The installer will:
1. Create a virtualenv at `~/.koda/venv`
2. Install the package
3. Write a `koda` wrapper to `/usr/local/bin/koda`
4. Run the setup wizard

---

## Setup

The setup wizard runs automatically after install. To run it manually:

```bash
koda setup
```

It walks you through:
- Your name (used in prompts and briefs)
- OpenRouter API key — get one free at [openrouter.ai/keys](https://openrouter.ai/keys)
- Telegram bot token and chat ID (optional but recommended for phone delivery)

Config is saved to `~/.koda/.env`.

---

## Configuration

`~/.koda/.env`:

```env
# Required
OPENROUTER_API_KEY=sk-or-...

# Optional: Telegram for phone delivery
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Your name
USER_NAME=Alex

# Override the default model (default: openai/gpt-oss-120b:free)
KODA_MODEL=openai/gpt-oss-120b:free
```

---

## Customize Koda's personality

Edit `~/.koda/Soul.md` to change how Koda talks, what it prioritizes, and how it operates. It's loaded as Koda's system prompt every session.

You can also create `~/.koda/UserProfile.md` for personal rules and preferences Koda should always follow.

---

## Scheduler

Koda has a built-in cron scheduler. You can manage it in conversation:

> "Schedule a daily brief every morning at 8am and send it to Telegram"
> "Show me all my scheduled jobs"
> "Run the Morning Brief now"

Schedule formats:
- `daily@08:00` — every day at 8am
- `weekly@mon@09:00` — every Monday at 9am
- `60` — every 60 minutes

Delivery options: `telegram`, `chat`, `background`

---

## Custom skills

Koda can create slash commands for itself:

> "Create a /focus skill that helps me pick the one most important thing to work on right now"

Once created, type `/focus` in the TUI to run it.

---

## Default automations

The setup wizard creates two default cron jobs:

| Job | Schedule | What it does |
|-----|----------|-------------|
| Morning Brief | daily@08:00 | Date, day of week, one motivating thought |
| SMS Triage *(macOS only)* | daily@09:00 | Scans iMessages, flags what needs a reply |

---

## Tech

- **LLMs via OpenRouter** — any model available on OpenRouter works
- **TUI** — [Rich](https://github.com/Textualize/rich) with asyncio, raw mode input, live streaming
- **Telegram** — `python-telegram-bot` style polling via httpx
- **macOS tools** — AppleScript via `osascript` subprocess calls
- **Optional Gemini path** — `pip install koda-agent[gemini]` for Google Antigravity SDK

---

## Requirements

- Python 3.11+
- An [OpenRouter](https://openrouter.ai) API key (free tier works)
- macOS, Linux, or Windows (WSL recommended on Windows)

---

## License

MIT
