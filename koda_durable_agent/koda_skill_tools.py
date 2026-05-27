"""
Koda custom skill tools — lets Koda create its own slash commands.
Skills are stored in ~/.koda/koda_skills.json.
Each skill is a saved prompt that fires when you type its /command.
"""
import json
import uuid
from pathlib import Path

SKILLS_FILE = Path.home() / ".koda" / "koda_skills.json"


def _load() -> list:
    if not SKILLS_FILE.exists():
        return []
    try:
        return json.loads(SKILLS_FILE.read_text())
    except Exception:
        return []


def _save(skills: list) -> None:
    SKILLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKILLS_FILE.write_text(json.dumps(skills, indent=2))


def list_koda_skills() -> str:
    """List all custom slash commands (skills) Koda has created.

    Returns:
        Formatted list of skills, or a message if none exist.
    """
    skills = _load()
    if not skills:
        return "No custom skills created yet. Use create_koda_skill to make one."
    lines = [f"{len(skills)} custom skill(s):"]
    for s in skills:
        lines.append(f"\n  {s['command']}  —  {s['description']}")
        lines.append(f"    Prompt: {s['prompt'][:120]}{'…' if len(s['prompt']) > 120 else ''}")
        lines.append(f"    ID: {s['id']}")
    return "\n".join(lines)


def create_koda_skill(command: str, description: str, prompt: str) -> str:
    """Create a new custom slash command that Koda can run.
    The command will appear in the slash command palette immediately.

    Args:
        command: The slash command to create (e.g. '/morning-brief'). Must start with /.
        description: Short description shown in the command palette.
        prompt: The task or instruction Koda will execute when the command is typed.

    Returns:
        Confirmation with the new skill details.
    """
    if not command.startswith("/"):
        command = "/" + command

    # Reject reserved built-in commands
    reserved = {
        "/help", "/model", "/models", "/status", "/tools", "/persona", "/personas",
        "/soul", "/soul-edit", "/compact", "/new", "/clear", "/history", "/export",
        "/memo", "/reload", "/sync", "/state", "/stats", "/heartbeat", "/sleep",
        "/cron", "/evolve", "/cci", "/aha", "/rate", "/setdefault", "/telegram",
        "/sweep", "/exit",
    }
    if command.lower() in reserved:
        return f"Error: '{command}' is a reserved built-in command. Choose a different name."

    skills = _load()

    # Update if command already exists
    for s in skills:
        if s["command"].lower() == command.lower():
            s["description"] = description
            s["prompt"] = prompt
            _save(skills)
            return f"Updated existing skill '{command}'.\nDescription: {description}\nPrompt: {prompt}"

    skill = {
        "id": f"skill-{uuid.uuid4().hex[:8]}",
        "command": command.lower(),
        "description": description,
        "prompt": prompt,
    }
    skills.append(skill)
    _save(skills)
    return (
        f"Skill created: '{command}'\n"
        f"Description: {description}\n"
        f"Prompt: {prompt}\n"
        f"ID: {skill['id']}\n\n"
        f"You can now type {command} in the TUI to run this. It will appear in the command palette on next session start."
    )


def delete_koda_skill(command: str) -> str:
    """Delete a custom slash command skill.

    Args:
        command: The slash command to delete (e.g. '/morning-brief') or its skill ID.

    Returns:
        Confirmation or error message.
    """
    skills = _load()
    original_len = len(skills)
    skills = [
        s for s in skills
        if s["command"].lower() != command.lower() and s["id"] != command
    ]
    if len(skills) == original_len:
        return f"No skill found matching '{command}'. Use list_koda_skills() to see current skills."
    _save(skills)
    return f"Deleted skill '{command}'."
