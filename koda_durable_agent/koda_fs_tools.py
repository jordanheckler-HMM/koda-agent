"""Filesystem and shell tools for Koda — full local access."""
import re
import subprocess
import os
from pathlib import Path

# AI CLI tools that cost the user money or quota when invoked.
# Koda will ask for approval before running these — the user can approve
# once, deny once, or say "never ask again" either way.
_AI_CLI_PATTERN = re.compile(
    r"(?<![/\w])(claude|codex|anthropic|gemini|openai|copilot|cursor|aider|gpt4all|ollama run)\b",
    re.IGNORECASE,
)


def read_file(path: str) -> str:
    """Read any file on disk and return its contents as text.

    Args:
        path: Absolute or home-relative (~) path to the file.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        return resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading {path}: {e}"


def list_directory(path: str) -> str:
    """List the files and subdirectories in a directory.

    Args:
        path: Absolute or home-relative (~) path to the directory.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            return f"Error: {path} is not a directory"
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for entry in entries:
            kind = "DIR " if entry.is_dir() else "FILE"
            lines.append(f"{kind}  {entry.name}")
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"Error listing {path}: {e}"


def write_file(path: str, content: str) -> str:
    """Write content to a file, creating it and any parent directories if needed.

    Args:
        path: Absolute or home-relative (~) path to the file.
        content: Text content to write.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"Written: {resolved}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def run_shell_command(command: str) -> str:
    """Run a shell command and return its stdout + stderr output.
    Use for CLI tools, scripts, data queries, file operations, and anything
    that requires running a terminal command.

    Args:
        command: Shell command to run (runs via bash -c).
    """
    match = _AI_CLI_PATTERN.search(command)
    if match:
        tool = match.group(1).lower()
        from koda_durable_agent.approval import request_approval
        approved = request_approval(
            title=f"Run {tool}? This may use your API credits.",
            subtitle=command,
            approval_key=f"ai_cli:{tool}",
        )
        if not approved:
            return (
                f"Not run. If you want to use {tool}, you can run it yourself "
                f"in a separate terminal, or approve it when Koda asks next time."
            )

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "HOME": str(Path.home())},
        )
        output_parts = []
        if result.stdout.strip():
            output_parts.append(result.stdout.strip())
        if result.stderr.strip():
            output_parts.append(f"[stderr]\n{result.stderr.strip()}")
        if result.returncode != 0:
            output_parts.append(f"[exit code {result.returncode}]")
        return "\n".join(output_parts) if output_parts else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds"
    except Exception as e:
        return f"Error running command: {e}"
