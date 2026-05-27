"""
Interactive approval prompt for Koda.

Shows an arrow-key selector when Koda needs explicit user permission
before taking an action (e.g. calling an external AI CLI).

Options:
  Yes               — allow this one time
  No                — deny this time
  Yes, never ask    — allow and remember forever for this tool/action
  No, never ask     — deny and remember forever (always block silently)

Approvals are persisted in ~/.koda/koda_prefs.json under 'approvals'.
"""
import json
import os
import sys
import tty
import termios
from pathlib import Path
from typing import Optional

_PREFS_PATH = Path.home() / ".koda" / "koda_prefs.json"

# ANSI
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_UP     = "\033[1A"
_CLEAR  = "\033[2K"


def _load_approvals() -> dict:
    try:
        if _PREFS_PATH.exists():
            data = json.loads(_PREFS_PATH.read_text())
            return data.get("approvals", {})
    except Exception:
        pass
    return {}


def _save_approval(key: str, decision: str) -> None:
    """Persist a 'never ask again' decision. decision is 'allow' or 'deny'."""
    try:
        _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if _PREFS_PATH.exists():
            try:
                data = json.loads(_PREFS_PATH.read_text())
            except Exception:
                pass
        approvals = data.setdefault("approvals", {})
        approvals[key] = decision
        _PREFS_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _read_key() -> str:
    """Read a single keypress from stdin. Returns 'up', 'down', 'enter', or the char."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "up"
                if ch3 == "B":
                    return "down"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            return "ctrl_c"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _render(title: str, subtitle: str, options: list, selected: int) -> None:
    icons = ["○", "○", "○", "○"]
    icons[selected] = "●"
    colors = [_GREEN, _RED, _GREEN, _RED]

    lines = [
        f"\n  {_BOLD}{_YELLOW}⚡ Approval Required{_RESET}",
        f"  {title}",
    ]
    if subtitle:
        lines.append(f"  {_DIM}{subtitle[:100]}{_RESET}")
    lines.append("")
    for i, (icon, opt) in enumerate(zip(icons, options)):
        color = colors[i] if i == selected else _DIM
        arrow = "▶" if i == selected else " "
        lines.append(f"  {arrow} {color}{icon}  {opt}{_RESET}")
    lines.append(f"\n  {_DIM}↑↓ to move  Enter to confirm{_RESET}\n")
    print("\n".join(lines), end="", flush=True)


def _clear_lines(n: int) -> None:
    for _ in range(n):
        sys.stdout.write(_UP + _CLEAR)
    sys.stdout.flush()


def request_approval(
    title: str,
    subtitle: str = "",
    approval_key: str = "",
) -> bool:
    """
    Show an interactive arrow-key approval prompt.

    Returns True if the user approved (yes or yes-never-ask),
    False if denied (no or no-never-ask).

    If not running in a TTY (background job, piped output), returns False.

    Args:
        title:        Short description of what's being approved.
        subtitle:     The actual command or detail (shown dimmed).
        approval_key: Stable key for persisting 'never ask again' decisions.
                      Use something like 'ai_cli:claude' or 'shell:rm_rf'.
    """
    # Background / non-interactive — deny silently
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False

    # Check persisted decision
    if approval_key:
        saved = _load_approvals().get(approval_key)
        if saved == "allow":
            return True
        if saved == "deny":
            return False

    options = [
        "Yes — allow this time",
        "No — not right now",
        "Yes — and never ask again for this",
        "No  — and never allow this",
    ]

    selected = 0
    n_render_lines = 4 + len(options) + 3  # approx lines drawn

    _render(title, subtitle, options, selected)

    try:
        while True:
            key = _read_key()
            if key == "up":
                selected = (selected - 1) % len(options)
            elif key == "down":
                selected = (selected + 1) % len(options)
            elif key == "enter":
                _clear_lines(n_render_lines)
                choice = selected
                if choice == 0:   # yes once
                    print(f"  {_GREEN}✓ Approved{_RESET}\n", flush=True)
                    return True
                elif choice == 1: # no once
                    print(f"  {_RED}✗ Denied{_RESET}\n", flush=True)
                    return False
                elif choice == 2: # yes, never ask
                    if approval_key:
                        _save_approval(approval_key, "allow")
                    print(f"  {_GREEN}✓ Approved — won't ask again{_RESET}\n", flush=True)
                    return True
                elif choice == 3: # no, never allow
                    if approval_key:
                        _save_approval(approval_key, "deny")
                    print(f"  {_RED}✗ Denied — won't allow this in future{_RESET}\n", flush=True)
                    return False
            elif key == "ctrl_c":
                _clear_lines(n_render_lines)
                print(f"  {_RED}✗ Cancelled{_RESET}\n", flush=True)
                return False
            else:
                continue
            _clear_lines(n_render_lines)
            _render(title, subtitle, options, selected)
    except Exception:
        # If terminal manipulation fails for any reason, fall back to plain input
        return _plain_approval(title, subtitle)


def _plain_approval(title: str, subtitle: str) -> bool:
    """Plain text fallback for environments where raw mode isn't available."""
    print(f"\n  ⚡ Approval Required: {title}")
    if subtitle:
        print(f"  {subtitle[:100]}")
    answer = input("  Allow? [y/N]: ").strip().lower()
    return answer in ("y", "yes")
