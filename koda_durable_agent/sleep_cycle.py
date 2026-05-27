"""
Koda Sleep Cycle — nightly interaction review and CCI calibration.

Fires at 2:00 AM local time. Reviews the day's interactions, grades quality,
and uses that grade to calculate a sleep quality score that adjusts CCI.

Without this, CCI only goes up (quantity-based). Sleep cycle makes it
quality-sensitive: great sessions earn more, poor ones earn less or lose ground.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("koda.sleep")

KODA_DIR = Path.home() / ".koda"
DREAMS_PATH = KODA_DIR / "koda_dreams.md"
SLEEP_LOG_PATH = KODA_DIR / "sleep_log.json"

# Grading rubric sent to the LLM
_GRADE_PROMPT = """\
You are reviewing a day of interactions between a user and Koda, their personal AI agent.

Here are the interactions from today:
{interactions}

Grade this day's interactions on a scale of 0.0 to 1.0 across these dimensions:
1. Task completion — did Koda actually finish what was asked?
2. Accuracy — were responses correct and grounded?
3. Efficiency — did Koda get to the point or ramble?
4. User satisfaction — did the user seem satisfied (no repeated corrections, frustration, or abandoned threads)?

Return a JSON object with this exact structure:
{{
  "task_completion": 0.0-1.0,
  "accuracy": 0.0-1.0,
  "efficiency": 0.0-1.0,
  "user_satisfaction": 0.0-1.0,
  "overall": 0.0-1.0,
  "summary": "one sentence summary of how the day went",
  "strengths": ["one strength"],
  "improvements": ["one area to improve"]
}}

Be honest. A day with no interactions scores 0.5 (neutral). A day with errors, repeated corrections,
or abandoned tasks should score below 0.5. Only score above 0.8 if Koda was genuinely helpful and efficient.
"""


def _get_openrouter_key() -> Optional[str]:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        env_path = KODA_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key or None


def _load_sleep_log() -> dict:
    if SLEEP_LOG_PATH.exists():
        try:
            return json.loads(SLEEP_LOG_PATH.read_text())
        except Exception:
            pass
    return {"last_sleep_date": None, "history": []}


def _save_sleep_log(data: dict) -> None:
    KODA_DIR.mkdir(parents=True, exist_ok=True)
    SLEEP_LOG_PATH.write_text(json.dumps(data, indent=2))


async def _grade_interactions(interactions: list) -> dict:
    """Call OpenRouter to grade today's interactions. Returns grading dict."""
    if not interactions:
        return {
            "task_completion": 0.5, "accuracy": 0.5, "efficiency": 0.5,
            "user_satisfaction": 0.5, "overall": 0.5,
            "summary": "No interactions today.",
            "strengths": [], "improvements": [],
        }

    api_key = _get_openrouter_key()
    if not api_key:
        return {
            "task_completion": 0.5, "accuracy": 0.5, "efficiency": 0.5,
            "user_satisfaction": 0.5, "overall": 0.5,
            "summary": "Could not grade — no API key.",
            "strengths": [], "improvements": [],
        }

    # Summarise interactions for the prompt (avoid sending full message content)
    interaction_text = ""
    for i, turn in enumerate(interactions[-20:], 1):  # last 20 turns max
        role = turn.get("role", "?")
        content = str(turn.get("content", ""))[:200]
        interaction_text += f"Turn {i} [{role}]: {content}\n"

    prompt = _GRADE_PROMPT.format(interactions=interaction_text)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "openai/gpt-oss-120b:free",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 400,
                },
            )
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            # Extract JSON from response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
    except Exception as e:
        logger.warning(f"Sleep cycle grading failed: {e}")

    return {
        "task_completion": 0.5, "accuracy": 0.5, "efficiency": 0.5,
        "user_satisfaction": 0.5, "overall": 0.5,
        "summary": "Grading unavailable.",
        "strengths": [], "improvements": [],
    }


def _sleep_quality_to_cci_delta(grade: dict, turns_today: int) -> float:
    """Convert sleep quality grade into a CCI delta.

    Design:
    - Base delta earned through the day is capped (turns * small_amount)
    - Sleep quality multiplies that earned amount: great day = full credit,
      poor day = partial or negative credit on top of what was already gained
    - This means quality matters more than quantity
    """
    overall = grade.get("overall", 0.5)

    if turns_today == 0:
        return 0.0

    # Raw daily gain from turns (already applied incrementally during day)
    # Sleep applies a retroactive quality correction:
    # quality 1.0 → +0.3 bonus
    # quality 0.7 → +0.1 bonus
    # quality 0.5 → 0 (neutral)
    # quality 0.3 → -0.2 penalty
    # quality 0.0 → -0.5 penalty
    if overall >= 0.8:
        return 0.3
    elif overall >= 0.7:
        return 0.15
    elif overall >= 0.6:
        return 0.05
    elif overall >= 0.5:
        return 0.0
    elif overall >= 0.4:
        return -0.1
    elif overall >= 0.3:
        return -0.2
    else:
        return -0.4


