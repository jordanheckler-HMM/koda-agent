"""
Obsidian sync — stub for standalone koda-agent.
The full Obsidian integration is available in the extended version.
"""
from pathlib import Path
from typing import Any, Dict, Optional


class KodaObsidianExporter:
    def __init__(self, *args, **kwargs):
        pass

    def sync_session(self, *args, **kwargs) -> str:
        return ""

    def export_signoff(self, *args, **kwargs) -> str:
        return ""


def append_koda_session_signoff(*args, **kwargs) -> None:
    pass


def read_text_if_exists(path) -> str:
    p = Path(path)
    if p.exists():
        try:
            return p.read_text()
        except Exception:
            pass
    return ""


def write_koda_live_state(*args, **kwargs) -> None:
    pass
