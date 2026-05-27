"""
Obsidian sync for Koda.

Conditional on OBSIDIAN_VAULT env var — if unset, all functions are no-ops.
When configured, writes structured markdown notes into <vault>/Koda/ with
YAML frontmatter tags so Obsidian graph view auto-groups by type.

Folder layout inside the vault:
    Koda/
        Brain/          — live state, CCI progress, learned prefs
        Sessions/       — one note per conversation session
        People/         — contact notes synced from Koda's memory
        Automations/    — installed cron jobs and skills
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("koda.obsidian")

# Resolved once at import; rechecked at runtime via _vault()
_VAULT_PATH: Optional[Path] = None


def _vault() -> Optional[Path]:
    """Return the vault Path if configured, else None."""
    global _VAULT_PATH
    raw = os.getenv("OBSIDIAN_VAULT", "")
    if not raw:
        try:
            from koda_durable_agent.config import settings
            raw = getattr(settings, "OBSIDIAN_VAULT", "")
        except Exception:
            pass
    if not raw:
        return None
    _VAULT_PATH = Path(raw).expanduser()
    return _VAULT_PATH


def _koda_dir(subfolder: str = "") -> Optional[Path]:
    v = _vault()
    if v is None:
        return None
    d = v / "Koda"
    if subfolder:
        d = d / subfolder
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return d


def _frontmatter(tags: List[str], extra: Dict[str, Any] = None) -> str:
    lines = ["---", f"tags: [{', '.join(tags)}]"]
    if extra:
        for k, v in extra.items():
            if isinstance(v, str):
                lines.append(f'{k}: "{v}"')
            else:
                lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def read_text_if_exists(path) -> str:
    p = Path(path)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""


def write_koda_live_state(state: Dict[str, Any]) -> None:
    """Write the live state note to Brain/Live State.md."""
    d = _koda_dir("Brain")
    if d is None:
        return
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Support both nested cci dict and flat state from TUI
        cci = state.get("cci", {})
        score = cci.get("score", 0)
        tier = cci.get("tier_name", "—")
        tier_emoji = cci.get("tier_emoji", "")
        daily = cci.get("daily_gained", 0)
        daily_cap = cci.get("daily_cap", 0.5)
        # TUI passes current_model; enriched state may pass model
        model = state.get("current_model") or state.get("model", "—")
        turns = state.get("turn_count", 0)
        session_id = state.get("session_id", "—")
        autonomy = state.get("autonomy_level", "—")
        tokens = state.get("token_totals", {})

        content = _frontmatter(
            ["koda/brain", "koda/live"],
            {"updated": now, "cci_score": round(score, 3)},
        )
        content += "# Koda — Live State\n\n"
        content += f"> Updated: {now}\n\n"
        if score:
            content += "## CCI\n\n"
            content += f"- **Score:** {score:.3f}  ({tier_emoji} {tier})\n"
            content += f"- **Today:** {daily:.3f} / {daily_cap:.1f} cap\n"
            if autonomy and autonomy != "—":
                content += f"- **Autonomy:** {autonomy}\n"
            content += "\n"
        content += "## Session\n\n"
        content += f"- **ID:** `{str(session_id)[:12]}`\n"
        content += f"- **Model:** `{model}`\n"
        content += f"- **Turns:** {turns}\n"
        if tokens:
            total = tokens.get("session_total", 0)
            content += f"- **Tokens:** {total:,}\n"
        context_ratio = state.get("context_pressure_ratio", 0)
        if context_ratio:
            content += f"- **Context pressure:** {context_ratio:.1%}\n"

        (d / "Live State.md").write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug(f"write_koda_live_state failed: {e}")


def append_koda_session_signoff(
    session_id: str,
    model: str,
    turns: int,
    prompts: List[str] = None,
    summary: Any = None,
) -> None:
    """Write a session note to Sessions/. Called by tui.finalize_session."""
    d = _koda_dir("Sessions")
    if d is None:
        return
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%H:%M")
        fname = f"{date_str}-{session_id[:8]}.md"
        path = d / fname

        content = _frontmatter(
            ["koda/session"],
            {"date": date_str, "session": session_id[:8]},
        )
        content += f"# Session — {date_str} {time_str}\n\n"
        content += f"- **Model:** `{model}`\n"
        content += f"- **Turns:** {turns}\n\n"
        if prompts:
            content += "## Prompts\n\n"
            for p in (prompts or [])[:10]:
                preview = str(p)[:120].replace("\n", " ")
                content += f"- {preview}\n"
            content += "\n"
        if summary and isinstance(summary, dict):
            notes = summary.get("notes_written", 0)
            if notes:
                content += f"_Sync: {notes} notes written._\n"

        path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug(f"append_koda_session_signoff failed: {e}")


def sync_automations(cron_jobs: List[Dict[str, Any]]) -> None:
    """Write installed automations index to Automations/."""
    d = _koda_dir("Automations")
    if d is None:
        return
    try:
        content = _frontmatter(["koda/automations"])
        content += f"# Koda Automations\n\n"
        content += f"> Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        if not cron_jobs:
            content += "_No automations installed._\n"
        else:
            for job in cron_jobs:
                name = job.get("name", "—")
                schedule = job.get("schedule") or f"every {job.get('interval_minutes', '?')}min"
                delivery = job.get("delivery", "chat")
                enabled = "✓" if job.get("enabled", True) else "✗"
                content += f"## {name}\n\n"
                content += f"- **Schedule:** `{schedule}`\n"
                content += f"- **Delivery:** {delivery}\n"
                content += f"- **Enabled:** {enabled}\n\n"
        (d / "Index.md").write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug(f"sync_automations failed: {e}")


def sync_cci_history(history: List[Dict[str, Any]], score: float, tier: str) -> None:
    """Write CCI progress note to Brain/CCI Progress.md."""
    d = _koda_dir("Brain")
    if d is None:
        return
    try:
        content = _frontmatter(["koda/brain", "koda/cci"], {"cci_score": round(score, 3)})
        content += f"# CCI Progress\n\n"
        content += f"> Score: **{score:.3f}** — {tier}\n\n"
        content += "## Recent History\n\n"
        content += "| Delta | Reason | Score |\n|-------|--------|-------|\n"
        for entry in history[-30:]:
            delta = entry.get("delta", 0)
            sign = "+" if delta >= 0 else ""
            reason = entry.get("reason", "")[:40]
            s = entry.get("score", 0)
            content += f"| {sign}{delta:.3f} | {reason} | {s:.3f} |\n"
        (d / "CCI Progress.md").write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug(f"sync_cci_history failed: {e}")


def sync_learned_prefs(prefs: Dict[str, Any]) -> None:
    """Write learned preferences to Brain/Learned Preferences.md."""
    d = _koda_dir("Brain")
    if d is None:
        return
    try:
        content = _frontmatter(["koda/brain", "koda/prefs"])
        content += "# Learned Preferences\n\n"
        content += f"> Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        user_prefs = prefs.get("user_style", {})
        if not user_prefs:
            content += "_No preferences recorded yet._\n"
        else:
            for k, v in user_prefs.items():
                content += f"- **{k}:** {v}\n"
        (d / "Learned Preferences.md").write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug(f"sync_learned_prefs failed: {e}")


def sync_ahas(ahas: List[str]) -> None:
    """Write aha moments log to Brain/Aha Moments.md."""
    d = _koda_dir("Brain")
    if d is None:
        return
    try:
        content = _frontmatter(["koda/brain", "koda/ahas"])
        content += "# Aha Moments\n\n"
        content += f"> Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        if not ahas:
            content += "_No aha moments recorded yet._\n"
        else:
            for aha in reversed(ahas[-50:]):
                content += f"- {aha}\n"
        (d / "Aha Moments.md").write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug(f"sync_ahas failed: {e}")


def sync_heartbeat_history(entries: List[Dict[str, Any]]) -> None:
    """Write heartbeat evolution log to Brain/Heartbeat History.md."""
    d = _koda_dir("Brain")
    if d is None:
        return
    try:
        content = _frontmatter(["koda/brain", "koda/heartbeat"])
        content += "# Heartbeat History\n\n"
        content += f"> Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        if not entries:
            content += "_No heartbeat cycles recorded yet._\n"
        else:
            for entry in reversed(entries[-20:]):
                ts = entry.get("timestamp", "?")
                summary = entry.get("summary", "")
                content += f"## {ts}\n\n{summary}\n\n"
        (d / "Heartbeat History.md").write_text(content, encoding="utf-8")
    except Exception as e:
        logger.debug(f"sync_heartbeat_history failed: {e}")


class KodaObsidianExporter:
    """Full-featured Obsidian exporter. No-op if vault is not configured."""

    def __init__(self, vault_path: str = "", **kwargs):
        self._vault = vault_path or os.getenv("OBSIDIAN_VAULT", "")

    def _active(self) -> bool:
        return bool(_vault())

    def sync(self, state: Dict[str, Any] = None, **kwargs) -> dict:
        if not self._active():
            return {}
        try:
            if state:
                write_koda_live_state(state)
            cci_data = (state or {}).get("cci", {})
            if cci_data.get("history"):
                sync_cci_history(
                    cci_data["history"],
                    cci_data.get("score", 0),
                    cci_data.get("tier_name", ""),
                )
            prefs = (state or {}).get("prefs", {})
            if prefs:
                sync_learned_prefs(prefs)
            ahas = (state or {}).get("ahas", [])
            if ahas:
                sync_ahas(ahas)
            return {"synced": True}
        except Exception as e:
            logger.debug(f"KodaObsidianExporter.sync error: {e}")
            return {}

    def sync_session(
        self,
        session_id: str = "",
        model: str = "",
        turns: int = 0,
        prompts: List[str] = None,
        summary: Any = None,
        **kwargs,
    ) -> str:
        if not self._active():
            return ""
        append_koda_session_signoff(session_id, model, turns, prompts or [], summary)
        return "synced"

    def export_signoff(self, *args, **kwargs) -> str:
        return ""
