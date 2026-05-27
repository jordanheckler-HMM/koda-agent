#!/usr/bin/env bash
# Koda Agent — installer
# Usage: bash install.sh
# Idempotent: safe to run multiple times.

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Koda Agent Installer ==="
echo "Repo: $REPO_DIR"

# ── 1. Check Python >= 3.11 ──────────────────────────────────────────────────
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "ERROR: python3 not found. Install Python 3.11+ and try again."
    exit 1
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "ERROR: Python 3.11+ required. Found Python $PY_VERSION."
    exit 1
fi
echo "Python $PY_VERSION — OK"

# ── 2. Create virtualenv at ~/.koda/venv ─────────────────────────────────────
KODA_DIR="$HOME/.koda"
VENV_DIR="$KODA_DIR/venv"

mkdir -p "$KODA_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Virtualenv already exists at $VENV_DIR — skipping creation."
fi

# ── 3. Install the package ────────────────────────────────────────────────────
echo "Installing koda-agent from $REPO_DIR ..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install -e "$REPO_DIR"
echo "Package installed."

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
echo "Wrapper script written to $WRAPPER_PATH"

# ── 5. Config directory ───────────────────────────────────────────────────────
mkdir -p "$KODA_DIR"
echo "Config directory ready at $KODA_DIR"

# ── 6. Template .env ─────────────────────────────────────────────────────────
ENV_FILE="$KODA_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'EOF'
# Koda Agent — environment configuration
# Fill in your API keys and settings below.

# Required: OpenRouter API key for LLM access
OPENROUTER_API_KEY=your_openrouter_api_key_here

# Optional: Telegram bot for remote access
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# Your name (used in agent prompts)
USER_NAME=
EOF
    echo "Template .env written to $ENV_FILE"
else
    echo ".env already exists at $ENV_FILE — skipping."
fi

# ── 7. Run first-time setup wizard ───────────────────────────────────────────
echo ""
echo "=== Koda installed successfully! ==="
echo ""

if [[ "$WRAPPER_PATH" == "$HOME/.local/bin/koda" ]]; then
    echo "  NOTE: $HOME/.local/bin is your wrapper location."
    echo "  Make sure it's on your PATH. Add this to ~/.zshrc or ~/.bashrc:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    # Add to PATH for this session so we can run koda setup immediately
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Running setup wizard..."
echo ""
"$VENV_DIR/bin/python" -m koda_durable_agent.main setup
echo ""
