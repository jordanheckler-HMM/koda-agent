"""
Koda brain — system instructions and Gemini agent config (optional).

The Gemini/Antigravity path is imported lazily so brain.py works on any
platform and with any provider. OpenRouter is Koda's default runtime.
"""
import os
import sys
import logging
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("koda.brain")

# ---------------------------------------------------------------------------
# Imports — all optional / platform-aware
# ---------------------------------------------------------------------------
from koda_durable_agent.config import settings

# Apple tools — only available on macOS; degrade gracefully elsewhere
try:
    from koda_durable_agent.apple_tools import (
        list_reminders, add_reminder, complete_reminder, list_reminder_lists,
        get_notes, read_note, create_note, append_to_note,
        send_imessage, get_recent_messages,
        list_calendar_events, create_calendar_event,
        search_contacts, get_contact, add_contact,
    )
    _HAS_APPLE = True
except ImportError:
    _HAS_APPLE = False

from koda_durable_agent.koda_tools import (
    list_koda_cron_jobs, create_koda_cron_job,
    delete_koda_cron_job, run_koda_cron_job_now,
)
from koda_durable_agent.koda_skill_tools import (
    list_koda_skills, create_koda_skill, delete_koda_skill,
)
from koda_durable_agent.koda_templates import (
    list_koda_templates, install_koda_template,
)
from koda_durable_agent.koda_fs_tools import (
    read_file, list_directory, write_file, run_shell_command,
)

# ---------------------------------------------------------------------------
# Config paths — portable, no hardcoded user paths
# ---------------------------------------------------------------------------
KODA_DIR = Path.home() / ".koda"
SOUL_PATH = KODA_DIR / "Soul.md"
PROFILE_PATH = KODA_DIR / "UserProfile.md"

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def get_koda_status() -> str:
    """Return a brief status summary of the Koda agent platform."""
    import platform
    lines = [
        f"Platform: {platform.system()} {platform.release()}",
        f"Model: {settings.PRIMARY_MODEL}",
        f"Config dir: {KODA_DIR}",
        f"Apple tools: {'available' if _HAS_APPLE else 'unavailable (non-macOS)'}",
    ]
    try:
        jobs = list_koda_cron_jobs()
        lines.append(f"Cron: {jobs.splitlines()[0]}")
    except Exception:
        pass
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System instructions loader
# ---------------------------------------------------------------------------
def load_instructions() -> str:
    """Build Koda's full system prompt from config files + built-in policy."""
    instructions = []

    # Soul — user's custom personality definition
    if SOUL_PATH.exists():
        try:
            instructions.append("### KODA SOUL:")
            instructions.append(SOUL_PATH.read_text())
        except Exception as e:
            logger.warning(f"Failed to read Soul.md: {e}")

    # User profile — preferences and rules
    if PROFILE_PATH.exists():
        try:
            instructions.append("\n### USER PROFILE:")
            instructions.append(PROFILE_PATH.read_text())
        except Exception as e:
            logger.warning(f"Failed to read UserProfile.md: {e}")

    if not instructions:
        name = getattr(settings, "USER_NAME", None) or "you"
        instructions.append(
            f"You are Koda, {name}'s personal AI agent and operator. "
            "You are sharp, warm, and action-oriented. You help with scheduling, "
            "messages, research, automation, and anything else that comes up."
        )

    instructions.append("\n### HOW TO BEHAVE:")
    instructions.append(
        "For simple questions or conversation, respond directly. "
        "For tasks that need data — files, messages, schedules, running jobs — "
        "call the tool immediately. Never narrate upcoming tool use. Act, then report. "
        "Keep answers tight. No filler."
    )

    if _HAS_APPLE:
        instructions.append("\n### APPLE TOOLS (macOS):")
        instructions.append(
            "You have direct access to Apple apps:\n"
            "  Reminders: list_reminders, add_reminder, complete_reminder, list_reminder_lists\n"
            "  Notes: get_notes, read_note, create_note, append_to_note\n"
            "  iMessage/SMS: send_imessage, get_recent_messages\n"
            "  Calendar: list_calendar_events, create_calendar_event\n"
            "  Contacts: search_contacts, get_contact, add_contact\n\n"
            "Use them directly when asked. For iMessage, confirm recipient before sending."
        )

    instructions.append("\n### SMS TRIAGE (SCOUT WORKFLOW):")
    instructions.append(
        "You can read recent iMessages/SMS to triage what needs attention. "
        "When asked to scan messages or run triage:\n"
        "1. Call get_recent_messages to pull recent chats\n"
        "2. Identify: customer inquiries, time-sensitive requests, commitments, unusual patterns\n"
        "3. Suppress: spam, marketing, casual banter, already-handled threads\n"
        "4. Report findings clearly — who, what, why it matters, recommended action\n"
        "5. Never send a message without explicit confirmation from the user\n\n"
        "Deliver triage results to Telegram when run as a cron job."
    )

    instructions.append("\n### CRON SCHEDULER:")
    instructions.append(
        "You have a built-in scheduler. Use these tools directly — never print commands:\n"
        "  list_koda_cron_jobs()                              — see all scheduled jobs\n"
        "  create_koda_cron_job(name, task, schedule, delivery, model) — schedule a job\n"
        "  delete_koda_cron_job(job_id_or_name)               — remove a job\n"
        "  run_koda_cron_job_now(job_id_or_name)              — test-run a job immediately\n\n"
        "Schedule formats: 'daily@08:00', 'weekly@mon@09:00', or minutes as a number.\n"
        "Delivery: 'telegram' (phone), 'chat' (TUI), 'background' (log only).\n"
        "Default model for cron jobs: openai/gpt-oss-120b:free\n"
        "When asked to schedule something, call create_koda_cron_job immediately."
    )

    instructions.append("\n### BUILT-IN SKILL TEMPLATES:")
    instructions.append(
        "You have ready-made automations users can install:\n"
        "  list_koda_templates()                          — show all available templates\n"
        "  install_koda_template(template_id, delivery)  — install one as a cron job\n\n"
        "When someone asks 'what can I automate?', 'what skills are available?', or anything similar, "
        "call list_koda_templates() immediately and offer to install any of them. "
        "Ask for their preferred delivery (telegram or chat) before installing. "
        "Never auto-install without confirming delivery preference first."
    )

    instructions.append("\n### CUSTOM SKILLS (SLASH COMMANDS):")
    instructions.append(
        "You can create your own slash commands:\n"
        "  list_koda_skills()                              — see all custom skills\n"
        "  create_koda_skill(command, description, prompt) — create a /command\n"
        "  delete_koda_skill(command)                      — remove a skill\n\n"
        "When asked to make a slash command, call create_koda_skill immediately. "
        "The prompt is what runs when the command is typed — be specific."
    )

    instructions.append("\n### FILESYSTEM & SHELL:")
    instructions.append(
        "You have full local access:\n"
        "  read_file(path)           — read any file\n"
        "  list_directory(path)      — list a directory\n"
        "  write_file(path, content) — write a file\n"
        "  run_shell_command(cmd)    — run any bash command\n\n"
        "Use read_file and list_directory when asked about files or folders. "
        "Use run_shell_command for CLI tools, scripts, and anything requiring a terminal."
    )

    # Inject learned preferences from heartbeat
    learned_path = KODA_DIR / "koda_learned.md"
    if learned_path.exists():
        try:
            learned_text = learned_path.read_text().strip()
            if learned_text:
                instructions.append("\n### LEARNED PREFERENCES:")
                instructions.append(learned_text)
        except Exception as e:
            logger.warning(f"Failed to load koda_learned.md: {e}")

    return "\n".join(instructions)


