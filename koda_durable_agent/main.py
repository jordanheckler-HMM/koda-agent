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


def _find_koda_repo() -> Path:
    """Find the koda-agent repo directory via editable install metadata, falling back to __file__."""
    import json
    # Try importlib.metadata first — works correctly for editable installs
    try:
        import importlib.metadata
        dist = importlib.metadata.distribution("koda-agent")
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            url = data.get("url", "")
            if url.startswith("file://"):
                candidate = Path(url[7:])
                if (candidate / ".git").exists():
                    return candidate
    except Exception:
        pass
    # Fallback: two levels up from this file
    return Path(__file__).resolve().parent.parent


def run_update() -> None:
    """Pull latest from GitHub and reinstall the package."""
    import subprocess

    repo_dir = _find_koda_repo()
    print(f"\n  Updating Koda from {repo_dir}...\n")

    if not (repo_dir / ".git").exists():
        print("  ✗  Not a git repo — can't auto-update.")
        print(f"     Re-run: bash {repo_dir}/install.sh")
        return

    # Check a remote named origin exists
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("  ✗  No git remote 'origin' found in this repo.")
        print("     Open a new terminal tab and try again —")
        print("     your shell may still be using an old alias.")
        return

    result = subprocess.run(
        ["git", "pull", "origin", "main"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  ✗  git pull failed:\n{result.stderr.strip()}")
        return

    output = result.stdout.strip()
    if "Already up to date" in output:
        print("  ✓  Already up to date.")
        return

    print(f"  {output}\n")

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
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        pass
    except Exception as e:
        # Swallow any residual asyncio shutdown noise
        if "KeyboardInterrupt" not in str(type(e).__mro__):
            raise


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        print("\n[Koda] Session ended.")
