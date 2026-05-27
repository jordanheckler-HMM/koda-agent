"""
Koda Sleep Cycle — nightly deep reflection loop (stub).
Full implementation activates when google-antigravity is installed.
"""
import asyncio
from pathlib import Path
from typing import Optional

KODA_DIR = Path.home() / ".koda"
DREAMS_PATH = KODA_DIR / "koda_dreams.md"


class SleepCycleRunner:
    def __init__(self, *args, **kwargs):
        self._task: Optional[asyncio.Task] = None
        self._sleep_count: int = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def is_running(self) -> bool:
        return False

    def next_sleep_eta(self) -> str:
        return "—"

    def last_sleep(self) -> Optional[str]:
        return None

    def get_pending_notice(self) -> Optional[str]:
        return None
