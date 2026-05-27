"""
Koda Heartbeat — periodic self-reflection loop.
Runs as a background asyncio task, learns preferences over time.
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

KODA_DIR = Path.home() / ".koda"
HEARTBEAT_LOG = KODA_DIR / "heartbeat.log"
EVOLVE_QUEUE_FILE = KODA_DIR / "koda_evolve_queue.json"
LEARNED_FILE = KODA_DIR / "koda_learned.md"

logger = logging.getLogger("koda.heartbeat")


def load_evolve_queue() -> list:
    if EVOLVE_QUEUE_FILE.exists():
        try:
            return json.loads(EVOLVE_QUEUE_FILE.read_text())
        except Exception:
            pass
    return []


def save_evolve_queue(queue: list) -> None:
    KODA_DIR.mkdir(parents=True, exist_ok=True)
    EVOLVE_QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def queue_evolution_item(item: str) -> None:
    q = load_evolve_queue()
    if item not in q:
        q.append(item)
        save_evolve_queue(q)


def apply_learned_preference(pref: str) -> None:
    KODA_DIR.mkdir(parents=True, exist_ok=True)
    existing = LEARNED_FILE.read_text() if LEARNED_FILE.exists() else ""
    if pref not in existing:
        with open(LEARNED_FILE, "a") as f:
            f.write(f"\n- {pref}")


class HeartbeatRunner:
    def __init__(self, *args, **kwargs):
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def is_running(self) -> bool:
        return False

    def get_pending_notice(self) -> Optional[str]:
        return None
