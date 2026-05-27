"""
Koda Cron Runner — self-scheduled recurring tasks.
Jobs stored in ~/.koda/koda_crons.json.
Runner checks every 60 seconds.

Schedule formats:
  interval_minutes > 0  — run every N minutes (legacy / interval-based)
  schedule="daily@04:00"          — every day at 4:00 AM
  schedule="weekly@mon@09:00"     — every Monday at 9:00 AM
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("koda.cron")

CRON_FILE    = Path.home() / ".koda" / "koda_crons.json"
CRON_LOG_DIR = Path.home() / ".koda" / "cron_logs"

_CRON_PROMPT_TEMPLATE = """\
You are Koda executing a scheduled task. Complete the task below and respond with a concise result summary (2-5 sentences max). Do not ask follow-up questions.

Task: {task}

Current time: {now}
Job name: {name}
"""

DELIVERY_MODES = ("chat", "telegram", "background")

_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _next_schedule_dt(schedule: str, last_run: Optional[str]) -> Optional[datetime]:
    """Compute next run datetime from a schedule string and last_run timestamp."""
    now = datetime.now()

    if schedule.startswith("daily@"):
        h, m = map(int, schedule.split("@", 1)[1].split(":"))
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run)
                return (last_dt + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
            except Exception:
                pass
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if schedule.startswith("weekly@"):
        parts = schedule.split("@")
        dow = _DOW.get(parts[1].lower() if len(parts) > 1 else "mon", 0)
        h, m = map(int, (parts[2] if len(parts) > 2 else "09:00").split(":"))
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run)
                return (last_dt + timedelta(weeks=1)).replace(hour=h, minute=m, second=0, microsecond=0)
            except Exception:
                pass
        days_ahead = (dow - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(weeks=1)
        return candidate

    return None


@dataclass
class KodaCronJob:
    id: str
    name: str
    task: str
    interval_minutes: int        # 0 when schedule-based
    model: Optional[str]
    enabled: bool
    last_run: Optional[str]
    created_at: str
    delivery: str = "chat"       # "chat" | "telegram" | "background"
    schedule: Optional[str] = None  # "daily@04:00" | "weekly@mon@09:00" | None

    def effective_model(self, prefs: dict) -> str:
        return self.model or prefs.get("default_model", "openai/gpt-oss-120b:free")

    def next_run_dt(self) -> Optional[datetime]:
        if self.schedule:
            return _next_schedule_dt(self.schedule, self.last_run)
        if not self.last_run:
            return None
        try:
            return datetime.fromisoformat(self.last_run) + timedelta(minutes=self.interval_minutes)
        except Exception:
            return None

    def is_overdue(self) -> bool:
        next_dt = self.next_run_dt()
        return next_dt is None or datetime.now() >= next_dt

    def schedule_label(self) -> str:
        """Human-readable schedule description."""
        if self.schedule:
            if self.schedule.startswith("daily@"):
                return f"daily at {self.schedule.split('@',1)[1]}"
            if self.schedule.startswith("weekly@"):
                parts = self.schedule.split("@")
                day = parts[1].capitalize() if len(parts) > 1 else "?"
                time = parts[2] if len(parts) > 2 else "?"
                return f"weekly {day} at {time}"
            return self.schedule
        h = self.interval_minutes // 60
        m = self.interval_minutes % 60
        if h and m:
            return f"every {h}h {m}m"
        if h:
            return f"every {h}h"
        return f"every {self.interval_minutes}m"

    def next_run_eta(self) -> str:
        next_dt = self.next_run_dt()
        if next_dt is None:
            return "pending"
        delta = next_dt - datetime.now()
        total_mins = int(delta.total_seconds() / 60)
        if total_mins <= 0:
            return "overdue"
        if total_mins < 60:
            return f"in ~{total_mins}m"
        if total_mins < 1440:
            return f"in ~{total_mins // 60}h {total_mins % 60}m"
        days = total_mins // 1440
        return f"in ~{days}d {(total_mins % 1440) // 60}h"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "KodaCronJob":
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in fields})


def load_cron_jobs() -> list:
    try:
        if CRON_FILE.exists():
            raw = json.loads(CRON_FILE.read_text())
            return [KodaCronJob.from_dict(d) for d in raw]
    except Exception as e:
        logger.warning(f"Failed to load cron jobs: {e}")
    return []


def save_cron_jobs(jobs: list) -> None:
    try:
        CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
        CRON_FILE.write_text(json.dumps([j.to_dict() for j in jobs], indent=2))
    except Exception as e:
        logger.warning(f"Failed to save cron jobs: {e}")


def add_cron_job(
    name: str,
    task: str,
    interval_minutes: int = 0,
    model: Optional[str] = None,
    delivery: str = "chat",
    schedule: Optional[str] = None,
) -> "KodaCronJob":
    if delivery not in DELIVERY_MODES:
        delivery = "chat"
    job = KodaCronJob(
        id=f"cron-{uuid.uuid4().hex[:8]}",
        name=name,
        task=task,
        interval_minutes=interval_minutes,
        model=model,
        enabled=True,
        last_run=None,
        created_at=datetime.now().isoformat(timespec="seconds"),
        delivery=delivery,
        schedule=schedule,
    )
    jobs = load_cron_jobs()
    jobs.append(job)
    save_cron_jobs(jobs)
    return job


def remove_cron_job(job_id: str) -> bool:
    jobs = load_cron_jobs()
    new_jobs = [j for j in jobs if j.id != job_id]
    if len(new_jobs) == len(jobs):
        return False
    save_cron_jobs(new_jobs)
    return True


def update_cron_job(job_id: str, **kwargs) -> Optional["KodaCronJob"]:
    jobs = load_cron_jobs()
    for job in jobs:
        if job.id == job_id:
            for k, v in kwargs.items():
                if hasattr(job, k):
                    setattr(job, k, v)
            save_cron_jobs(jobs)
            return job
    return None


def _get_openrouter_api_key() -> Optional[str]:
    import os
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        env_path = Path.home() / ".koda" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    return key or None


class KodaCronRunner:
    def __init__(
        self,
        prefs: dict,
        save_prefs: Callable,
        notify: Callable[[str], None],
        send_telegram: Optional[Callable[[str], None]] = None,
    ):
        self.prefs = prefs
        self.save_prefs = save_prefs
        self.notify = notify
        self.send_telegram = send_telegram
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if not self.is_running():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def trigger_job(self, job_id: str) -> bool:
        jobs = load_cron_jobs()
        for job in jobs:
            if job.id == job_id:
                await self._run_job(job)
                return True
        return False

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                await self._check_jobs()
        except asyncio.CancelledError:
            pass

    async def _check_jobs(self) -> None:
        jobs = load_cron_jobs()
        for job in jobs:
            if job.enabled and job.is_overdue():
                await self._run_job(job)

    async def _run_job(self, job: "KodaCronJob") -> None:
        import httpx
        api_key = _get_openrouter_api_key()

        result = ""
        if not api_key:
            result = "Skipped — no OPENROUTER_API_KEY available"
            logger.warning(f"Cron job '{job.name}': no API key, skipping")
        else:
            # Use job's assigned model or fall back to a capable free model
            raw_model = job.model or self.prefs.get("default_model", "")
            # Strip provider prefix for Gemini models; use a capable OpenRouter model as default
            if not raw_model or raw_model.startswith("gemini") or raw_model.startswith("ollama/"):
                model = "openai/gpt-oss-120b:free"
            elif raw_model.startswith("openrouter/"):
                model = raw_model.removeprefix("openrouter/")
            else:
                model = raw_model

            prompt = _CRON_PROMPT_TEMPLATE.format(
                task=job.task,
                now=datetime.now().strftime("%Y-%m-%d %H:%M"),
                name=job.name,
            )
            for attempt in range(1, 3):
                try:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        resp = await client.post(
                            "https://openrouter.ai/api/v1/chat/completions",
                            json={
                                "model": model,
                                "messages": [{"role": "user", "content": prompt}],
                                "temperature": 0.3,
                                "max_tokens": 1024,
                            },
                            headers={"Authorization": f"Bearer {api_key}"},
                        )
                        resp.raise_for_status()
                        result = resp.json()["choices"][0]["message"]["content"].strip()
                    break
                except Exception as e:
                    status_code = getattr(getattr(e, "response", None), "status_code", None)
                    is_transient = status_code in (429, 502, 503) or "timeout" in str(e).lower()
                    if attempt < 2 and is_transient:
                        await asyncio.sleep(20)
                    else:
                        result = f"Error: {type(e).__name__}: {e}"
                        logger.warning(f"Cron job '{job.name}' failed: {e}")

        self._write_log(job, model if api_key else "none", result)
        update_cron_job(job.id, last_run=datetime.now().isoformat(timespec="seconds"))

        delivery = getattr(job, "delivery", "chat")
        notice = f"⏰ Cron: {job.name}\n{result[:300]}"
        if delivery == "telegram":
            if self.send_telegram:
                self.send_telegram(notice)
            else:
                logger.warning(f"Cron job '{job.name}' delivery=telegram but no send_telegram configured")
        elif delivery == "background":
            pass
        else:
            self.notify(notice)

    def _write_log(self, job: "KodaCronJob", model: str, result: str) -> None:
        try:
            CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = CRON_LOG_DIR / f"{job.id}.log"
            entry = json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "job": job.name,
                "model": model,
                "result": result,
            })
            with open(log_path, "a") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning(f"Failed to write cron log for '{job.name}': {e}")
