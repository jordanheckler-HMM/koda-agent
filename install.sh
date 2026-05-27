#!/usr/bin/env bash
# Koda Agent — installer
# Usage: bash install.sh
# Idempotent: safe to run multiple times.

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "╔══════════════════════════════╗"
echo "║   🐻  Koda Agent Installer   ║"
echo "╚══════════════════════════════╝"
echo ""

# ── 1. Ensure Python 3.11+ is available ──────────────────────────────────────
_py_version_ok() {
    local py="$1"
    local ver
    ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || return 1
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }
}

PYTHON=""

# Check common explicit python3.11+ names first
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" &>/dev/null && _py_version_ok "$candidate"; then
        PYTHON=$(command -v "$candidate")
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ not found — installing it now..."
    echo ""

    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS — use Homebrew, installing it first if needed
        if ! command -v brew &>/dev/null; then
            echo "Installing Homebrew (the standard macOS package manager)..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            # Add Homebrew to PATH for this session (Apple Silicon vs Intel)
            if [ -f /opt/homebrew/bin/brew ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -f /usr/local/bin/brew ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
        fi
        echo "Installing Python 3.11..."
        brew install python@3.11
        PYTHON=$(brew --prefix python@3.11)/bin/python3.11
    else
        # Linux / WSL
        echo "Installing Python 3.11 via apt (you may be asked for your password)..."
        sudo apt-get update -qq
        sudo apt-get install -y python3.11 python3.11-venv curl
        PYTHON=$(command -v python3.11)
    fi

    if [ -z "$PYTHON" ] || ! _py_version_ok "$PYTHON"; then
        echo ""
        echo "ERROR: Could not install Python automatically."
        echo "Please install Python 3.11 manually from https://python.org and run this script again."
        exit 1
    fi
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python $PY_VERSION — OK"

# ── 1b. Ensure venv module is available (Linux/WSL only) ─────────────────────
if [[ "$OSTYPE" != "darwin"* ]]; then
    if ! "$PYTHON" -c "import venv" &>/dev/null 2>&1; then
        echo "Installing python${PY_VERSION}-venv..."
        sudo apt-get install -y "python${PY_VERSION}-venv" 2>/dev/null || \
        sudo apt-get install -y python3-venv 2>/dev/null || true
    fi
fi

# ── 2. Create virtualenv at ~/.koda/venv ─────────────────────────────────────
KODA_DIR="$HOME/.koda"
VENV_DIR="$KODA_DIR/venv"

mkdir -p "$KODA_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "Setting up Koda environment..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Koda environment already exists — skipping creation."
fi

# ── 3. Install the package ────────────────────────────────────────────────────
echo "Installing Koda..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install -e "$REPO_DIR"
echo "Done."

# ── 4. Create koda wrapper script ────────────────────────────────────────────
WRAPPER_CONTENT="#!/usr/bin/env bash
source \"$VENV_DIR/bin/activate\"
exec python -m koda_durable_agent.main \"\$@\"
"

# Try /usr/local/bin first, fall back to ~/.local/bin
if [ -w /usr/local/bin ]; then
    WRAPPER_PATH="/usr/local/bin/koda"
else
    mkdir -p "$HOME/.local/bin"
    WRAPPER_PATH="$HOME/.local/bin/koda"
fi

echo "$WRAPPER_CONTENT" > "$WRAPPER_PATH"
chmod +x "$WRAPPER_PATH"

# ── 5. Config directory ───────────────────────────────────────────────────────
mkdir -p "$KODA_DIR"

# ── 6. Template .env ─────────────────────────────────────────────────────────
ENV_FILE="$KODA_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'EOF'
# Koda Agent — configuration
# Fill in your API keys below.

# Required: get a free key at https://openrouter.ai/keys
OPENROUTER_API_KEY=

# Optional: Email inbox triage (Outlook, Gmail, Yahoo, any IMAP)
EMAIL_ADDRESS=
EMAIL_PASSWORD=
# EMAIL_IMAP_SERVER= (auto-detected for Outlook/Gmail/Yahoo/iCloud)

# Optional: Telegram for phone notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional: GitHub token for filing user bug reports (public_repo scope)
GITHUB_TOKEN=

# Your name
USER_NAME=
EOF
fi

# ── 7. PATH notice + run setup wizard ────────────────────────────────────────
echo ""

if [[ "$WRAPPER_PATH" == "$HOME/.local/bin/koda" ]]; then
    # Add to PATH for this session
    export PATH="$HOME/.local/bin:$PATH"

    # Persist to shell rc file
    SHELL_RC=""
    if [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    fi

    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if [ -n "$SHELL_RC" ] && ! grep -qF "$PATH_LINE" "$SHELL_RC"; then
        echo "" >> "$SHELL_RC"
        echo "# Koda" >> "$SHELL_RC"
        echo "$PATH_LINE" >> "$SHELL_RC"
        echo "Added Koda to PATH in $SHELL_RC"
    fi
fi

echo "Running setup..."
echo ""
"$VENV_DIR/bin/python" -m koda_durable_agent.main setup
echo ""
