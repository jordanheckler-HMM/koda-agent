import os
import sys
import logging
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

logger = logging.getLogger("koda.config")

# Load from ~/.koda/.env first, then fall back to any .env in cwd
KODA_DIR = Path.home() / ".koda"
load_dotenv(KODA_DIR / ".env")
load_dotenv()


class Settings:
    BASE_DIR = Path(__file__).resolve().parent
    APP_DATA_DIR = KODA_DIR

    # Session persistence
    SAVE_DIR = str(KODA_DIR / "sessions")
    os.makedirs(SAVE_DIR, exist_ok=True)

    # User identity
    USER_NAME: str = os.getenv("USER_NAME", "")

    # API keys
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

    # GitHub (for filing user-reported issues)
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ALLOWED_USERS: set = set()

    # Sync interval
    KODA_SYNC_INTERVAL_SECONDS: int = int(os.getenv("KODA_SYNC_INTERVAL_SECONDS", "300"))

    # Obsidian vault path (optional — empty means no vault configured)
    OBSIDIAN_VAULT: str = os.getenv("OBSIDIAN_VAULT", "")
    KODA_OBSIDIAN_VAULT: str = os.getenv("OBSIDIAN_VAULT", "")

    # Model config
    PRIMARY_MODEL: str = os.getenv("KODA_MODEL", "openai/gpt-oss-120b:free")

    # Endpoints
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    def __init__(self):
        self._load_env_keys()

    def _load_env_keys(self):
        """Reload env vars so instance attributes reflect current environment."""
        self.USER_NAME = os.getenv("USER_NAME", self.USER_NAME)
        self.OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", self.OPENROUTER_API_KEY)
        self.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", self.GEMINI_API_KEY)
        self.GROQ_API_KEY = os.getenv("GROQ_API_KEY", self.GROQ_API_KEY)
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", self.TELEGRAM_BOT_TOKEN)
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", self.TELEGRAM_CHAT_ID)
        self.PRIMARY_MODEL = os.getenv("KODA_MODEL", self.PRIMARY_MODEL)
        self.OBSIDIAN_VAULT = os.getenv("OBSIDIAN_VAULT", self.OBSIDIAN_VAULT)
        self.KODA_OBSIDIAN_VAULT = self.OBSIDIAN_VAULT

        if self.GEMINI_API_KEY:
            os.environ["GEMINI_API_KEY"] = self.GEMINI_API_KEY


settings = Settings()
