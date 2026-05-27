"""Runtime helpers for Koda's local TUI and bridge entrypoints."""

from __future__ import annotations

import sys
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
ANTIGRAVITY_VENV = Path.home() / ".gemini" / "antigravity" / "scratch" / "antigravity-project" / "venv"


def bootstrap_pythonpath() -> None:
    """Expose the repo root and Antigravity site-packages to this interpreter."""
    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    lib_dir = ANTIGRAVITY_VENV / "lib"
    if not lib_dir.exists():
        return

    for site_packages in sorted(lib_dir.glob("python*/site-packages"), reverse=True):
        site_packages_str = str(site_packages)
        if site_packages_str not in sys.path:
            sys.path.insert(0, site_packages_str)
        break


def ensure_session_state(session_id: str, save_dir: str | Path, app_data_dir: str | Path) -> Path:
    """Create the files/directories Antigravity expects for a conversation."""
    normalized_session_id = session_id or "default-tui-thread"

    session_dir = Path(save_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    traj_file = session_dir / f"traj-{normalized_session_id}"
    traj_file.touch(exist_ok=True)

    brain_dir = Path(app_data_dir) / "brain" / normalized_session_id
    brain_dir.mkdir(parents=True, exist_ok=True)

    return traj_file
