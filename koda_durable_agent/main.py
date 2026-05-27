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


def run_update() -> None:
    """Pull latest from GitHub and reinstall the package."""
    import subprocess

    # The repo root is always two levels up from this file
    repo_dir = Path(__file__).resolve().parent.parent

    print(f"\n  Updating Koda from {repo_dir}...\n")

    # Check it's actually a git repo
    if not (repo_dir / ".git").exists():
        print("  ✗  Not a git repo — can't auto-update.")
        print(f"     Re-run: bash {repo_dir}/install.sh")
        return

    # Pull latest (explicit remote/branch so it works without tracking info)
    result = subprocess.run(["git", "pull", "origin", "main"], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ✗  git pull failed:\n{result.stderr.strip()}")
        return

    output = result.stdout.strip()
    if "Already up to date" in output:
        print("  ✓  Already up to date.")
        return

    print(f"  {output}\n")

    # Reinstall so any new deps are picked up
    venv_pip = Path.home() / ".koda" / "venv" / "bin" / "pip"
    pip = str(venv_pip) if venv_pip.exists() else "pip"
    result = subprocess.run(
        [pip, "install", "--quiet", "-e", str(repo_dir)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ✗  pip install failed:\n{result.stderr.strip()}")
        return

    print("  ✓  Koda updated. Restart to apply changes.\n")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "setup":
        from koda_durable_agent.setup_wizard import run_setup
        run_setup()
        return

    if cmd == "update":
        run_update()
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