def _write_dream(grade: dict, cci_delta: float, today: str) -> None:
    """Append the night's dream summary to koda_dreams.md."""
    KODA_DIR.mkdir(parents=True, exist_ok=True)
    emoji = "🌟" if grade["overall"] >= 0.8 else "🌙" if grade["overall"] >= 0.5 else "🌧"
    sign = "+" if cci_delta >= 0 else ""
    entry = (
        f"\n## {today}  {emoji}\n"
        f"**Sleep quality:** {grade['overall']:.2f}  ·  **CCI delta:** {sign}{cci_delta:.2f}\n\n"
        f"_{grade.get('summary', '')}_\n\n"
    )
    if grade.get("strengths"):
        entry += "**Strengths:**\n" + "\n".join(f"- {s}" for s in grade["strengths"]) + "\n\n"
    if grade.get("improvements"):
        entry += "**Improvements:**\n" + "\n".join(f"- {s}" for s in grade["improvements"]) + "\n"

    with open(DREAMS_PATH, "a") as f:
        f.write(entry)


async def run_sleep_cycle(interactions: list, turns_today: int, cci_tracker) -> dict:
    """Run the full sleep cycle. Called by SleepCycleRunner at 2am.

    Args:
        interactions: List of message dicts from today's conversations.
        turns_today: Number of turns completed today.
        cci_tracker: The active CCITracker instance.

    Returns the grade dict.
    """
    today = date.today().isoformat()
    log = _load_sleep_log()

    # Skip if already ran today
    if log.get("last_sleep_date") == today:
        logger.info("Sleep cycle already ran today — skipping.")
        return {}

    logger.info("Sleep cycle starting...")
    grade = await _grade_interactions(interactions)
    cci_delta = _sleep_quality_to_cci_delta(grade, turns_today)

    # Apply to CCI
    if cci_delta != 0:
        cci_tracker.add_delta(cci_delta, f"sleep_quality:{grade['overall']:.2f}")

    # Record in sleep log
    log["last_sleep_date"] = today
    log.setdefault("history", []).append({
        "date": today,
        "overall": grade["overall"],
        "cci_delta": cci_delta,
        "turns": turns_today,
        "summary": grade.get("summary", ""),
    })
    if len(log["history"]) > 90:
        log["history"] = log["history"][-90:]
    _save_sleep_log(log)

    _write_dream(grade, cci_delta, today)
    logger.info(f"Sleep cycle complete: quality={grade['overall']:.2f}, CCI delta={cci_delta:+.2f}")
    return grade


def _seconds_until_2am() -> float:
    """How many seconds until the next 2:00 AM."""
    now = datetime.now()
    target = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if now >= target:
        # Already past 2am today — aim for tomorrow
        from datetime import timedelta
        target += timedelta(days=1)
    return (target - now).total_seconds()


class SleepCycleRunner:
    def __init__(self, cci_tracker=None, get_interactions=None,
                 get_turns=None, notify=None, **kwargs):
        self._task: Optional[asyncio.Task] = None
        self._sleep_count: int = 0
        self._last_sleep: Optional[str] = None
        self._cci_tracker = cci_tracker
        self._get_interactions = get_interactions  # callable → list of message dicts
        self._get_turns = get_turns                # callable → int
        self._notify = notify                      # callable → str notice to show in TUI

        log = _load_sleep_log()
        self._last_sleep = log.get("last_sleep_date")
        self._sleep_count = len(log.get("history", []))

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def next_sleep_eta(self) -> str:
        secs = _seconds_until_2am()
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        return f"in {h}h {m}m"

    def last_sleep(self) -> Optional[str]:
        return self._last_sleep

    def get_pending_notice(self) -> Optional[str]:
        return None

    async def _loop(self) -> None:
        while True:
            wait = _seconds_until_2am()
            logger.info(f"Sleep cycle scheduled in {wait/3600:.1f}h")
            await asyncio.sleep(wait)
            try:
                interactions = self._get_interactions() if self._get_interactions else []
                turns = self._get_turns() if self._get_turns else 0
                grade = await run_sleep_cycle(interactions, turns, self._cci_tracker)
                self._sleep_count += 1
                self._last_sleep = date.today().isoformat()
                if grade and self._notify:
                    overall = grade.get("overall", 0.5)
                    emoji = "🌟" if overall >= 0.8 else "🌙" if overall >= 0.5 else "🌧"
                    self._notify(
                        f"{emoji} Sleep cycle complete — quality {overall:.2f}  "
                        f"· see ~/.koda/koda_dreams.md"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sleep cycle error: {e}")
            # Sleep 23h before checking again (avoids re-triggering same night)
            await asyncio.sleep(23 * 3600)
