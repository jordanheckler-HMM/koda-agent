"""
CCI — Continuous Capability Improvement tracker.
Tracks quality-weighted capability growth across sessions.

Design:
- Normal interactions (turns, tools) are capped per day so quantity alone
  can't inflate the score.
- Sleep cycle runs nightly, grades the day's interactions, and applies a
  quality bonus or penalty. Great days earn more. Poor days earn less.
- Aha moments and explicit ratings bypass the daily cap.
"""
import json
from datetime import date as _date
from pathlib import Path
from typing import Tuple

KODA_DIR = Path.home() / ".koda"
CCI_FILE = KODA_DIR / "koda_cci.json"

# Max CCI gainable per day through normal interaction.
# Sleep cycle quality review grants additional points beyond this.
_DAILY_INTERACTION_CAP = 0.5

_TIERS = [
    ("Cub",      "🐣", "#a8d8a8", 0.0),
    ("Scout",    "🐾", "#7ec8a0", 1.0),
    ("Ranger",   "🌲", "#4caf82", 2.5),
    ("Tracker",  "🦅", "#2196a0", 5.0),
    ("Guardian", "🐻", "#9c27b0", 10.0),
]


class CCITracker:
    DELTA_TELEGRAM_UP = 0.05
    DELTA_TURN = 0.05
    DELTA_TOOL = 0.02
    DELTA_ERROR = -0.01
    DELTA_AHA = 0.3
    DELTA_AHA_JORDAN = 0.3  # alias kept for compatibility
    SOUL_VER = KODA_DIR / "soul_versions"

    def __init__(self):
        self._data = self._load()

    def _load(self) -> dict:
        if CCI_FILE.exists():
            try:
                return json.loads(CCI_FILE.read_text())
            except Exception:
                pass
        return {
            "score": 0.0, "sessions": 0, "tool_calls": 0,
            "turns": 0, "errors": 0, "history": [],
            "daily_gained": 0.0, "daily_date": None,
        }

    def _save(self) -> None:
        KODA_DIR.mkdir(parents=True, exist_ok=True)
        CCI_FILE.write_text(json.dumps(self._data, indent=2))

    def _reset_daily_if_needed(self) -> None:
        today = _date.today().isoformat()
        if self._data.get("daily_date") != today:
            self._data["daily_date"] = today
            self._data["daily_gained"] = 0.0

    @property
    def score(self) -> float:
        return float(self._data.get("score", 0.0))

    @property
    def daily_gained(self) -> float:
        self._reset_daily_if_needed()
        return float(self._data.get("daily_gained", 0.0))

    def add_delta(self, delta: float, reason: str = "", bypass_cap: bool = False) -> float:
        self._reset_daily_if_needed()

        # Positive deltas from normal interaction are capped per day.
        # Sleep cycle (reason starts with "sleep_"), aha moments, and ratings bypass.
        _bypass = bypass_cap or reason.startswith("sleep_") or reason.startswith("aha") or reason.startswith("rating")
        if delta > 0 and not _bypass:
            remaining = _DAILY_INTERACTION_CAP - self._data.get("daily_gained", 0.0)
            if remaining <= 0:
                return self.score
            delta = min(delta, remaining)
            self._data["daily_gained"] = round(self._data.get("daily_gained", 0.0) + delta, 4)

        new_score = round(self.score + delta, 4)
        self._data["score"] = new_score
        history = self._data.setdefault("history", [])
        history.append({"delta": delta, "reason": reason, "score": new_score})
        if len(history) > 100:
            self._data["history"] = history[-100:]
        self._save()
        return new_score

    def tier_info(self) -> Tuple[str, str, str]:
        s = self.score
        name, emoji, color, _ = _TIERS[0]
        for t_name, t_emoji, t_color, threshold in _TIERS:
            if s >= threshold:
                name, emoji, color = t_name, t_emoji, t_color
        return name, emoji, color

    def tier_name(self) -> str:
        return self.tier_info()[0]

    def tier_color(self) -> str:
        return self.tier_info()[2]

    def autonomy_level(self) -> str:
        s = self.score
        if s >= 10.0: return "full"
        if s >= 5.0:  return "high"
        if s >= 2.5:  return "medium"
        if s >= 1.0:  return "low"
        return "minimal"

    def record_turn(self) -> None:
        self._data["turns"] = self._data.get("turns", 0) + 1
        self.add_delta(0.05, "turn")

    def record_tool(self) -> None:
        self._data["tool_calls"] = self._data.get("tool_calls", 0) + 1
        self.add_delta(0.02, "tool_call")

    def record_error(self) -> None:
        self._data["errors"] = self._data.get("errors", 0) + 1
        self.add_delta(-0.01, "error")

    def record_session_start(self) -> None:
        self._data["sessions"] = self._data.get("sessions", 0) + 1
        self.add_delta(0.1, "session_start")

    def record_aha_jordan(self, desc: str = "") -> float:
        return self.add_delta(0.3, f"aha: {desc[:40]}", bypass_cap=True)

    def record_rate(self, rating: int) -> float:
        delta = (rating - 3) * 0.1
        return self.add_delta(delta, f"rating:{rating}", bypass_cap=True)

    def record_telegram_up(self) -> float:
        return self.add_delta(0.05, "telegram_connected")

    def summary(self) -> dict:
        name, emoji, color = self.tier_info()
        s = self.score
        next_threshold = None
        xp_to_next = None
        for _, _, _, threshold in _TIERS:
            if threshold > s:
                next_threshold = threshold
                xp_to_next = threshold - s
                break
        return {
            "tier_name": name,
            "tier_emoji": emoji,
            "tier_color": color,
            "score": s,
            "xp": s * 100,
            "xp_to_next": xp_to_next,
            "next_threshold": next_threshold,
            "daily_gained": self.daily_gained,
            "daily_cap": _DAILY_INTERACTION_CAP,
            "sessions": self._data.get("sessions", 0),
            "turns": self._data.get("turns", 0),
            "tool_calls": self._data.get("tool_calls", 0),
        }

    def sparkline(self, n: int = 8) -> str:
        bars = "▁▂▃▄▅▆▇█"
        history = self._data.get("history", [])[-n:]
        if not history:
            return "▁" * n
        deltas = [h.get("delta", 0) for h in history]
        max_d = max(abs(d) for d in deltas) or 1
        return "".join(bars[min(7, max(0, int((d / max_d + 1) / 2 * 7)))] for d in deltas)
