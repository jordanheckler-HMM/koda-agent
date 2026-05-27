"""
Koda self-management tools — cron jobs, session state, etc.
These are registered as agent tools so Koda can manage its own scheduler
directly rather than handing commands back to Jordan.
"""
import logging
from typing import Optional

logger = logging.getLogger("koda.koda_tools")


def list_koda_cron_jobs() -> str:
    """List all of Koda's scheduled cron jobs with their status and next run time.

    Call this whenever Jordan asks what recurring tasks are set up, whether a
    cron job exists, or what's scheduled. Always check here before saying you
    don't have any jobs set up.

    Returns:
        A formatted list of all cron jobs, or a message if none exist.
    """
    from koda_durable_agent.cron_runner import load_cron_jobs
    jobs = load_cron_jobs()
    if not jobs:
        return "No cron jobs scheduled yet."
    lines = [f"{len(jobs)} cron job(s) scheduled:"]
    for i, job in enumerate(jobs, 1):
        status = "✓ enabled" if job.enabled else "⏸ paused"
        lines.append(f"\n{i}. {job.name}  [{status}]")
        lines.append(f"   Schedule: {job.schedule_label()}")
        lines.append(f"   Next run: {job.next_run_eta()}")
        lines.append(f"   Task: {job.task}")
        lines.append(f"   Delivery: {job.delivery}")
        lines.append(f"   ID: {job.id}")
    return "\n".join(lines)


def create_koda_cron_job(
    name: str,
    task: str,
    schedule: str,
    delivery: str = "background",
    model: str = "",
) -> str:
    """Create a new Koda scheduled cron job. Use this directly — do not give Jordan a /cron add command to run.

    Args:
        name: Short descriptive name (e.g. 'Daily Storage Scan').
        task: What Koda should do when the job fires. Be specific (e.g. 'Run storage optimization with dry_run=True and report findings').
        schedule: When to run — 'daily@04:00', 'daily@08:00', 'weekly@mon@09:00', 'weekly@fri@17:00', or minutes as a plain number (e.g. '1440').
        delivery: Where to send results — 'telegram' (push to phone), 'chat' (show in TUI), or 'background' (silent log only).
        model: OpenRouter model ID to use. Leave empty for default. Use 'deepseek/deepseek-r1-0528:free' for analysis/research jobs.

    Returns:
        Confirmation string with job details and next run time.
    """
    from koda_durable_agent.cron_runner import add_cron_job

    interval = 0
    sched = None
    if schedule.startswith("daily@") or schedule.startswith("weekly@"):
        sched = schedule
    else:
        try:
            interval = int(schedule)
        except ValueError:
            return (
                f"Error: invalid schedule '{schedule}'. "
                "Use 'daily@HH:MM', 'weekly@DOW@HH:MM', or an interval in minutes."
            )

    if delivery not in ("background", "telegram", "chat"):
        delivery = "background"

    try:
        job = add_cron_job(
            name=name,
            task=task,
            interval_minutes=interval,
            model=model.strip() or None,
            delivery=delivery,
            schedule=sched,
        )
        return (
            f"Cron job created: '{job.name}'\n"
            f"Schedule: {job.schedule_label()}\n"
            f"Next run: {job.next_run_eta()}\n"
            f"Delivery: {job.delivery}\n"
            f"ID: {job.id}"
        )
    except Exception as e:
        logger.warning(f"create_koda_cron_job failed: {e}")
        return f"Error creating cron job: {e}"


def run_koda_cron_job_now(job_id_or_name: str) -> str:
    """Immediately run a cron job by its ID or name, returning the result.
    Use this to test a cron job without waiting for its schedule.

    Args:
        job_id_or_name: The job ID (e.g. 'cron-abc123') or the job name.

    Returns:
        The job output, or an error message if the job couldn't run.
    """
    import httpx
    from datetime import datetime
    from koda_durable_agent.cron_runner import (
        load_cron_jobs, update_cron_job,
        CRON_LOG_DIR, _CRON_PROMPT_TEMPLATE, _get_openrouter_api_key,
    )

    jobs = load_cron_jobs()
    job = next(
        (j for j in jobs if j.id == job_id_or_name or j.name.lower() == job_id_or_name.lower()),
        None,
    )
    if job is None:
        return f"No cron job found matching '{job_id_or_name}'. Use list_koda_cron_jobs() to see current jobs."

    api_key = _get_openrouter_api_key()
    if not api_key:
        return "Error: OPENROUTER_API_KEY not set — can't run job."

    raw_model = job.model or ""
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

    result = ""
    for attempt in range(1, 3):
        try:
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 1024,
                },
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=90.0,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"].strip()
            break
        except Exception as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            is_transient = status_code in (429, 502, 503) or "timeout" in str(e).lower()
            if attempt < 2 and is_transient:
                import time; time.sleep(5)
            else:
                hint = ""
                if status_code == 404:
                    hint = f" (model '{model}' not found on OpenRouter — try a different model ID)"
                elif status_code == 401:
                    hint = " (API key rejected — check OPENROUTER_API_KEY)"
                elif status_code == 429:
                    hint = " (rate limited — try again shortly)"
                return f"Error running job '{job.name}': HTTP {status_code or str(e)}{hint}"

    # Write log and update last_run
    try:
        import json
        CRON_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = CRON_LOG_DIR / f"{job.id}.log"
        entry = json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "job": job.name,
            "model": model,
            "result": result,
            "triggered": "manual",
        })
        with open(log_path, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass
    update_cron_job(job.id, last_run=datetime.now().isoformat(timespec="seconds"))

    # Respect the job's delivery setting — same as the scheduled runner does
    delivery = getattr(job, "delivery", "chat")
    notice = f"⏰ Cron (manual run): {job.name}\n{result}"

    if delivery == "telegram":
        try:
            from koda_durable_agent.gateway import queue_outbound
            from koda_durable_agent.config import settings as _s
            chat_id = _s.TELEGRAM_CHAT_ID
            if chat_id:
                queue_outbound(str(chat_id), notice)
                return f"[{job.name}] ran successfully. Result sent to Telegram."
            else:
                return f"[{job.name}] ran but TELEGRAM_CHAT_ID not configured. Result:\n\n{result}"
        except Exception as e:
            return f"[{job.name}] ran but Telegram delivery failed ({e}). Result:\n\n{result}"
    elif delivery == "background":
        return f"[{job.name}] ran successfully (delivery=background, result logged only)."
    else:
        # chat delivery — return result directly so Koda shows it here
        return f"[{job.name}] ran now using {model}:\n\n{result}"


def delete_koda_cron_job(job_id_or_name: str) -> str:
    """Delete a Koda cron job by its ID or name.

    Args:
        job_id_or_name: The job ID (e.g. 'cron-abc123') or the job name.

    Returns:
        Confirmation or error message.
    """
    from koda_durable_agent.cron_runner import load_cron_jobs, remove_cron_job
    jobs = load_cron_jobs()
    for job in jobs:
        if job.id == job_id_or_name or job.name.lower() == job_id_or_name.lower():
            remove_cron_job(job.id)
            return f"Deleted cron job: '{job.name}' (id: {job.id})"
    return f"No cron job found matching '{job_id_or_name}'. Use list_koda_cron_jobs() to see current jobs."