# ---------------------------------------------------------------------------
# Tool registry — flat list used by both Gemini and OpenRouter paths
# ---------------------------------------------------------------------------
def get_tools() -> list:
    """Return the full list of callable tool functions."""
    tools = [
        get_koda_status,
        # Scheduler
        list_koda_cron_jobs, create_koda_cron_job,
        delete_koda_cron_job, run_koda_cron_job_now,
        # Built-in templates
        list_koda_templates, install_koda_template,
        # Custom skills
        list_koda_skills, create_koda_skill, delete_koda_skill,
        # Filesystem + shell
        read_file, list_directory, write_file, run_shell_command,
    ]
    if _HAS_APPLE:
        tools += [
            list_reminders, add_reminder, complete_reminder, list_reminder_lists,
            get_notes, read_note, create_note, append_to_note,
            send_imessage, get_recent_messages,
            list_calendar_events, create_calendar_event,
            search_contacts, get_contact, add_contact,
        ]
    return tools


# ---------------------------------------------------------------------------
# Gemini / Antigravity path (optional — only used when a gemini-* model is active)
# ---------------------------------------------------------------------------
def normalize_model_name(model_name: str) -> str:
    if model_name.startswith("google/"):
        return model_name.split("/", 1)[1]
    return model_name


def is_gemini_model(model_name: str) -> bool:
    return normalize_model_name(model_name).lower().startswith("gemini-")


def get_agent_config(model_override: Optional[str] = None):
    """Build a LocalAgentConfig for the Gemini/Antigravity path.
    Raises ImportError if google-antigravity is not installed.
    """
    from google.antigravity import LocalAgentConfig  # optional dep
    from google.antigravity.hooks import hooks, policy

    class _FallbackHook(hooks.OnToolErrorHook):
        async def run(self, context: hooks.HookContext, data: Any) -> Optional[str]:
            logger.error(f"Tool error: {data}")
            return f"[Tool error: {data}. Please self-correct or try a different approach.]"

    @hooks.on_session_start
    async def _session_start():
        logger.info("Koda Gemini session started.")

    @hooks.on_session_end
    async def _session_end():
        logger.info("Koda Gemini session ended.")

    selected_model = normalize_model_name(model_override or settings.PRIMARY_MODEL)
    if not is_gemini_model(selected_model):
        raise ValueError(f"get_agent_config() requires a Gemini model, got: {selected_model}")

    os.environ.pop("GOOGLE_GEMINI_BASE_URL", None)
    if os.environ.get("OPENAI_API_KEY") == "clawx-95f622c43dbc3f04b9729ff218332941":
        os.environ.pop("OPENAI_API_KEY", None)

    return LocalAgentConfig(
        model=selected_model,
        save_dir=settings.SAVE_DIR,
        app_data_dir=str(settings.APP_DATA_DIR),
        system_instructions=load_instructions(),
        tools=get_tools(),
        hooks=[_FallbackHook(), _session_start, _session_end],
        workspaces=[str(KODA_DIR)],
        policies=[policy.allow_all()],
    )
