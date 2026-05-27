"""
Koda built-in skill templates.
These are ready-made cron jobs users can install by asking Koda.
"""
import sys

_IS_MACOS = sys.platform == "darwin"

# Each template: id, name, description, task, schedule, delivery, macos_only
_TEMPLATES = [
    {
        "id": "morning_brief",
        "name": "Morning Brief",
        "description": "Daily morning message with the date, day of week, and a motivating thought.",
        "task": (
            "Write a short morning brief for {user_name}. "
            "Include: today's date, day of week, and one motivating thought for the day. "
            "Keep it under 4 sentences. Be warm and direct. No filler."
        ),
        "schedule": "daily@08:00",
        "delivery": "{delivery}",
        "macos_only": False,
    },
    {
        "id": "sms_triage",
        "name": "SMS Triage",
        "description": "Scans your recent iMessages and flags anything that needs a reply. Suppresses spam and banter.",
        "task": (
            "Triage {user_name}'s recent iMessages. "
            "Call get_recent_messages to scan recent conversations. "
            "Identify messages that need a reply: questions, time-sensitive requests, commitments, unusual patterns. "
            "Suppress: spam, marketing, casual banter, already-handled threads. "
            "Report clearly: who sent it, what they want, recommended action. "
            "If nothing needs attention, say 'All clear — nothing new needs a reply.'"
        ),
        "schedule": "daily@09:00",
        "delivery": "{delivery}",
        "macos_only": True,
    },
    {
        "id": "weekly_review",
        "name": "Weekly Review",
        "description": "Every Sunday evening: reflect on the week, surface wins and open loops.",
        "task": (
            "Run a weekly review for {user_name}. "
            "Today is the end of the week. Prompt with: "
            "What did I accomplish this week? What's still open? What should I do differently next week? "
            "Keep it brief and practical — 5-7 bullet points max. Be direct."
        ),
        "schedule": "weekly@sun@18:00",
        "delivery": "{delivery}",
        "macos_only": False,
    },
    {
        "id": "daily_focus",
        "name": "Daily Focus",
        "description": "Every morning: asks what the single most important thing to do today is.",
        "task": (
            "Help {user_name} set their daily focus. "
            "Ask: Given everything on your plate, what is the ONE most important thing to accomplish today? "
            "Then offer 2-3 follow-up questions to pressure-test the answer. "
            "Keep the whole exchange under 5 turns."
        ),
        "schedule": "daily@07:30",
        "delivery": "{delivery}",
        "macos_only": False,
    },
    {
        "id": "reminders_check",
        "name": "Reminders Check",
        "description": "Daily scan of your Apple Reminders for overdue or due-soon items. macOS only.",
        "task": (
            "Check {user_name}'s Apple Reminders. "
            "Call list_reminders to scan all lists. "
            "Flag: overdue items, items due today, items due in the next 24 hours. "
            "Suppress completed items. "
            "Report clearly: what's due, which list it's in, how overdue if applicable. "
            "If nothing is urgent, say 'All clear — nothing due soon.'"
        ),
        "schedule": "daily@08:30",
        "delivery": "{delivery}",
        "macos_only": True,
    },
    {
        "id": "evening_wrap",
        "name": "Evening Wrap",
        "description": "End-of-day message: what got done, what to carry to tomorrow.",
        "task": (
            "Write a short evening wrap-up for {user_name}. "
            "Include: a prompt to note what was accomplished today and what carries to tomorrow. "
            "Keep it under 3 sentences. Calm and grounding. No hype."
        ),
        "schedule": "daily@18:00",
        "delivery": "{delivery}",
        "macos_only": False,
    },
]


def list_koda_templates() -> str:
    """List all available built-in skill templates that can be installed as cron jobs.

    Returns a formatted list of templates with their IDs, descriptions, and schedules.
    Use install_koda_template(template_id) to install one.
    """
    lines = ["Available skill templates:\n"]
    for t in _TEMPLATES:
        if t["macos_only"] and not _IS_MACOS:
            continue
        macos_tag = "  [macOS only]" if t["macos_only"] else ""
        lines.append(f"  {t['id']:<20} {t['name']}{macos_tag}")
        lines.append(f"  {'':20} {t['description']}")
        lines.append(f"  {'':20} Schedule: {t['schedule']}\n")
    lines.append("To install: install_koda_template('<id>')")
    return "\n".join(lines)


def install_koda_template(template_id: str, delivery: str = "chat") -> str:
    """Install a built-in skill template as a scheduled cron job.

    Args:
        template_id: The template ID from list_koda_templates (e.g. 'morning_brief', 'sms_triage').
        delivery: Where to deliver results — 'telegram' (phone), 'chat' (TUI), or 'background' (silent).
    """
    from koda_durable_agent.cron_runner import add_cron_job, load_cron_jobs
    from koda_durable_agent.config import settings

    template = next((t for t in _TEMPLATES if t["id"] == template_id), None)
    if not template:
        ids = [t["id"] for t in _TEMPLATES if not t["macos_only"] or _IS_MACOS]
        return f"Unknown template '{template_id}'. Available: {', '.join(ids)}"

    if template["macos_only"] and not _IS_MACOS:
        return f"'{template['name']}' requires macOS and is not available on this platform."

    # Check if already installed
    existing = {j.name.lower() for j in load_cron_jobs()}
    if template["name"].lower() in existing:
        return f"'{template['name']}' is already scheduled. Use /cron to view it."

    user_name = getattr(settings, "USER_NAME", None) or "you"
    task = template["task"].replace("{user_name}", user_name)

    sched = template["schedule"]
    add_cron_job(
        name=template["name"],
        task=task,
        interval_minutes=0,
        model=None,
        delivery=delivery,
        schedule=sched,
    )
    return (
        f"✓ '{template['name']}' installed — runs {sched}, delivery={delivery}.\n"
        f"Use /cron to view all scheduled jobs or run it now with run_koda_cron_job_now('{template['name']}')."
    )
