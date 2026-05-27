# Koda 🐻
<img width="1024" height="416" alt="image" src="https://github.com/user-attachments/assets/d3797499-4b63-497e-ad23-b7832868376a" />

**Your personal AI agent. Runs on your computer. Works anywhere.**

Koda is a terminal-based AI agent that lives on your machine, remembers your preferences, and actually gets things done. Chat with it, schedule automations, and get updates on your phone — all from your own computer.

---

## Install

### macOS

Open Terminal and run:

```bash
bash <(curl -sSL https://raw.githubusercontent.com/jordanheckler-HMM/koda-agent/main/install.sh)
```

### Windows

**Step 1 — Install WSL** (skip if you already have it)

Open **PowerShell** (search "PowerShell" in the Start menu) and run:

```powershell
wsl --install
```

This installs WSL (Windows Subsystem for Linux) — a free Microsoft tool that runs Linux on Windows. Restart your computer when it finishes, then open the **Ubuntu** app from the Start menu.

**Step 2 — Install Koda**

In the Ubuntu window, run:

```bash
bash <(curl -sSL https://raw.githubusercontent.com/jordanheckler-HMM/koda-agent/main/install.sh)
```

The installer handles everything else automatically.

> Every time you want to run Koda on Windows, open the **Ubuntu** app from the Start menu and type `koda`.

---

## What it does

- **Chat** — talk to Koda like a smart assistant that knows you
- **Scheduled automations** — morning briefs, reminders, weekly reviews on a timer
- **Telegram delivery** — get automation results sent to your phone (optional)
- **Full local access** — reads and writes files, runs commands
- **Custom slash commands** — Koda builds its own `/skills` that you can run anytime
- **macOS extras** — reads iMessages, Reminders, Calendar, Notes, and Contacts

---

## Setup

The setup wizard runs automatically after install. You only need two things:

### 1. Your name

Just your first name — Koda uses it in conversations.

### 2. An OpenRouter API key (free)

OpenRouter is how Koda connects to AI models. The free tier is genuinely free — no credit card required.

1. Go to [openrouter.ai/keys](https://openrouter.ai/keys)
2. Sign up (takes about a minute)
3. Click **Create Key**
4. Paste it into the setup wizard when asked

That's it. Koda uses a free model by default so you won't spend anything.

### 3. Telegram (optional)

Telegram lets Koda send automation results to your phone. Skip this during setup — you can add it later.

If you do want it, setup will walk you through the steps.

---

## Running Koda

After install, type `koda` in your terminal to start. On Windows, run it from the Ubuntu app.

```bash
koda
```

To run setup again at any time:

```bash
koda setup
```

---

## Automations

Ask Koda to set up automations in conversation:

> "What automations can I set up?"

Koda will show you a list of built-in options — morning briefs, SMS triage, weekly reviews, and more. You pick what you want. Nothing runs automatically unless you ask for it.

You can also ask for anything custom:

> "Remind me every weekday at 9am to check my email"
> "Send me a summary every Sunday evening"

---

## Custom skills

Koda can build slash commands for itself:

> "Create a /focus skill that helps me pick the one most important thing to work on"

Once created, type `/focus` to run it.

---

## Personalizing Koda

Koda learns your preferences as you use it. If you want to give it explicit instructions, tell it directly in conversation:

> "Always be brief with me"
> "Remember that I'm a morning person"

You can also edit `~/.koda/Soul.md` to change Koda's personality at a deeper level — but this is totally optional.

---

## See Koda's brain in Obsidian (optional)

[Obsidian](https://obsidian.md) is a free note app with a graph view that shows how your notes connect. When you link it to Koda, you get a live visual map of everything Koda knows — your sessions, automations, learned preferences, and CCI progress — all updating in real time as you use it.

**Setup:**

1. Download Obsidian at [obsidian.md](https://obsidian.md) — free, works on Mac and Windows
2. Create a new vault (or use an existing one)
3. Add one line to `~/.koda/.env`:

```env
OBSIDIAN_VAULT=/path/to/your/vault
```

On Mac the path is usually something like `/Users/yourname/Documents/MyVault`.
On Windows (WSL) it looks like `/mnt/c/Users/yourname/Documents/MyVault`.

4. Restart Koda — it will create a `Koda/` folder inside your vault automatically

Open the graph view in Obsidian and you'll see Koda's notes grouped by type: sessions, brain state, automations, and more. It updates every time you use Koda.

---

## Keeping Koda updated

```bash
koda update
```

---

## Requirements

- **macOS** 12+ or **Windows 10/11** with WSL
- A free [OpenRouter](https://openrouter.ai) account (the installer handles everything else)

---

## License

MIT
