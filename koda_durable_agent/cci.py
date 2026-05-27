"""
CCI — Continuous Capability Improvement tracker.
Tracks XP earned through tool use and conversations.
"""
import json
from pathlib import Path

KODA_DIR = Path.home() / ".koda"
CCI_FILE = KODA_DIR / "koda_cci.json"

_TIERS = [
    (0,     "Cub"),
    (500,   "Scout"),
    (2000,  "Ranger"),
    (5000,  "Tracker"),
    (10000, "Guardian"),
]


class CCITracker:
    def __init__(self):
        self._data = self._load()

    def _load(self) -> dict:
        if CCI_FILE.exists():
            try:
                return json.loads(CCI_FILE.read_text())
            except Exception:
                pass
        return {"xp": 0, "sessions": 0, "tool_calls": 0, "turns": 0}

    def _save(self) -> None:
        KODA_DIR.mkdir(parents=True, exist_ok=True)
        CCI_FILE.write_text(json.dumps(self._data, indent=2))

    @property
    def xp(self) -> int:
        return self._data.get("xp", 0)

    def add_xp(self, amount: int, reason: str = "") -> None:
        self._data["xp"] = self.xp + amount
        self._save()

    def record_turn(self, tool_calls: int = 0) -> None:
        self._data["turns"] = self._data.get("turns", 0) + 1
        self._data["tool_calls"] = self._data.get("tool_calls", 0) + tool_calls
        self.add_xp(10 + tool_calls * 5)

    def record_session_start(self) -> None:
        self._data["sessions"] = self._data.get("sessions", 0) + 1
        self._save()

    def tier_name(self) -> str:
        xp = self.xp
        name = _TIERS[0][1]
        for threshold, tier in _TIERS:
            if xp >= threshold:
                name = tier
        return name

    def summary(self) -> str:
        return (
            f"Tier: {self.tier_name()} | XP: {self.xp} | "
            f"Sessions: {self._data.get('sessions', 0)} | "
            f"Turns: {self._data.get('turns', 0)} | "
            f"Tool calls: {self._data.get('tool_calls', 0)}"
        )
