"""
Koda CCI Leaderboard — powered by GitHub Issues on jheckler/koda-agent.

Each user submits their score by filing a GitHub issue with the 'leaderboard'
label. The leaderboard is built by reading all such issues. Users need only
a public_repo-scoped GitHub token — the same one used for bug reports.
"""
import json
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("koda.leaderboard")

_REPO = "jheckler/koda-agent"
_LABEL = "leaderboard"
_API_BASE = "https://api.github.com"

_TIERS = [
    ("Guardian", "🐻", 10.0),
    ("Tracker",  "🦅", 5.0),
    ("Ranger",   "🌲", 2.5),
    ("Scout",    "🐾", 1.0),
    ("Cub",      "🐣", 0.0),
]


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


def _tier_for(score: float) -> tuple:
    for name, emoji, threshold in _TIERS:
        if score >= threshold:
            return name, emoji
    return "Cub", "🐣"


def _gh_request(method: str, path: str, data: dict = None, token: str = None) -> dict:
    url = f"{_API_BASE}{path}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "koda-agent",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    payload = json.dumps(data).encode() if data else None
    if payload:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def submit_cci_score(handle: str = "") -> str:
    """Submit your CCI score to the Koda leaderboard.

    Posts your current score as a public GitHub issue tagged 'leaderboard'.
    Your score and chosen handle will be visible to everyone.
    Requires GITHUB_TOKEN in ~/.koda/.env.

    Args:
        handle: The name to display on the leaderboard (e.g. your first name or a nickname).
                Leave blank to appear as 'anonymous'.
    """
    token = _get_token()
    if not token:
        return (
            "GitHub token not configured — can't submit to the leaderboard.\n"
            "Add GITHUB_TOKEN to ~/.koda/.env (needs public_repo scope).\n"
            "Get one at: https://github.com/settings/tokens"
        )

    try:
        from koda_durable_agent.cci import CCITracker
        cci = CCITracker()
        summary = cci.summary()
        score = summary["score"]
        tier_name = summary["tier_name"]
        tier_emoji = summary["tier_emoji"]
        sessions = summary.get("sessions", 0)
        turns = summary.get("turns", 0)
    except Exception as e:
        return f"Couldn't read CCI data: {e}"

    display = handle.strip() or "anonymous"
    date_str = datetime.now().strftime("%Y-%m-%d")

    title = f"[leaderboard] {display} — {tier_emoji} {tier_name} — {score:.2f} CCI"
    body = (
        f"**Handle:** {display}\n"
        f"**Score:** {score:.3f}\n"
        f"**Tier:** {tier_emoji} {tier_name}\n"
        f"**Sessions:** {sessions}\n"
        f"**Turns:** {turns}\n"
        f"**Submitted:** {date_str}\n\n"
        f"---\n_Submitted from Koda. [What is CCI?](https://github.com/jheckler/koda-agent#cci)_"
    )

    try:
        # Check if this handle already has a submission — update it by closing old + creating new
        existing = _find_existing_submission(display, token)
        if existing:
            # Close the old one
            _gh_request(
                "PATCH",
                f"/repos/{_REPO}/issues/{existing}",
                {"state": "closed"},
                token,
            )

        issue = _gh_request(
            "POST",
            f"/repos/{_REPO}/issues",
            {"title": title, "body": body, "labels": [_LABEL]},
            token,
        )
        url = issue.get("html_url", "")
        number = issue.get("number", "?")
        return (
            f"{tier_emoji} Score submitted! Issue #{number}\n"
            f"Handle: {display}  |  Score: {score:.3f}  |  Tier: {tier_name}\n"
            f"View leaderboard: https://github.com/{_REPO}/issues?q=label%3Aleaderboard+is%3Aopen\n"
            f"Your entry: {url}"
        )
    except Exception as e:
        logger.warning(f"Leaderboard submit failed: {e}")
        return f"Submission failed: {e}"


def _find_existing_submission(handle: str, token: str) -> Optional[int]:
    """Return issue number of an existing open leaderboard entry for this handle, or None."""
    try:
        query = urllib.parse.quote(f"[leaderboard] {handle}")
        issues = _gh_request(
            "GET",
            f"/repos/{_REPO}/issues?labels={_LABEL}&state=open&per_page=100",
            token=token,
        )
        if isinstance(issues, list):
            for issue in issues:
                if f"[leaderboard] {handle}" in issue.get("title", ""):
                    return issue["number"]
    except Exception:
        pass
    return None


def view_leaderboard(top: int = 10) -> str:
    """Show the current Koda CCI leaderboard.

    Reads the top scores submitted by Koda users worldwide.
    No login required to view.

    Args:
        top: How many entries to show (default 10, max 30).
    """
    top = min(top, 30)
    try:
        issues = _gh_request(
            "GET",
            f"/repos/{_REPO}/issues?labels={_LABEL}&state=open&per_page=100",
        )
        if not isinstance(issues, list):
            return "Couldn't fetch leaderboard — GitHub API returned unexpected data."
        if not issues:
            return (
                "The leaderboard is empty — be the first to submit!\n"
                "Ask Koda: 'Submit my score to the leaderboard'"
            )

        entries = []
        for issue in issues:
            title = issue.get("title", "")
            body = issue.get("body", "")
            score = _parse_score_from_body(body)
            handle = _parse_handle_from_title(title)
            tier_name, tier_emoji = _tier_for(score)
            if score >= 0 and handle:
                entries.append((score, handle, tier_name, tier_emoji))

        entries.sort(key=lambda x: x[0], reverse=True)
        entries = entries[:top]

        lines = [f"{'':─<48}", " 🏆  Koda CCI Leaderboard", f"{'':─<48}"]
        for i, (score, handle, tier_name, tier_emoji) in enumerate(entries, 1):
            rank = f"#{i:<3}"
            lines.append(f"  {rank}  {tier_emoji}  {handle:<20}  {score:.3f}  {tier_name}")
        lines.append(f"{'':─<48}")
        lines.append(f"  Full board: github.com/{_REPO}/issues?q=label%3Aleaderboard")
        lines.append(f"  Submit: ask Koda 'post my score to the leaderboard'")
        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"Leaderboard fetch failed: {e}")
        return (
            f"Couldn't load leaderboard ({e}).\n"
            f"View it directly: https://github.com/{_REPO}/issues?q=label%3Aleaderboard"
        )


def _parse_score_from_body(body: str) -> float:
    for line in body.splitlines():
        if line.startswith("**Score:**"):
            try:
                return float(line.split("**Score:**")[1].strip())
            except ValueError:
                pass
    return -1.0


def _parse_handle_from_title(title: str) -> str:
    # Format: "[leaderboard] handle — emoji Tier — score CCI"
    try:
        rest = title.split("[leaderboard]")[1].strip()
        handle = rest.split("—")[0].strip()
        return handle
    except (IndexError, AttributeError):
        return ""
