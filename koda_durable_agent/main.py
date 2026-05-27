import asyncio
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from koda_durable_agent.runtime import bootstrap_pythonpath

bootstrap_pythonpath()

from koda_durable_agent.config import settings

# ── Logging setup ──────────────────────────────────────────────────────────────
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

KODA_DIR = Path.home() / ".koda"
KODA_DIR.mkdir(parents=True, exist_ok=True)
log_file = KODA_DIR / "koda.log"
try:
    file_handler = logging.FileHandler(str(log_file), mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.root.addHandler(file_handler)
except Exception:
    pass

logging.root.setLevel(logging.WARNING)

def _mock_basic_config(*args, **kwargs):
    pass
logging.basicConfig = _mock_basic_config

for _name in ["websockets", "urllib3", "httpx", "httpcore", "h11", "koda"]:
    _l = logging.getLogger(_name)
    _l.setLevel(logging.WARNING)
    _l.propagate = False

logger = logging.getLogger("koda.main")


def render_banner() -> None:
    print("""
  ======================================================
       ___      ___  ____  ____  _   __
      / _ \\    / _ \\|  _ \\|  _ \\| | / /
     | | | |  | | | | |_) | |_) | |/ /
     | |_| |  | |_| |  _ <|  _ <|   /
      \\___/    \\___/|_| \\_\\_| \\_\\_|/_/

   -- Koda 🐻 : Your Personal AI Agent --
  ======================================================
""")


def main() -> None:
    # ── koda setup ────────────────────────────────────────────────────────────
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from koda_durable_agent.setup_wizard import run_setup
        run_setup()
        return

    # ── Normal TUI boot ───────────────────────────────────────────────────────
    from koda_durable_agent.tui import KodaTUISession
    session = KodaTUISession()
    try:
        asyncio.run(session.start_loop())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        print("\n[Koda] Session ended.")
