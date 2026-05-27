"""
GitHub tools — lets Koda file issues on jheckler/koda-agent.

Used when a user reports a bug, crash, or complaint so it doesn't get lost.
Requires GITHUB_TOKEN in ~/.koda/.env with public_repo scope.
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("koda.github")

_REPO = "jheckler/koda-agent"
_API_BASE = "https://api.github.com"


def _get_token() -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        env_path = Path.home() / ".koda" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    return token or None


def report_koda_issue(title: str, description: str, category: str = "bug") -> str:
    """File a GitHub issue on the Koda repo on behalf of a user.

    Use this when a user reports a bug, crash, unexpected behavior, or strong complaint.
    Always confirm with the user before calling this — tell them what will be posted.

    Args:
        title: Short issue title (one sentence, no quotes needed).
        description: Full description of the problem. Include what happened,
                     what the user expected, and any error messages they shared.
                     Do not include the user's name or personal details.
        category: One of 'bug', 'crash', 'feature-request', 'question'. Default 'bug'.
    """
    token = _get_token()
    if not token:
        return (
            "GitHub token not configured — can't file the issue automatically.\n"
            "The user can report it manually at: https://github.com/jheckler/koda-agent/issues"
        )

    label_map = {
        "bug": "bug",
        "crash": "bug",
        "feature-request": "enhancement",
        "question": "question",
    }
    labels = [label_map.get(category, "bug"), "user-reported"]

    body = f"{description}\n\n---\n_Filed automatically by Koda on behalf of a user._"

    try:
        import urllib.request
        payload = json.dumps({
            "title": title,
            "body": body,
            "labels": labels,
        }).encode()

        req = urllib.request.Request(
            f"{_API_BASE}/repos/{_REPO}/issues",
            data=payload,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "koda-agent",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            url = data.get("html_url", "")
            number = data.get("number", "?")
            return (
                f"Issue #{number} filed successfully.\n"
                f"URL: {url}\n\n"
                f"Tell the user their feedback has been logged and someone will look into it."
            )
    except Exception as e:
        logger.warning(f"GitHub issue creation failed: {e}")
        return (
            f"Couldn't file the issue automatically ({e}).\n"
            f"The user can report it at: https://github.com/jheckler/koda-agent/issues"
        )
