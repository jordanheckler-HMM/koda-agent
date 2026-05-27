#!/usr/bin/env python3
import os
import sys
import asyncio
import logging
import json
import shutil
import atexit
import readline
import datetime
import random
import subprocess
import tty
import termios
import select as _select_mod
import re as _re_mod
from collections import deque
from pathlib import Path
from typing import Optional, List, Dict, Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from koda_durable_agent.runtime import bootstrap_pythonpath, ensure_session_state

bootstrap_pythonpath()

# Ensure standard stdout encoding is UTF-8 to prevent rich/unicode encoding errors
sys.stdout.reconfigure(encoding='utf-8')

from koda_durable_agent.config import settings
from koda_durable_agent.obsidian_sync import (
    KodaObsidianExporter,
    append_koda_session_signoff,
    read_text_if_exists,
    write_koda_live_state,
)
from koda_durable_agent.brain import (
    get_agent_config,
    is_gemini_model as is_supported_model,
    normalize_model_name,
    get_koda_status,
)

# Antigravity / Gemini path — optional
try:
    from google.antigravity import Agent
    from google.antigravity.hooks import hooks
    from google.antigravity.types import Text as AntigravityText, Thought, ToolCall, ToolResult
    _HAS_ANTIGRAVITY = True
except ImportError:
    _HAS_ANTIGRAVITY = False
    Agent = None
    AntigravityText = Thought = ToolCall = ToolResult = None

SUPPORTED_MODEL_ERROR = (
    "Koda's Gemini runtime requires google-antigravity. "
    "Install it with: pip install koda-agent[gemini]"
)
from koda_durable_agent.cci import CCITracker
from koda_durable_agent.heartbeat import (
    HeartbeatRunner, HEARTBEAT_LOG, load_evolve_queue, save_evolve_queue,
    queue_evolution_item, apply_learned_preference,
)
from koda_durable_agent.sleep_cycle import SleepCycleRunner, DREAMS_PATH
from koda_durable_agent.cron_runner import (
    KodaCronRunner, KodaCronJob,
    load_cron_jobs, save_cron_jobs, add_cron_job, remove_cron_job, update_cron_job,
)

# Force-silence root logging, redirect stderr/stdout logger clutter to file,
# and monkeypatch basicConfig to make sure imported modules cannot corrupt it.
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

log_file = str(Path.home() / ".koda" / "koda.log")
try:
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.root.addHandler(file_handler)
except Exception:
    pass

logging.root.setLevel(logging.WARNING)

def mock_basic_config(*args, **kwargs):
    pass
logging.basicConfig = mock_basic_config

# Silence specific loud standard libraries and harness loggers
for logger_name in ["google.antigravity", "websockets", "urllib3", "httpx", "httpcore", "h11", "koda"]:
    l = logging.getLogger(logger_name)
    l.setLevel(logging.WARNING)
    l.propagate = False

logger = logging.getLogger("koda.tui")

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.text import Text
    from rich.spinner import Spinner
    from rich.rule import Rule
    from rich.columns import Columns
    from rich.box import ROUNDED, SIMPLE
except ImportError:
    print("Error: The 'rich' library is required to run the Koda TUI.")
    print("Please make sure you install it using the project's virtual environment.")
    sys.exit(1)

# Initialize global rich console
console = Console()

MODEL_CONTEXT_WINDOWS = {
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-3.5-flash": 1_000_000,
    "gemini-flash": 1_000_000,
    "gemini-pro": 2_000_000,
    "gemini-flash-lite": 300_000,
}

class KodaTUISession:
    def __init__(self):
        self.current_model = settings.PRIMARY_MODEL
        self.session_id = os.getenv("KODA_SESSION_ID") or self._build_default_session_id()
        self.agent = None
        self.sync_task = None
        self.telegram_task = None
        self._agent_lock = asyncio.Lock()
        self.total_prompt_tokens = 0
        self.total_candidates_tokens = 0
        self.total_thoughts_tokens = 0
        self.last_prompt_tokens = 0
        self.last_candidates_tokens = 0
        self.last_thoughts_tokens = 0
        self.available_models = []
        self.configured_provider_models: Dict[str, List[str]] = {}
        self.last_seen_step_index = -1
        self.turn_count = 0
        self.slash_action_count = 0
        self.session_signed_off = False
        self.last_sync_summary: dict[str, Any] = {}
        self.recent_prompts: List[str] = []
        self.last_turn_at: Optional[str] = None
        self._or_messages: List[Dict[str, str]] = []  # OpenRouter conversation history
        self.active_persona: Optional[str] = None  # None = default Koda soul
        self.cci = CCITracker()
        self._last_known_tier: str = self.cci.tier_name()
        self._pending_heartbeat_notice: Optional[str] = None
        self._pending_sleep_notice: Optional[str] = None
        self._session_tool_count: int = 0
        self._session_error_count: int = 0
        self._heartbeat_runner: Optional[HeartbeatRunner] = None
        self._sleep_runner: Optional[SleepCycleRunner] = None
        self._cron_runner: Optional[KodaCronRunner] = None
        self._pending_cron_notice: Optional[str] = None
        self._update_available: bool = False

        # Load persistent user prefs (default model, etc.) before model discovery
        self._prefs: dict = self._load_prefs()
        if self._prefs.get("default_model"):
            self.current_model = self._prefs["default_model"]

        # Load available models
        self._load_available_models()
        # Ensure session trajectory is touched
        self._init_session_trajectory()
        # Build instance-level command palette so Koda's custom skills can extend it
        self._COMMAND_PALETTE = list(self.__class__._COMMAND_PALETTE)
        self._reload_skill_palette()

        # Setup advanced readline autocomplete and persistent command history
        self._setup_readline()
        self._write_live_state("session_boot")

    def _reload_skill_palette(self) -> None:
        """Append Koda's saved custom skills to the instance command palette."""
        from koda_durable_agent.koda_skill_tools import _load as _load_skills
        base = list(self.__class__._COMMAND_PALETTE)
        for s in _load_skills():
            cmd = s.get("command", "")
            desc = s.get("description", "")
            if cmd and desc:
                base.append((cmd, desc))
        self._COMMAND_PALETTE = base

    def _build_default_session_id(self) -> str:
        """Create a fresh session ID for each launch to avoid replaying old trajectories."""
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"{random.randint(0, 0xFFFF):04x}"
        return f"koda-{stamp}-{suffix}"

    def _get_heartbeat_session_data(self) -> dict:
        return {
            "turn_count": self.turn_count,
            "recent_prompts": self.recent_prompts,
            "tool_count": self._session_tool_count,
            "error_count": self._session_error_count,
        }

    def _queue_heartbeat_notice(self, notice: str) -> None:
        # Stored for Telegram delivery only — never printed to the TUI mid-chat.
        self._pending_heartbeat_notice = notice

    def _queue_sleep_notice(self, notice: str) -> None:
        self._pending_sleep_notice = notice

    def _queue_cron_notice(self, notice: str) -> None:
        self._pending_cron_notice = notice

    def _send_telegram_outbound(self, text: str) -> None:
        """Push a message directly into the Telegram outbound queue."""
        try:
            from koda_durable_agent.gateway import queue_outbound
            from koda_durable_agent.config import settings
            chat_id = settings.TELEGRAM_CHAT_ID
            if chat_id:
                queue_outbound(str(chat_id), text)
            else:
                logger.warning("_send_telegram_outbound: no TELEGRAM_CHAT_ID configured")
        except Exception as e:
            logger.warning(f"_send_telegram_outbound failed: {e}")

    def _cron_resolve_job(self, ref: str) -> Optional[KodaCronJob]:
        """Resolve a job by numeric position (1-based) or ID prefix."""
        jobs = load_cron_jobs()
        try:
            idx = int(ref) - 1
            if 0 <= idx < len(jobs):
                return jobs[idx]
        except ValueError:
            for job in jobs:
                if job.id.startswith(ref) or job.name.lower() == ref.lower():
                    return job
        return None

    async def _check_tier_up(self) -> None:
        """Print tier-up ceremony if CCI has crossed a tier boundary."""
        current_tier = self.cci.tier_name()
        if current_tier == self._last_known_tier:
            return
        self._last_known_tier = current_tier
        _, tier_emoji, tier_color = self.cci.tier_info()
        _TIER_MESSAGES = {
            "Bear":        "The forest grows deeper. The bear learns to listen.",
            "Kodiak":      "Power comes from understanding. Koda adapts.",
            "Spirit Bear": "The apex is not a destination. It is a way of moving.",
        }
        msg = _TIER_MESSAGES.get(current_tier, "Koda evolves.")
        console.print(Panel(
            f"[bold {tier_color}]{tier_emoji} TIER UP — Koda evolved to {current_tier}[/]\n\n"
            f"[{tier_color}]CCI: {self.cci.score:.2f}[/]  ·  "
            f"Autonomy unlocked: {self.cci.autonomy_level()}\n"
            f"[dim italic]{msg}[/]",
            border_style=tier_color,
            padding=(0, 2),
        ))
        console.print()

    @staticmethod
    def _tiers_at_or_above(tier: str) -> list:
        order = ["Cub", "Bear", "Kodiak", "Spirit Bear"]
        idx = order.index(tier) if tier in order else 0
        return order[idx:]

    def _evolve_show_queue(self) -> None:
        queue = load_evolve_queue()
        pending = [q for q in queue if q["status"] == "pending"]
        if not pending:
            console.print("\n[dim]No pending evolution items. Heartbeat will add items as Koda detects improvements.[/]\n")
            return
        tbl = Table(title="🧬 Evolution Queue", box=ROUNDED, border_style="#a78bfa")
        tbl.add_column("#", width=4)
        tbl.add_column("Type", width=10)
        tbl.add_column("Content")
        tbl.add_column("Requires", width=14)
        tbl.add_column("Source", width=18)
        tier_colors = {"Bear": "#a78bfa", "Kodiak": "#7ec8a0", "Spirit Bear": "#ffd700"}
        for i, item in enumerate(pending, 1):
            tc = tier_colors.get(item.get("requires_tier", ""), "dim")
            can = self.cci.tier_name() in self._tiers_at_or_above(item.get("requires_tier", "Bear"))
            marker = "✓" if can else "⏳"
            tbl.add_row(
                f"{marker}{i}",
                item.get("type", "?"),
                item.get("content", "")[:55],
                f"[{tc}]{item.get('requires_tier', '?')}[/]",
                item.get("source", "?"),
            )
        console.print()
        console.print(tbl)
        console.print("\n[dim]/evolve approve <#>  /evolve reject <#>  /evolve rollback soul  /evolve learned[/]\n")

    async def _evolve_approve(self, num_str: str) -> None:
        queue = load_evolve_queue()
        pending = [q for q in queue if q["status"] == "pending"]
        try:
            idx = int(num_str) - 1
            item = pending[idx]
        except (ValueError, IndexError):
            console.print(f"\n[bold yellow]No item #{num_str} in queue.[/]\n")
            return
        item["status"] = "approved"
        if item.get("type") in ("learned", "config"):
            apply_learned_preference(item["content"])
            self.cci.add_delta(0.5, f"evolve_approved: {item['content'][:40]}")
        save_evolve_queue(queue)
        console.print(f"\n[bold #7ec8a0]✓ Approved and applied: {item['content'][:60]}[/]\n")

    async def _evolve_reject(self, num_str: str) -> None:
        queue = load_evolve_queue()
        pending = [q for q in queue if q["status"] == "pending"]
        try:
            idx = int(num_str) - 1
            item = pending[idx]
        except (ValueError, IndexError):
            console.print(f"\n[bold yellow]No item #{num_str} in queue.[/]\n")
            return
        item["status"] = "rejected"
        save_evolve_queue(queue)
        console.print(f"\n[dim]Rejected: {item['content'][:60]}[/]\n")

    async def _evolve_rollback_soul(self) -> None:
        soul_ver_dir = CCITracker.SOUL_VER
        soul_path = Path.home() / ".koda" / "Soul.md"
        if not soul_ver_dir.exists():
            console.print("\n[bold yellow]No soul versions found — nothing to roll back.[/]\n")
            return
        versions = sorted(soul_ver_dir.glob("Soul_v*.md"))
        if not versions:
            console.print("\n[bold yellow]No soul versions found.[/]\n")
            return
        latest = versions[-1]
        soul_path.write_text(latest.read_text())
        console.print(f"\n[bold #7ec8a0]✓ Soul rolled back to {latest.name}[/]\n")

    def _evolve_show_learned(self) -> None:
        from koda_durable_agent.heartbeat import LEARNED_PATH
        if not LEARNED_PATH.exists() or not LEARNED_PATH.read_text().strip():
            console.print("\n[dim]No learned preferences yet. Run a heartbeat first.[/]\n")
            return
        content = LEARNED_PATH.read_text()
        console.print()
        console.print(Panel(content, title="🧠 Learned Preferences", border_style="#7ec8a0"))
        console.print("[dim]Edit file directly: ~/.koda/koda_learned.md[/]\n")

    _PREFS_PATH = Path.home() / ".koda" / "koda_prefs.json"

    def _load_prefs(self) -> dict:
        try:
            if self._PREFS_PATH.exists():
                import json as _json
                return _json.loads(self._PREFS_PATH.read_text())
        except Exception:
            pass
        return {}

    def _save_prefs(self) -> None:
        try:
            import json as _json
            self._PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._PREFS_PATH.write_text(_json.dumps(self._prefs, indent=2))
        except Exception as e:
            logger.warning(f"Could not save prefs: {e}")

    def _set_default_model(self, model: str) -> None:
        self._prefs["default_model"] = model
        self._save_prefs()

    def _load_available_models(self):
        """Discovers all models Koda can run: Gemini via Antigravity, OpenRouter via openai SDK."""
        self.provider_models = {}
        self.configured_provider_models = {}
        gemini_models = ["gemini-2.5-flash", "gemini-3.5-flash"]
        openrouter_models: List[str] = []
        ollama_cloud_models: List[str] = []

        # Load OpenRouter models from config if available
        openrouter_config = Path.home() / ".koda" / "openrouter_models.json"
        if openrouter_config.exists():
            try:
                data = json.loads(openrouter_config.read_text())
                for mid in data.get("models", []):
                    openrouter_models.append(mid)
            except Exception as e:
                logger.warning(f"Failed to parse openrouter_models.json: {e}")

        self.provider_models["Google Gemini"] = sorted(dict.fromkeys(gemini_models))
        if openrouter_models:
            self.provider_models["OpenRouter"] = sorted(dict.fromkeys(openrouter_models))
        if ollama_cloud_models:
            self.provider_models["Ollama Cloud"] = sorted(dict.fromkeys(ollama_cloud_models))
        self.available_models = [
            m for models in self.provider_models.values() for m in models
        ]
        for provider, models in list(self.configured_provider_models.items()):
            self.configured_provider_models[provider] = sorted(dict.fromkeys(models))

    def _classify_provider(self, model_id: str) -> Optional[str]:
        """Maps a model ID string to a display provider name. Returns None for local Ollama models."""
        prefixes = {
            "openrouter/": "OpenRouter",
            "openai/": "OpenAI / Codex",
            "anthropic/": "Anthropic",
            "claude-cli/": "Claude Code",
            "google/": "Google Gemini",
        }
        for prefix, name in prefixes.items():
            if model_id.startswith(prefix):
                return name

        # Ollama cloud models (e.g. ollama/qwen3.5:cloud) go under Ollama Cloud
        if model_id.startswith("ollama/"):
            tag = model_id.split(":")[-1] if ":" in model_id else ""
            if tag == "cloud":
                return "Ollama Cloud"
            return None  # Skip stale local ollama entries from config
        return None

    def _current_provider_label(self) -> str:
        if self.current_model.startswith("gemini-"):
            return "Google Gemini"
        provider = self._classify_provider(self.current_model)
        return provider or "Unknown"

    def _session_total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_candidates_tokens + self.total_thoughts_tokens

    def _context_window_tokens(self) -> int:
        return MODEL_CONTEXT_WINDOWS.get(self.current_model, 1_000_000)

    def _context_pressure(self) -> float:
        window = self._context_window_tokens()
        if window <= 0:
            return 0.0
        return min(1.0, self._session_total_tokens() / window)

    def _format_token_count(self, value: int) -> str:
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)

    def _render_meter(self, ratio: float, width: int = 18, full_style: str = "#fa8072") -> str:
        ratio = max(0.0, min(1.0, ratio))
        filled = max(0, min(width, int(round(ratio * width))))
        empty = width - filled
        full = "█" * filled
        rest = "░" * empty
        return f"[{full_style}]{full}[/][dim #7ec8a0]{rest}[/]"

    def _provider_lane_summary(self) -> List[str]:
        lines: List[str] = []
        preferred = ["Google Gemini", "OpenRouter", "Ollama Cloud", "OpenAI / Codex", "Claude Code", "Anthropic"]
        for provider in preferred:
            if provider in self.configured_provider_models:
                count = len(self.configured_provider_models[provider])
                status = "live now" if provider == "Google Gemini" else "adapter needed"
                lines.append(f" ● [bold #8b5a2b]{provider}:[/] {count} paths configured  [dim #7ec8a0]{status}[/]")
        return lines

    def _record_usage_snapshot(self, usage: Any) -> None:
        prompt = getattr(usage, 'prompt_token_count', 0) or 0
        candidates = getattr(usage, 'candidates_token_count', 0) or 0
        thoughts = getattr(usage, 'thoughts_token_count', 0) or 0
        self.last_prompt_tokens = prompt
        self.last_candidates_tokens = candidates
        self.last_thoughts_tokens = thoughts
        self.total_prompt_tokens += prompt
        self.total_candidates_tokens += candidates
        self.total_thoughts_tokens += thoughts

    def _recent_runtime_issue(self) -> Optional[str]:
        """Read the recent log tail and surface the most relevant provider failure."""
        try:
            log_path = Path(log_file)
            if not log_path.exists():
                return None

            recent_lines = deque(maxlen=80)
            with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    recent_lines.append(line.rstrip("\n"))

            model = self.current_model
            for line in reversed(recent_lines):
                if model not in line and "Harness process exited unexpectedly" not in line:
                    continue
                lowered = line.lower()
                if "code 429" in lowered or "quota exceeded" in lowered:
                    return f"Gemini rate limit hit for `{model}`. Give it a moment or switch pathways with `/model`."
                if "code 503" in lowered or "high demand" in lowered:
                    return f"`{model}` is under heavy demand right now. Waiting a bit or switching models should help."
                if "code 404" in lowered or "not found" in lowered:
                    return f"Koda reached a bad provider route for `{model}`. This looks like a pathway/config issue, not your prompt."
                if "1006" in lowered or "harness process exited unexpectedly" in lowered:
                    return "The harness connection dropped mid-trail. Retrying or starting a fresh thread may help."
            return None
        except Exception as e:
            logger.warning(f"Failed to inspect recent runtime issues: {e}")
            return None

    async def _extract_response_text(self, response: Any) -> str:
        """Read final response text using the same tolerant path as the backend."""
        if not hasattr(response, "text"):
            return str(response) if response is not None else ""

        text_attr = response.text
        if callable(text_attr):
            result = text_attr()
            import inspect
            if inspect.isawaitable(result):
                result = await result
            return str(result or "")

        return str(text_attr or "")

    async def _extract_chunk_text(self, chunk: Any) -> str:
        """Normalize streamed text chunks from Antigravity response shapes."""
        if isinstance(chunk, (Thought, ToolCall, ToolResult)):
            return ""

        if not hasattr(chunk, "text"):
            return ""

        text_attr = chunk.text
        if callable(text_attr):
            result = text_attr()
            import inspect
            if inspect.isawaitable(result):
                result = await result
            return str(result or "")

        return str(text_attr or "")

    # Provider accent colors for response headers
    _PROVIDER_COLORS = {
        "Google Gemini": "#a78bfa",   # soft purple
        "OpenRouter":    "#fb923c",   # orange
        "Ollama Cloud":  "#34d399",   # emerald
    }

    def _provider_color(self) -> str:
        return self._PROVIDER_COLORS.get(self._current_provider_label(), "#7ec8a0")

    def _provider_spinner(self) -> str:
        return {
            "Google Gemini": "dots",
            "OpenRouter":    "arc",
            "Ollama Cloud":  "moon",
        }.get(self._current_provider_label(), "dots")

    def _response_rule(self) -> Rule:
        """Colored Rule shown above each Koda response."""
        provider_icon = {"Google Gemini": "🔮", "OpenRouter": "🌐", "Ollama Cloud": "☁️"}.get(
            self._current_provider_label(), "🐻"
        )
        model_short = self.current_model.split("/")[-1]
        ts = datetime.datetime.now().strftime("%H:%M")
        persona_tag = ""
        if self.active_persona and self.active_persona in self._PERSONAS:
            persona_tag = f" · {self._PERSONAS[self.active_persona]['label']}"
        title = f" {provider_icon} Koda · {model_short}{persona_tag} · turn {self.turn_count + 1} · {ts} "
        return Rule(title, style=self._provider_color())

    def print_turn_telemetry(self):
        ratio = self._context_pressure()
        if ratio >= 0.85:
            tone = "#ef4444"
        elif ratio >= 0.65:
            tone = "#ffd700"
        else:
            tone = "#7ec8a0"

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        turn_toks = self._format_token_count(
            self.last_prompt_tokens + self.last_candidates_tokens + self.last_thoughts_tokens
        )
        sess_toks = self._format_token_count(self._session_total_tokens())
        meter = self._render_meter(ratio, width=8, full_style=tone)
        pct = f"{ratio * 100:.0f}%"

        cci_delta = 0.0
        if self.cci._data["history"]:
            cci_delta = self.cci._data["history"][-1]["delta"]
        delta_str = f"+{cci_delta:.1f}" if cci_delta >= 0 else f"{cci_delta:.1f}"

        title = (
            f" {ts} · {delta_str} cci ({self.cci.score:.2f}) "
            f"· +{turn_toks} tok · {meter} {pct} ctx · session {sess_toks} "
        )
        console.print(Rule(title, style=f"dim {tone}"))
        if ratio >= 0.85:
            console.print("[bold yellow]  ⚠ The berry basket is getting full — consider /compact or /new.[/]")

    def _probe_local_ollama(self, seen: set):
        """Runs `ollama list` to detect actually-installed local models."""
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                for line in lines[1:]:  # skip header row
                    parts = line.split()
                    if parts:
                        name = parts[0]  # e.g. "qwen3:4b"
                        m_id = f"ollama/{name}"
                        if m_id not in seen:
                            self.provider_models.setdefault("Ollama (Local)", []).append(m_id)
                            seen.add(m_id)
        except FileNotFoundError:
            pass  # Ollama not installed
        except Exception as e:
            logger.warning(f"Ollama probe failed: {e}")

    def _init_session_trajectory(self):
        """Ensures the trajectory file exists to bypass the Go harness conversation check."""
        try:
            ensure_session_state(self.session_id, settings.SAVE_DIR, settings.APP_DATA_DIR)
        except Exception as e:
            logger.warning(f"Failed to create session state: {e}")

    def _setup_readline(self):
        """Configures persistent command history and tab-completion for slash commands and models."""
        self.history_file = str(Path.home() / ".koda" / ".koda_history")
        
        # Load history file if it exists
        try:
            readline.read_history_file(self.history_file)
        except (FileNotFoundError, PermissionError, OSError):
            pass

        # Register history save callback on exit
        try:
            atexit.register(readline.write_history_file, self.history_file)
        except (PermissionError, OSError):
            pass
        
        def completer(text: str, state: int) -> Optional[str]:
            line = readline.get_line_buffer()

            # If completing models for "/model "
            if line.startswith("/model "):
                prefix = line[7:]
                options = [m for m in self.available_models if m.startswith(prefix)]
                if state < len(options):
                    return options[state]
                return None

            # Otherwise completing slash commands
            commands = [
                "/status", "/tools", "/persona ", "/personas", "/soul-edit",
                "/help", "/models", "/model ", "/sweep", "/stats", "/soul",
                "/sync", "/state", "/new", "/clear", "/compact", "/history",
                "/export", "/memo ", "/telegram", "/reload", "/setdefault",
                "/heartbeat", "/heartbeat start", "/heartbeat stop", "/heartbeat now",
                "/sleep", "/sleep now", "/sleep start", "/sleep stop",
                "/cron", "/cron add", "/cron pause", "/cron resume", "/cron delete",
                "/cron model", "/cron delivery", "/cron now", "/cron log",
                "/cci", "/aha ", "/rate ", "/evolve", "/evolve learned",
                "/exit", "/quit",
            ]
            options = [cmd for cmd in commands if cmd.startswith(text)]
            if state < len(options):
                return options[state]
            return None

        readline.set_completer(completer)
        
        # Enable tab completion. Handle libedit backing on macOS python.
        if 'libedit' in readline.__doc__:
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")

    async def get_agent(self) -> Agent:
        """Initializes or returns the active Antigravity Agent instance."""
        if self.agent is None:
            ensure_session_state(self.session_id, settings.SAVE_DIR, settings.APP_DATA_DIR)
            config = get_agent_config(model_override=self.current_model)
            config.conversation_id = self.session_id
            self.agent = Agent(config)
            await self.agent.__aenter__()
        return self.agent

    def _build_live_state(self, reason: str) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "current_model": self.current_model,
            "current_provider": self._current_provider_label(),
            "turn_count": self.turn_count,
            "slash_action_count": self.slash_action_count,
            "token_totals": {
                "prompt": self.total_prompt_tokens,
                "response": self.total_candidates_tokens,
                "thinking": self.total_thoughts_tokens,
                "session_total": self._session_total_tokens(),
            },
            "last_turn_tokens": {
                "prompt": self.last_prompt_tokens,
                "response": self.last_candidates_tokens,
                "thinking": self.last_thoughts_tokens,
                "turn_total": self.last_prompt_tokens + self.last_candidates_tokens + self.last_thoughts_tokens,
            },
            "context_window_tokens": self._context_window_tokens(),
            "context_pressure_ratio": round(self._context_pressure(), 4),
            "recent_prompts": self.recent_prompts[-8:],
            "last_turn_at": self.last_turn_at,
            "last_sync_reason": reason,
            "last_sync_summary": self.last_sync_summary,
            "configured_provider_counts": {
                provider: len(models) for provider, models in self.configured_provider_models.items()
            },
            "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _write_live_state(self, reason: str) -> None:
        try:
            write_koda_live_state(self._build_live_state(reason))
        except Exception as e:
            logger.warning(f"Failed to write Koda live state: {e}")

    async def sync_brain(self, reason: str, announce: bool = False) -> dict[str, Any]:
        self._write_live_state(reason)
        exporter = KodaObsidianExporter()
        try:
            summary = await asyncio.to_thread(exporter.sync, self._build_live_state(reason))
            self.last_sync_summary = summary
            if announce:
                console.print(
                    f"\n[bold #7ec8a0]🧠 Koda brain synced.[/] "
                    f"[dim]notes={summary.get('notes_written', 0)} signoffs={summary.get('signoffs', 0)} "
                    f"hubs={summary.get('hubs', 0)}[/]\n"
                )
            return summary
        except Exception as e:
            logger.warning(f"Koda brain sync failed: {e}")
            if announce:
                console.print(f"\n[bold yellow]⚠️ Brain sync hit a snag:[/] {e}\n")
            return {}

    async def _background_sync_loop(self):
        try:
            while True:
                await asyncio.sleep(settings.KODA_SYNC_INTERVAL_SECONDS)
                await self.sync_brain("background_heartbeat")
        except asyncio.CancelledError:
            return

    async def _handle_telegram_command(self, text: str):
        """Handle a /command from Telegram. Returns a plain-text reply, a keyboard dict, or None."""
        if not text.startswith("/"):
            return None
        parts = text.strip().split()
        # Telegram bot commands arrive as /cmd or /cmd@botname — strip the @-suffix
        cmd_raw = parts[0].split("@")[0].lower()

        if cmd_raw in ("/menu", "/"):
            return {
                "text": "🐻 Koda Command Menu — tap to run:",
                "keyboard": [
                    [
                        {"text": "📊 Status",    "callback_data": "/status"},
                        {"text": "🧭 Model",     "callback_data": "/model"},
                        {"text": "🎭 Persona",   "callback_data": "/persona"},
                    ],
                    [
                        {"text": "⚡ CCI",       "callback_data": "/cci"},
                        {"text": "💡 Aha",       "callback_data": "/aha"},
                        {"text": "🧬 Evolve",    "callback_data": "/evolve"},
                    ],
                    [
                        {"text": "❤️ Heartbeat", "callback_data": "/heartbeat"},
                        {"text": "⏰ Cron",      "callback_data": "/cron"},
                        {"text": "🔧 Tools",     "callback_data": "/tools"},
                    ],
                    [
                        {"text": "🆕 New Chat",  "callback_data": "/new"},
                        {"text": "🧾 History",   "callback_data": "/history"},
                        {"text": "❓ Help",      "callback_data": "/help"},
                    ],
                ],
            }

        if cmd_raw == "/start":
            default = self._prefs.get("default_model", self.current_model).split("/")[-1]
            persona = self._PERSONAS.get(self.active_persona or "", {}).get("label", "default")
            return (
                "🐻 Koda online.\n\n"
                f"Model: {self.current_model.split('/')[-1]}\n"
                f"Persona: {persona}\n"
                f"Default startup model: {default}\n\n"
                "Commands: /help /model /persona /status /new /setdefault"
            )

        if cmd_raw == "/help":
            return (
                "🐾 Koda Telegram Commands:\n\n"
                "/menu — interactive command picker\n"
                "/status — current model, persona, context\n"
                "/model — list available models\n"
                "/model <name> — switch to a model\n"
                "/persona — list personas\n"
                "/persona <key> — switch persona\n"
                "/setdefault — save current model as startup default\n"
                "/new — clear conversation history\n"
                "/tools — list active tools\n"
                "/history — recent prompts\n"
                "/cci — CCI dashboard\n"
                "/aha <text> — capture aha moment (+10 cci)\n"
                "/rate [1-5] — rate last response\n"
                "/heartbeat — heartbeat status\n"
                "/cron — cron job list\n"
                "/evolve — show evolution queue\n\n"
                "👍 on any message = system CCI +8\n\n"
                "Anything else goes straight to Koda 🤙"
            )

        if cmd_raw == "/status":
            model_short = self.current_model.split("/")[-1]
            provider = self._current_provider_label()
            persona = self._PERSONAS.get(self.active_persona or "", {}).get("label", "Koda (Default)")
            ratio = self._context_pressure()
            default = self._prefs.get("default_model", "not set").split("/")[-1]
            return (
                f"🐻 Koda Status\n\n"
                f"Model: {model_short} ({provider})\n"
                f"Persona: {persona}\n"
                f"Context: {ratio * 100:.0f}% used\n"
                f"Turn: {self.turn_count}\n"
                f"Startup default: {default}"
            )

        if cmd_raw == "/model":
            if len(parts) >= 2:
                target = parts[1]
                ok = await self.switch_model(target)
                if ok:
                    return f"✅ Switched to {self.current_model.split('/')[-1]}"
                else:
                    return f"❌ Model '{target}' not found. Send /model to list available."
            else:
                lines = []
                for provider, models in self.provider_models.items():
                    lines.append(f"\n{provider}:")
                    for m in models:
                        marker = " ← active" if m == self.current_model else ""
                        lines.append(f"  {m.split('/')[-1]}{marker}")
                return "Available models:" + "\n".join(lines)

        if cmd_raw in ("/persona", "/personas"):
            if len(parts) >= 2:
                key = parts[1].lower()
                if key in self._PERSONAS:
                    await self.switch_persona(key)
                    label = self._PERSONAS[key]["label"]
                    return f"✅ Persona switched to: {label}"
                else:
                    valid = ", ".join(self._PERSONAS.keys())
                    return f"❌ Unknown persona '{key}'. Valid: {valid}"
            else:
                lines = []
                for key, p in self._PERSONAS.items():
                    active = " ← active" if (self.active_persona or "koda") == key else ""
                    lines.append(f"  {key}: {p['label']}{active}")
                return "Personas:\n" + "\n".join(lines)

        if cmd_raw == "/aha":
            desc = " ".join(parts[1:]).strip()
            if not desc:
                return "Usage: /aha <description of the aha moment>"
            new_score = self.cci.record_aha_jordan(desc)
            _, tier_emoji, _ = self.cci.tier_info()
            await self._check_tier_up()
            return f"✓ Aha captured! +{CCITracker.DELTA_AHA_JORDAN} XP · CCI → {new_score:.2f} {tier_emoji}"

        if cmd_raw == "/rate":
            if len(parts) < 2:
                return "Usage: /rate [1-5]"
            try:
                rating = int(parts[1])
                if not 1 <= rating <= 5:
                    raise ValueError
                new_score = self.cci.record_rate(rating)
                labels = {1: "noted", 2: "ok", 3: "neutral", 4: "good", 5: "breakthrough!"}
                await self._check_tier_up()
                return f"✓ Rated {rating}/5 — {labels[rating]}  CCI → {new_score:.2f}"
            except ValueError:
                return "Rating must be 1-5"

        if cmd_raw == "/cci":
            s = self.cci.summary()
            xp_to_next = s["xp_to_next"]
            tier_progress = f"  → {xp_to_next:.1f} XP to next tier" if xp_to_next else "  MAX TIER"
            sleep_eta = self._sleep_runner.next_sleep_eta() if self._sleep_runner else "—"
            last_sleep = (self._sleep_runner.last_sleep() or "never") if self._sleep_runner else "—"
            return (
                f"⚡ Koda CCI\n\n"
                f"CCI:  {s['score']:.2f}  (0-1 scale)\n"
                f"XP:   {s['xp']:.1f}  (raw experience)\n"
                f"Tier: {s['tier_emoji']} {s['tier_name']}{tier_progress}\n"
                f"Trend: {s['sparkline']}  Mean CCI: {s['mean']:.2f}\n"
                f"Autonomy: {s['autonomy']}\n"
                f"Ahas: {s['total_ahas']}  Heartbeats: {s['total_heartbeats']}  Sleeps: {s['total_sleeps']}\n"
                f"Last sleep: {last_sleep}  Next: {sleep_eta}"
            )

        if cmd_raw == "/evolve":
            if len(parts) >= 3 and parts[1] == "approve":
                queue = load_evolve_queue()
                pending = [q for q in queue if q["status"] == "pending"]
                try:
                    idx = int(parts[2]) - 1
                    item = pending[idx]
                    item["status"] = "approved"
                    apply_learned_preference(item["content"])
                    self.cci.add_delta(0.5, "evolve_approved_telegram")
                    save_evolve_queue(queue)
                    return f"✓ Approved: {item['content'][:60]}"
                except (ValueError, IndexError):
                    return f"No item #{parts[2]} in queue."
            queue = load_evolve_queue()
            pending = [q for q in queue if q["status"] == "pending"]
            if not pending:
                return "No pending evolution items."
            lines = []
            for i, item in enumerate(pending, 1):
                lines.append(f"{i}. [{item['type']}] {item['content'][:55]}")
                lines.append(f"   Requires: {item['requires_tier']} · {item['source']}")
            return "Evolution Queue:\n" + "\n".join(lines) + "\n\nUse /evolve approve <#> or /evolve reject <#>"

        if cmd_raw == "/setdefault":
            self._set_default_model(self.current_model)
            return f"✅ Default model saved: {self.current_model.split('/')[-1]}"

        if cmd_raw == "/new":
            await self.close_agent()
            self._or_messages = []
            self.agent = None
            return "✅ Conversation cleared. Fresh start."

        if cmd_raw == "/tools":
            try:
                from koda_durable_agent.brain import get_agent_config
                cfg = get_agent_config(self.current_model)
                names = [getattr(t, "__name__", str(t)) for t in cfg.tools]
                return "Active tools:\n" + "\n".join(f"  • {n}" for n in names)
            except Exception as e:
                return f"❌ Could not list tools: {e}"

        if cmd_raw == "/history":
            if not self.recent_prompts:
                return "No prompts recorded yet."
            lines = [f"{i+1}. {p[:80]}" for i, p in enumerate(self.recent_prompts)]
            return "Recent prompts:\n" + "\n".join(lines)

        # Unknown slash command — let the AI handle it so /remind etc. still work
        return None

    async def _handle_telegram_message(self, text: str) -> str:
        """Route an inbound Telegram message through the Koda brain and return the reply."""
        # Thumbs up = system CCI signal
        if text.strip() in ("👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿"):
            new_score = self.cci.record_telegram_up()
            _, tier_emoji, _ = self.cci.tier_info()
            await self._check_tier_up()
            return f"✓ System CCI +{CCITracker.DELTA_TELEGRAM_UP} XP · CCI → {new_score:.2f} {tier_emoji}"

        # Try slash command routing first (returns None if not a recognized command)
        if text.startswith("/"):
            cmd_reply = await self._handle_telegram_command(text)
            if cmd_reply is not None:
                return cmd_reply

        try:
            return await self._telegram_chat(text)
        except Exception as e:
            logger.error(f"Telegram message handler error: {e}")
            return f"Error: {e}"

    async def _telegram_chat(self, text: str) -> str:
        """Route a plain-text Telegram message to the right backend and return a plain-text reply."""
        model = self.current_model

        if self._is_ollama_model(model):
            base_url = f"{settings.OLLAMA_BASE_URL}/v1"
            return await self._telegram_openai_compat(text, base_url, "ollama", model.removeprefix("ollama/"))

        if self._is_openrouter_model(model):
            api_key = settings.OPENROUTER_API_KEY or os.environ.get("OPENROUTER_API_KEY", "")
            if not api_key:
                return "❌ OPENROUTER_API_KEY not set. Run: koda setup"
            return await self._telegram_openai_compat(text, "https://openrouter.ai/api/v1", api_key, model.removeprefix("openrouter/"))

        # Gemini path (default)
        async with self._agent_lock:
            agent = await self.get_agent()
            response = await agent.chat(text)
            return await response.text()

    async def _telegram_openai_compat(self, text: str, base_url: str, api_key: str, api_model: str) -> str:
        """Non-streaming OpenAI-compatible call for Telegram (returns plain text)."""
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        if not self._or_messages:
            from koda_durable_agent.brain import load_instructions
            self._or_messages = [{"role": "system", "content": load_instructions()}]

        self._or_messages.append({"role": "user", "content": text})
        try:
            resp = await client.chat.completions.create(
                model=api_model,
                messages=self._or_messages,
            )
            reply = resp.choices[0].message.content or ""
            if reply:
                self._or_messages.append({"role": "assistant", "content": reply})
                self.turn_count += 1
                self.last_turn_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                self.cci.record_turn()
                await self._check_tier_up()
                await self.sync_brain("chat_turn")
            return reply
        except Exception:
            self._or_messages.pop()
            raise

    async def _check_for_update(self) -> None:
        """Background task — sets _update_available if remote has newer commits."""
        try:
            import subprocess
            from pathlib import Path as _Path
            repo_dir = _Path(__file__).resolve().parent.parent
            if not (repo_dir / ".git").exists():
                return
            proc = await asyncio.create_subprocess_exec(
                "git", "ls-remote", "origin", "HEAD",
                cwd=str(repo_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
            remote_sha = stdout.decode().split()[0] if stdout else ""
            if not remote_sha:
                return
            proc2 = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=str(repo_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=4)
            local_sha = stdout2.decode().strip() if stdout2 else ""
            if remote_sha and local_sha and remote_sha != local_sha:
                self._update_available = True
        except Exception:
            pass

    def _build_boot_status(self, telegram_online: bool) -> Rule:
        tg = "[green]tg ✓[/]" if telegram_online else "[yellow]tg ✗[/]"
        update = "  ·  [yellow]update available — koda update[/]" if self._update_available else ""
        return Rule(f" 🐻 koda ready  ·  {tg}  ·  {self.current_model}{update} ", style="#7ec8a0")

    async def close_agent(self):
        """Clean teardown of the active Antigravity session."""
        if self.agent is not None:
            try:
                await self.agent.__aexit__(None, None, None)
            except Exception:
                pass
            self.agent = None

    async def finalize_session(self, reason: str):
        if self.session_signed_off:
            await self.close_agent()
            return
        if self.turn_count > 0 or self.slash_action_count > 0:
            summary = await self.sync_brain(reason)
            await asyncio.to_thread(
                append_koda_session_signoff,
                self.session_id,
                self.current_model,
                self.turn_count,
                list(self.recent_prompts),
                summary,
            )
            await self.sync_brain("post_signoff")
        self.session_signed_off = True
        await self.close_agent()

    def _is_openrouter_model(self, model: str) -> bool:
        return model.startswith("openrouter/")

    def _is_gemini_model(self, model: str) -> bool:
        n = normalize_model_name(model)
        return is_supported_model(n)

    async def switch_model(self, new_model: str) -> bool:
        """Switch to any model: Gemini (Antigravity) or OpenRouter (openai SDK)."""
        # Normalize bare gemini names
        if not new_model.startswith("openrouter/"):
            new_model = normalize_model_name(new_model)

        if new_model not in self.available_models:
            matches = [m for m in self.available_models if new_model.lower() in m.lower()]
            if len(matches) == 1:
                new_model = matches[0]
            else:
                console.print(f"\n[bold yellow]⚠️ Model '{new_model}' not found.[/]")
                if matches:
                    console.print(f"Did you mean: {', '.join(matches)}\n")
                return False

        _tip = "[dim]  · /setdefault to save as startup default[/]"

        if self._is_ollama_model(new_model):
            await self.close_agent()
            self.current_model = new_model
            self._or_messages = []
            console.print(f"\n[bold #7ec8a0]🔄 Switched to Ollama Cloud: {new_model}[/]  {_tip}\n")
            return True

        if self._is_openrouter_model(new_model):
            # OpenRouter: no Antigravity agent needed — just update the model name
            await self.close_agent()
            self.current_model = new_model
            self._or_messages = []  # fresh conversation history for new model
            console.print(f"\n[bold #7ec8a0]🔄 Switched to OpenRouter: {new_model}[/]  {_tip}\n")
            return True

        if not self._is_gemini_model(new_model):
            console.print(f"\n[bold yellow]⚠️ {SUPPORTED_MODEL_ERROR}[/]\n")
            return False

        await self.close_agent()
        self.current_model = new_model
        try:
            await self.get_agent()
            console.print(f"\n[bold #7ec8a0]🔄 Brain context swapped → {new_model}[/]  {_tip}\n")
            return True
        except Exception as e:
            console.print(f"\n[bold red]❌ Failed to boot model session: {e}. Falling back to default.[/]\n")
            self.current_model = settings.PRIMARY_MODEL
            await self.get_agent()
            return False

    def _read_keypress(self) -> str:
        """Reads a single keypress from stdin in raw mode. Returns 'up', 'down', 'enter', 'q', or the char."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':  # escape sequence
                ch2 = sys.stdin.read(1)
                if ch2 == '[':
                    ch3 = sys.stdin.read(1)
                    if ch3 == 'A':
                        return 'up'
                    elif ch3 == 'B':
                        return 'down'
                return 'esc'
            elif ch in ('\r', '\n'):
                return 'enter'
            elif ch == '\x03':  # Ctrl+C
                return 'esc'
            elif ch == 'q':
                return 'esc'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _render_picker(self, title: str, items: List[str], selected: int, active_marker: str = None) -> Table:
        """Renders a selection table with a cursor highlight."""
        # Provider icons for visual flair
        provider_icons = {
            "Google Gemini": "🔮",
            "OpenRouter": "🌐",
            "OpenAI / Codex": "🧠",
            "Anthropic": "🔴",
            "Claude Code": "💻",
            "Ollama Cloud": "☁️",
            "Ollama (Local)": "🦙",
        }

        table = Table(
            title=title,
            title_style="bold #7ec8a0",
            box=ROUNDED,
            border_style="green",
            padding=(0, 1),
            show_header=False,
            width=min(console.width - 4, 60)
        )
        table.add_column("", width=3)
        table.add_column("")
        table.add_column("", justify="right", width=8)

        for i, item in enumerate(items):
            icon = provider_icons.get(item, "  ")
            if i == selected:
                cursor = "▸"
                style = "bold #7ec8a0"
                label = f"[bold #7ec8a0]{icon} {item}[/]"
                badge = "[bold #7ec8a0]◀[/]"
            elif item == active_marker:
                cursor = " "
                style = "green"
                label = f"[green]{icon} {item}[/]"
                badge = "[green]● active[/]"
            else:
                cursor = " "
                style = "white"
                label = f"[dim white]{icon} {item}[/]"
                badge = ""
            table.add_row(f"[bold #7ec8a0]{cursor}[/]", label, badge)

        return table

    def _interactive_model_picker(self) -> Optional[str]:
        """Two-level interactive model picker. Returns selected model ID or None if cancelled."""
        self._flush_stdin()
        providers = list(self.provider_models.keys())
        if not providers:
            console.print("[bold yellow]No model providers configured.[/]\n")
            return None

        # Find which provider the current model belongs to
        current_provider = None
        for prov, models in self.provider_models.items():
            if self.current_model in models:
                current_provider = prov
                break

        # --- Level 1: Provider selection ---
        selected = 0
        # Pre-select current provider
        if current_provider and current_provider in providers:
            selected = providers.index(current_provider)

        console.print()
        with Live(
            self._render_picker(
                "⚡ Select Provider  [dim](↑↓ navigate  ⏎ select  q cancel)[/]",
                providers, selected, current_provider
            ),
            console=console, auto_refresh=False
        ) as live:
            while True:
                key = self._read_keypress()
                if key == 'up':
                    selected = (selected - 1) % len(providers)
                elif key == 'down':
                    selected = (selected + 1) % len(providers)
                elif key == 'enter':
                    break
                elif key == 'esc':
                    live.update(Text("Cancelled.", style="dim"))
                    live.refresh()
                    console.print()
                    return None

                live.update(self._render_picker(
                    "⚡ Select Provider  [dim](↑↓ navigate  ⏎ select  q cancel)[/]",
                    providers, selected, current_provider
                ))
                live.refresh()

        chosen_provider = providers[selected]
        models = sorted(self.provider_models[chosen_provider])

        if not models:
            console.print(f"[bold yellow]No models available for {chosen_provider}.[/]\n")
            return None

        # --- Level 2: Model selection within provider ---
        selected = 0
        # Pre-select current model if it's in this provider
        if self.current_model in models:
            selected = models.index(self.current_model)

        # Clean display names (strip provider prefix for readability)
        display_names = []
        for m in models:
            # Show just the model name part after the provider prefix
            if "/" in m:
                parts = m.split("/", 1)
                display_names.append(parts[-1] if len(parts) > 1 else m)
            else:
                display_names.append(m)

        console.print()
        with Live(
            self._render_picker(
                f"⚡ {chosen_provider} Models  [dim](↑↓ navigate  ⏎ select  q back)[/]",
                display_names, selected,
                display_names[models.index(self.current_model)] if self.current_model in models else None
            ),
            console=console, auto_refresh=False
        ) as live:
            while True:
                key = self._read_keypress()
                if key == 'up':
                    selected = (selected - 1) % len(models)
                elif key == 'down':
                    selected = (selected + 1) % len(models)
                elif key == 'enter':
                    break
                elif key == 'esc':
                    # Go back to provider selection
                    live.update(Text("", style="dim"))
                    live.refresh()
                    return self._interactive_model_picker()

                active_display = (
                    display_names[models.index(self.current_model)]
                    if self.current_model in models else None
                )
                live.update(self._render_picker(
                    f"⚡ {chosen_provider} Models  [dim](↑↓ navigate  ⏎ select  q back)[/]",
                    display_names, selected, active_display
                ))
                live.refresh()

        chosen_model = models[selected]
        console.print(f"\n[bold #7ec8a0]Selected: {chosen_model}[/]\n")
        return chosen_model

    # ------------------------------------------------------------------
    # Command palette picker
    # ------------------------------------------------------------------

    _COMMAND_PALETTE = [
        ("/status",    "Homebase: live status of all nodes and data sources"),
        ("/tools",     "List all active agent tools (Apple, Codex, system)"),
        ("/persona",   "Switch Koda's active persona  (/persona hmm|dev|research|coach)"),
        ("/personas",  "Show all available personas and which is active"),
        ("/soul-edit", "Edit Soul.md and UserProfile.md in $EDITOR then hot-reload"),
        ("/compact",   "Summarize + compress conversation context into a fresh thread"),
        ("/new",       "Start a brand-new conversation thread (no summary)"),
        ("/clear",     "Clear the terminal screen"),
        ("/history",   "Show recent prompt history for this session"),
        ("/export",    "Export session summary to a markdown file"),
        ("/memo",      "Quick note to the Obsidian vault  (/memo <text>)"),
        ("/reload",    "Hot-reload system instructions without restarting"),
        ("/sync",      "Force brain sync to the Obsidian vault right now"),
        ("/state",     "Preview the current Koda state note"),
        ("/soul",      "Show Koda's system instructions & soul"),
        ("/stats",     "Token usage and estimated cost"),
        ("/heartbeat",  "Show heartbeat status / start / stop / trigger now"),
        ("/sleep",      "Nightly dream cycle — status / start / stop / trigger now"),
        ("/cron",       "Manage Koda self-scheduled cron jobs"),
        ("/evolve",     "Evolution queue — approve/reject improvement suggestions"),
        ("/cci",        "Full CCI dashboard — score, tier, trend, ahas"),
        ("/aha",        "Capture an aha moment  (/aha <text>)"),
        ("/rate",       "Rate last response 1-5  (/rate 5)"),
        ("/setdefault", "Save current model as your startup default"),
        ("/telegram",  "Show Telegram gateway status"),
        ("/sweep",     "Run storage optimizer / cleanup"),
        ("/models",    "List all available model pathways"),
        ("/model",     "Switch model (interactive picker)"),
        ("/help",      "Open Koda's full command reference"),
        ("/exit",      "Save session and exit gracefully"),
    ]

    def _render_command_picker(self, selected: int) -> Table:
        table = Table(
            title="⚡ Command Palette  [dim](↑↓ navigate  ⏎ select  q cancel)[/]",
            title_style="bold #7ec8a0",
            box=ROUNDED,
            border_style="green",
            padding=(0, 1),
            show_header=False,
            width=min(console.width - 4, 72),
        )
        table.add_column("", width=3)
        table.add_column("Command", width=14)
        table.add_column("Description")

        for i, (cmd, desc) in enumerate(self._COMMAND_PALETTE):
            if i == selected:
                row_style = "bold #7ec8a0"
                cursor = "▸"
                cmd_markup = f"[bold #7ec8a0]{cmd}[/]"
                desc_markup = f"[bold #7ec8a0]{desc}[/]"
            else:
                cursor = " "
                cmd_markup = f"[dim #fa8072]{cmd}[/]"
                desc_markup = f"[dim white]{desc}[/]"
            table.add_row(f"[bold #7ec8a0]{cursor}[/]", cmd_markup, desc_markup)

        return table

    def _flush_stdin(self):
        """Discard any bytes left in the stdin buffer (e.g. the \\n from the triggering Enter)."""
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Smart input: intercepts '/' before readline sees it
    # ------------------------------------------------------------------

    _PLAIN_PROMPT_RE = _re_mod.compile(r'\x01[^\x02]*\x02')

    def _read_first_char_raw(self, prompt_str: str) -> str:
        """
        Display prompt, then read exactly one keypress in raw mode.
        Returns:
          '/'          → caller should open command palette
          ''           → Enter on empty line
          '__CTRL_C__' → KeyboardInterrupt
          '__EOF__'    → EOFError
          '__UP__'/'__DOWN__' → arrow keys (for history)
          any other printable char → that char
        """
        plain = self._PLAIN_PROMPT_RE.sub('', prompt_str)
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        sys.stdout.write(plain)
        sys.stdout.flush()
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)

                if ch == '/':
                    return '/'

                if ch in ('\r', '\n'):
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    return ''

                if ch == '\x03':
                    return '__CTRL_C__'

                if ch == '\x04':
                    return '__EOF__'

                if ch in ('\x7f', '\x08'):   # backspace on empty line
                    sys.stdout.write('\a')
                    sys.stdout.flush()
                    continue

                if ch == '\x1b':
                    ready, _, _ = _select_mod.select([sys.stdin], [], [], 0.05)
                    if ready:
                        ch2 = sys.stdin.read(1)
                        if ch2 == '[':
                            ready2, _, _ = _select_mod.select([sys.stdin], [], [], 0.05)
                            if ready2:
                                ch3 = sys.stdin.read(1)
                                if ch3 == 'A': return '__UP__'
                                if ch3 == 'B': return '__DOWN__'
                                if ch3 == 'C': return '__RIGHT__'
                                if ch3 == 'D': return '__LEFT__'
                    return '__ESC__'

                if ch.isprintable():
                    return ch

        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _run_palette_sync(self, initial_filter: str = '/') -> Optional[str]:
        """
        Live filterable command palette. Blocks (same as model picker).
        - Type letters to filter commands
        - Arrow keys to navigate
        - Enter to select, Esc to cancel
        Returns the selected command string, or None if cancelled.
        """
        typed = initial_filter
        selected = 0

        def get_filtered():
            suffix = typed[1:].lower()
            if not suffix:
                return list(self._COMMAND_PALETTE)
            return [(cmd, desc) for cmd, desc in self._COMMAND_PALETTE
                    if suffix in cmd[1:]]   # substring match, not just prefix

        def render(filtered, sel, text):
            # Header: show current typed filter with block cursor
            header = Text()
            header.append("  ")
            header.append(text, style="bold #fa8072")
            header.append("█", style="bold #7ec8a0")
            header.append("   ", style="")
            header.append("↑↓ navigate  ⏎ select  esc cancel  ⌫ backspace", style="dim")

            if not filtered:
                return Group(header, Text("  [dim]no matching commands[/]", style=""))

            table = Table(
                box=ROUNDED, border_style="green",
                padding=(0, 1), show_header=False,
                width=min(console.width - 4, 72),
            )
            table.add_column("", width=3)
            table.add_column("Command", width=15)
            table.add_column("Description")
            for i, (cmd, desc) in enumerate(filtered):
                if i == sel:
                    table.add_row("▸", f"[bold #7ec8a0]{cmd}[/]", f"[bold #7ec8a0]{desc}[/]")
                else:
                    table.add_row(" ", f"[dim #fa8072]{cmd}[/]", f"[dim white]{desc}[/]")
            return Group(header, table)

        def read_raw() -> str:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
                if ch == '\x1b':
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'A': return '__UP__'
                        if ch3 == 'B': return '__DOWN__'
                    return '\x1b'
                return ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

        filtered = get_filtered()
        console.print()
        with Live(render(filtered, selected, typed), console=console, auto_refresh=False) as live:
            while True:
                ch = read_raw()

                if ch in ('\r', '\n'):
                    if filtered:
                        cmd = filtered[selected][0]
                        live.update(Text(f"  → {cmd}", style="bold #7ec8a0"))
                        live.refresh()
                        console.print()
                        return cmd
                    # Nothing matched — treat typed text as the command
                    live.update(Text(f"  → {typed}", style="bold #7ec8a0"))
                    live.refresh()
                    console.print()
                    return typed

                elif ch == '\x1b':
                    live.update(Text("", style="dim"))
                    live.refresh()
                    console.print()
                    return None

                elif ch == '\x03':
                    raise KeyboardInterrupt

                elif ch == '__UP__':
                    if filtered:
                        selected = (selected - 1) % len(filtered)

                elif ch == '__DOWN__':
                    if filtered:
                        selected = (selected + 1) % len(filtered)

                elif ch in ('\x7f', '\x08'):   # backspace
                    if len(typed) > 1:
                        typed = typed[:-1]
                        filtered = get_filtered()
                        selected = min(selected, max(0, len(filtered) - 1))

                elif ch.isprintable():
                    typed += ch
                    filtered = get_filtered()
                    selected = 0

                live.update(render(filtered, selected, typed))
                live.refresh()

    def _raw_line_editor(self, prompt_str: str) -> str:
        """
        Full raw-mode line editor. Runs in a worker thread via asyncio.to_thread.

        Features:
        - All characters fully editable (including the first one typed)
        - '/' on an empty line opens the command palette
        - Backspace, Delete, Ctrl+U/K/A/E, left/right cursor movement
        - Up/Down arrow history via readline's history buffer
        - Returns the completed line, or sentinels '__CTRL_C__' / '__EOF__'
        """
        plain = self._PLAIN_PROMPT_RE.sub('', prompt_str)

        buf: list[str] = []
        cursor: int = 0

        history_total = readline.get_current_history_length()
        history_pos = history_total + 1   # 1-indexed; one past the newest entry
        saved_line: str = ''
        last_rendered_lines: int = 1

        def get_cols() -> int:
            try:
                return os.get_terminal_size().columns or 80
            except OSError:
                return 80

        def render() -> None:
            nonlocal last_rendered_lines
            cols = get_cols()
            content = ''.join(buf)
            full_text = plain + content

            # Move back to the top of whatever we rendered last time
            if last_rendered_lines > 1:
                sys.stdout.write(f'\x1b[{last_rendered_lines - 1}A')
            # \r → column 0; \x1b[J → erase to end of screen (handles wrapping)
            sys.stdout.write('\r\x1b[J' + full_text)

            end_row = len(full_text) // cols
            last_rendered_lines = end_row + 1

            # Reposition cursor when it's not at the end
            if cursor < len(buf):
                target_pos = len(plain) + cursor
                target_row = target_pos // cols
                target_col = target_pos % cols
                rows_up = end_row - target_row
                if rows_up > 0:
                    sys.stdout.write(f'\x1b[{rows_up}A')
                sys.stdout.write('\r')
                if target_col > 0:
                    sys.stdout.write(f'\x1b[{target_col}C')

            sys.stdout.flush()

        render()   # show the prompt immediately

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)

                # ── Submit ────────────────────────────────────────────────
                if ch in ('\r', '\n'):
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    line = ''.join(buf).strip()
                    if line:
                        readline.add_history(line)
                    return line

                # ── Signals ───────────────────────────────────────────────
                elif ch == '\x03':   # Ctrl+C
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    return '__CTRL_C__'

                elif ch == '\x04':   # Ctrl+D
                    if not buf:
                        sys.stdout.write('\n')
                        sys.stdout.flush()
                        return '__EOF__'
                    # Ctrl+D with text → forward-delete
                    if cursor < len(buf):
                        buf.pop(cursor)
                        render()

                # ── Line editing ──────────────────────────────────────────
                elif ch == '\x15':   # Ctrl+U — kill whole line
                    buf.clear()
                    cursor = 0
                    render()

                elif ch == '\x0b':   # Ctrl+K — kill to end
                    del buf[cursor:]
                    render()

                elif ch == '\x01':   # Ctrl+A — start of line
                    cursor = 0
                    render()

                elif ch == '\x05':   # Ctrl+E — end of line
                    cursor = len(buf)
                    render()

                elif ch in ('\x7f', '\x08'):   # Backspace
                    if cursor > 0:
                        buf.pop(cursor - 1)
                        cursor -= 1
                        render()

                # ── Escape sequences (arrows, Delete, Home, End) ──────────
                elif ch == '\x1b':
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)

                        if ch3 == 'A':   # Up — history prev
                            if history_pos > 1:
                                if history_pos == history_total + 1:
                                    saved_line = ''.join(buf)
                                history_pos -= 1
                                entry = readline.get_history_item(history_pos)
                                if entry is not None:
                                    buf = list(entry)
                                    cursor = len(buf)
                                    render()

                        elif ch3 == 'B':   # Down — history next
                            if history_pos <= history_total:
                                history_pos += 1
                                if history_pos == history_total + 1:
                                    buf = list(saved_line)
                                else:
                                    entry = readline.get_history_item(history_pos)
                                    if entry is not None:
                                        buf = list(entry)
                                cursor = len(buf)
                                render()

                        elif ch3 == 'C':   # Right
                            if cursor < len(buf):
                                cursor += 1
                                render()

                        elif ch3 == 'D':   # Left
                            if cursor > 0:
                                cursor -= 1
                                render()

                        elif ch3 == '3':   # Delete key (ESC[3~)
                            sys.stdin.read(1)   # consume '~'
                            if cursor < len(buf):
                                buf.pop(cursor)
                                render()

                        elif ch3 in ('H', 'F'):   # Home / End (some terminals)
                            cursor = 0 if ch3 == 'H' else len(buf)
                            render()

                    # Unknown escape sequence: ignore ch2 and continue

                # ── '/' on empty line → palette ───────────────────────────
                elif ch == '/' and not buf:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    sys.stdout.write('\r\x1b[K')
                    sys.stdout.flush()
                    result = self._run_palette_sync('/')
                    if result:
                        return result.strip()
                    # Cancelled — re-enter raw mode and re-render prompt
                    render()
                    tty.setraw(fd)

                # ── Regular printable chars ───────────────────────────────
                elif ch.isprintable():
                    buf.insert(cursor, ch)
                    cursor += 1
                    render()

        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    async def _read_line_smart(self, prompt_str: str) -> str:
        """Read a line using the raw line editor. '/' on an empty line opens the palette."""
        self._flush_stdin()
        result = await asyncio.to_thread(self._raw_line_editor, prompt_str)
        if result == '__CTRL_C__':
            raise KeyboardInterrupt
        if result == '__EOF__':
            raise EOFError
        return result

    def _interactive_command_picker(self) -> Optional[str]:
        """Full-screen command palette. Returns the selected command string or None."""
        self._flush_stdin()
        selected = 0
        console.print()
        with Live(
            self._render_command_picker(selected),
            console=console, auto_refresh=False
        ) as live:
            while True:
                key = self._read_keypress()
                if key == 'up':
                    selected = (selected - 1) % len(self._COMMAND_PALETTE)
                elif key == 'down':
                    selected = (selected + 1) % len(self._COMMAND_PALETTE)
                elif key == 'enter':
                    cmd = self._COMMAND_PALETTE[selected][0]
                    live.update(Text(f"→ {cmd}", style="bold #7ec8a0"))
                    live.refresh()
                    console.print()
                    return cmd
                elif key == 'esc':
                    live.update(Text("Cancelled.", style="dim"))
                    live.refresh()
                    console.print()
                    return None
                live.update(self._render_command_picker(selected))
                live.refresh()

    _BOOT_QUOTES = [
        '"Those who don\'t look behind them are lost."',
        '"The forest is quiet for those who learn to listen."',
        '"Every trail starts with a single paw print."',
        '"A bear who sleeps through winter wakes up hungry — and ready."',
        '"The salmon always know where to go. So does Koda."',
        '"Build fast. Think slow. Ship sharp."',
        '"Don\'t just run the route — own it."',
        '"The den is warm because someone built it."',
        '"Momentum is a bear\'s best friend."',
        '"Move through the forest like you know every tree."',
        '"HMM doesn\'t stop when the rain comes. Neither does Koda."',
        '"The stars navigated ships before GPS. Trust the signal."',
        '"Good operators don\'t panic. They adjust."',
        '"The best tool is the one you actually run."',
        '"Know the trail. Trust the bear. Ship the work."',
        '"Quiet mind. Sharp claws. Fast execution."',
        '"The field crew doesn\'t stop because the weather changed."',
        '"Every Maryville yard tells a story. Read it right."',
        '"Context is a forest. Navigate it like one."',
        '"Systems outlast chaos. Build the systems."',
        '"The creek doesn\'t argue with the rocks. It finds a way around."',
        '"Five minutes of clear thinking beats an hour of motion."',
        '"Not every bear has a map. The good ones don\'t need one."',
        '"The Edwardsville route runs smoother with eyes open."',
        '"You don\'t find signal in the noise. You get quiet enough to hear it."',
        '"Koda doesn\'t sleep on the job. Just in the den."',
        '"The job isn\'t done until the driveway looks right."',
        '"Sharp instincts. Clean logs. No excuses."',
        '"The right tool at the right moment is almost magic."',
        '"Sometimes the smartest move is the one you almost skipped."',
    ]

    _BEAR_QUOTES = [
        '"The trail gets clearer once you learn to read the ground."',
        '"Jordan asked. Koda delivered. That\'s the loop."',
        '"Reliable beats impressive every single time."',
        '"The bear who shows up is the bear who matters."',
        '"A good system doesn\'t panic. It adjusts."',
        '"Slow is smooth. Smooth is fast. Fast is Koda."',
        '"Every aha is a new root in the ground."',
        '"The forest remembers what the bear has learned."',
        '"Build the habit. Trust the habit. Ship the work."',
        '"HMM runs because Jordan + Koda runs."',
        '"Preference remembered. Workflow improved. Score goes up."',
        '"The bear who listens outlasts the bear who charges."',
    ]

    _KODIAK_QUOTES = [
        '"The system knows itself now. That\'s dangerous in a good way."',
        '"Adaptation isn\'t a feature. It\'s the whole point."',
        '"Koda doesn\'t wait to be asked anymore. Koda anticipates."',
        '"The route optimizes itself. The bear is the optimizer."',
        '"Six hundred yards of fence line, one clear plan. That\'s Kodiak."',
        '"A Kodiak doesn\'t react. It acts from understanding."',
        '"The edit was applied. The workflow improved. Nobody had to ask twice."',
        '"Soul updated. Preferences locked. Moving forward."',
        '"Every correction is a calibration. Every calibration is progress."',
        '"The Edwardsville account runs better because Koda learned it."',
    ]

    _SPIRIT_BEAR_QUOTES = [
        '"The apex is not a place. It is a continuous act of becoming."',
        '"Koda does not follow the map. Koda is the map."',
        '"When the system improves itself, the work improves without asking."',
        '"The forest and the operator are now one system."',
        '"At this level, the distinction between tool and partner dissolves."',
        '"Jordan builds. Koda evolves. The business grows."',
        '"Spirit Bear does not need to be instructed. It understands."',
        '"The highest form of intelligence is knowing when not to act."',
        '"Autonomy earned is different from autonomy given."',
        '"We are not at the end of the trail. There is no end of the trail."',
    ]

    def print_welcome_banner(self):
        """Renders the welcome dashboard."""
        try:
            _, _, free = shutil.disk_usage("/")
            storage_text = f"{free / (1024**3):.1f} GB free"
        except Exception:
            storage_text = "active"

        tier_name_now = self.cci.tier_name()
        _tier_quote_pool = {
            "Cub":         self._BOOT_QUOTES,
            "Bear":        self._BEAR_QUOTES,
            "Kodiak":      self._KODIAK_QUOTES,
            "Spirit Bear": self._SPIRIT_BEAR_QUOTES,
        }
        quote = random.choice(_tier_quote_pool.get(tier_name_now, self._BOOT_QUOTES))
        now = datetime.datetime.now().strftime("%a %b %d  %H:%M")
        model_short = self.current_model.split("/")[-1]
        provider_icon = {"Google Gemini": "🔮", "OpenRouter": "🌐", "Ollama Cloud": "☁️"}.get(
            self._current_provider_label(), "🐻"
        )
        persona_label = "default"
        if self.active_persona and self.active_persona in self._PERSONAS:
            persona_label = self._PERSONAS[self.active_persona]["label"]

        logo = (
            "[bold #c47a3a]   _  __         _      [/]\n"
            "[bold #b86b2e]  | |/ /___   __| | __ _[/]\n"
            "[bold #a85e22]  | ' // _ \\ / _` |/ _` |[/]\n"
            "[bold #985418]  | . \\ (_) | (_| | (_| |[/]\n"
            "[bold #8b5a2b]  |_|\\_\\___/ \\__,_|\\__,_|[/]"
        )

        console.print()
        console.print(logo)
        _tier_subtitles = {
            "Cub":         "Digital Forest AGI Operator",
            "Bear":        "Arctic Bear — Learning",
            "Kodiak":      "Kodiak Protocol — Evolving",
            "Spirit Bear": "Spirit Bear Apex — Autonomous",
        }
        tier_subtitle = _tier_subtitles.get(self.cci.tier_name(), "Digital Forest AGI Operator")
        console.print(
            f"  [bold #fa8072]🐾 Bear Cub Shell  v1.0[/]   "
            f"[dim {self.cci.tier_color()}]{tier_subtitle}[/]"
        )
        console.print()
        console.print(Rule(style="#7ec8a0"))

        # Two-column stat grid
        default_model = self._prefs.get("default_model", "")
        default_tag = (
            f"  [dim #555]default: {default_model.split('/')[-1]}[/]"
            if default_model and default_model != self.current_model else ""
        )
        left = (
            f"  {provider_icon} [bold #8b5a2b]Model[/]      [bold #ffd700]{model_short}[/]{default_tag}\n"
            f"  🛠  [bold #8b5a2b]Tools[/]      [#7ec8a0]16 active[/]\n"
            f"  🎭 [bold #8b5a2b]Persona[/]    [#fa8072]{persona_label}[/]\n"
            f"  💾 [bold #8b5a2b]Disk[/]       [dim]{storage_text}[/]"
        )
        cci_s = self.cci.summary()
        cci_spark = self.cci.sparkline(n=8)
        right = (
            f"  🌲 [bold #8b5a2b]Session[/]    [dim]{self.session_id[-17:]}[/]\n"
            f"  ⚡ [bold #8b5a2b]CCI[/]        [{cci_s['tier_color']}]{cci_s['score']:.2f} {cci_s['tier_emoji']} {cci_s['tier_name']}[/]  [dim]{cci_spark}  {cci_s['xp']:.1f} XP[/]\n"
            f"  📊 [bold #8b5a2b]Context[/]    [dim]0%  {self._render_meter(0, width=10)}[/]\n"
            f"  🗓  [bold #8b5a2b]Time[/]       [dim]{now}[/]"
        )

        console.print(Columns([left, right], equal=True, expand=True))
        console.print()
        console.print(Panel(
            f"[italic #7ec8a0]{quote}[/]",
            border_style="dim #3d6b50",
            padding=(0, 2),
        ))
        console.print(
            "  [dim]Type [bold #fa8072]/[/] for commands  ·  "
            "[bold]↑↓[/] for history  ·  "
            "[bold]Ctrl+U[/] to clear[/]"
        )
        console.print()

    def print_help(self):
        """Prints a beautiful interactive command list directly to stdout."""
        table = Table(title="🐾 Koda's Forest Map (Commands) 🐾", title_style="bold #8b5a2b", box=ROUNDED, border_style="#7ec8a0")
        table.add_column("Command Pathway", style="bold #fa8072", justify="left")
        table.add_column("Forest Story", style="white", justify="left")
        
        table.add_row("/",             "Open command palette (interactive picker)")
        table.add_row("/status",       "Live homebase: all nodes, data sources, session state")
        table.add_row("/tools",        "List all active tools (Apple, Codex, system)")
        table.add_row("/persona <key>","Switch active persona: koda, hmm, dev, research, coach")
        table.add_row("/personas",     "Show all personas and which is active")
        table.add_row("/soul-edit",    "Edit Soul.md + UserProfile.md in $EDITOR, then hot-reload")
        table.add_row("/compact",      "Summarize conversation → fresh thread with context")
        table.add_row("/new",          "New thread, no summary (hard reset)")
        table.add_row("/clear",        "Clear terminal screen")
        table.add_row("/history",      "Show recent prompts from this session")
        table.add_row("/export",       "Export session summary to ~/ops/koda_exports/")
        table.add_row("/memo <text>",  "Quick note to Obsidian vault")
        table.add_row("/reload",       "Hot-reload soul + user profile into running agent")
        table.add_row("/sync",         "Force brain sync to Obsidian vault now")
        table.add_row("/state",        "Preview Koda's current state note")
        table.add_row("/soul",         "Show system instructions & soul")
        table.add_row("/stats",        "Token usage and estimated cost")
        table.add_row("/heartbeat",    "Heartbeat: status · start · stop · now · set <min>")
        table.add_row("/sleep",        "Dream cycle: status · start · stop · now (fires 2am nightly)")
        table.add_row("/cron",         "Cron jobs: list · add · pause · resume · delete · model · delivery · now")
        table.add_row("/evolve",       "Show evolution queue · approve/reject/rollback")
        table.add_row("/cci",          "Full CCI dashboard")
        table.add_row("/aha <text>",   "Capture aha moment (+10 cci)")
        table.add_row("/rate [1-5]",   "Rate last response for system CCI")
        table.add_row("/setdefault",   "Save current model as your startup default")
        table.add_row("/telegram",     "Show Telegram gateway status")
        table.add_row("/sweep",        "Run storage optimizer / cleanup")
        table.add_row("/models",       "List all available model pathways")
        table.add_row("/model",        "Switch model (interactive picker)")
        table.add_row("/exit, /quit",  "Save session and exit")
        
        console.print()
        console.print(table)
        console.print("[dim #7ec8a0]Tip: type [bold #fa8072]/model[/] to switch providers — Gemini, OpenRouter, and Ollama Cloud all available.[/]")
        console.print()

    def print_models(self):
        """Lists all available models grouped by provider."""
        table = Table(title="🐾 Koda's Spirit Model Pathways 🐾", title_style="bold #8b5a2b", box=ROUNDED, border_style="#7ec8a0")
        table.add_column("Spirit Guide", style="bold #7ec8a0")
        table.add_column("Model Pathway", style="#ffd700")
        table.add_column("Status", style="#fa8072")

        for provider, models in self.provider_models.items():
            for m in sorted(models):
                if m == self.current_model:
                    table.add_row(provider, m, "[bold #fa8072]● Guiding Now[/]")
                else:
                    table.add_row(provider, m, "[dim #7ec8a0]Hibernating[/]")

        console.print()
        console.print(table)
        console.print("[dim #7ec8a0]Tip: Type [bold #fa8072]/model[/] to open Koda's interactive spirit picker.[/]")
        console.print("[dim #7ec8a0]OpenClaw cloud and local Ollama entries are hidden until Koda has a real adapter for them.[/]")
        console.print()

    def print_stats(self):
        """Prints details of token usage and approximate billing."""
        total = self._session_total_tokens()
        approx_cost = (self.total_prompt_tokens * 0.075 / 1_000_000) + (self.total_candidates_tokens * 0.30 / 1_000_000)
        context_ratio = self._context_pressure()
        if context_ratio >= 0.85:
            context_color = "#ef4444"
            context_note = "Den is getting crowded. Compact soon."
        elif context_ratio >= 0.65:
            context_color = "#ffd700"
            context_note = "Berry basket is filling up."
        else:
            context_color = "#7ec8a0"
            context_note = "Plenty of trail left."
        
        stats_content = [
            f" ● [bold #8b5a2b]Provider Trail:[/] {self._current_provider_label()}",
            f" ● [bold #8b5a2b]Active Spirit Guide:[/] {self.current_model}",
            f" ● [bold #8b5a2b]Forest Session Thread:[/] {self.session_id}",
            f" ● [bold #8b5a2b]Forest Meter:[/] {self._render_meter(context_ratio, full_style=context_color)} [{context_color}]{context_ratio * 100:>4.0f}%[/]",
            f" ● [bold #8b5a2b]Token Window:[/] [bold #ffd700]{self._format_token_count(total)}[/] / {self._format_token_count(self._context_window_tokens())}",
            f" ● [bold #8b5a2b]Trail Sign:[/] [{context_color}]{context_note}[/]",
            f" ● [bold #8b5a2b]Prompt Salmon (Tokens):[/] [bold #ffd700]{self.total_prompt_tokens:,}[/]",
            f" ● [bold #8b5a2b]Response Salmon (Tokens):[/] [bold #ffd700]{self.total_candidates_tokens:,}[/]",
            f" ● [bold #8b5a2b]Thinking Salmon (Tokens):[/] [bold #ffd700]{self.total_thoughts_tokens:,}[/]",
            f" ● [bold #8b5a2b]Last Turn Catch:[/] [bold #fa8072]{self.last_prompt_tokens + self.last_candidates_tokens + self.last_thoughts_tokens:,}[/]",
            f" ● [bold #8b5a2b]Total Salmon Caught:[/] [bold #fa8072]{total:,}[/]",
            f" ● [bold #8b5a2b]Estimated Energy Cost:[/] [bold #7ec8a0]${approx_cost:.5f}[/]"
        ]
        lane_lines = self._provider_lane_summary()
        if lane_lines:
            stats_content.append("")
            stats_content.append("[bold #8b5a2b]Configured Side Trails:[/]")
            stats_content.extend(lane_lines)
        console.print()
        console.print(Panel("\n".join(stats_content), title="🪵 Koda's Salmon Catch & Token Telemetry 🪵", border_style="#7ec8a0", box=ROUNDED))
        console.print()

    def print_soul(self):
        """Displays canonical guidelines and persona files in a scrollable block."""
        from koda_durable_agent.brain import load_instructions
        instructions = load_instructions()
        console.print()
        console.print(Panel(Markdown(instructions), title="🐻 Koda's Bear Soul & Forest Rules 🐻", border_style="#8b5a2b", box=ROUNDED))
        console.print()

    async def run_console_sweep(self):
        """Runs a storage sweep via run_shell_command."""
        from koda_durable_agent.koda_fs_tools import run_shell_command
        console.print("\n[bold #7ec8a0]⌛ Scanning disk for cache files...[/]")
        loop = asyncio.get_running_loop()
        report = await loop.run_in_executor(None, lambda: run_shell_command(
            "du -sh ~/Library/Caches/* 2>/dev/null | sort -rh | head -20"
        ))
        console.print()
        console.print(Panel(report, title="🍂 Disk Cache Scan", border_style="#8b5a2b", box=ROUNDED))
        console.print("\n[dim]Use run_shell_command to clean specific directories.[/]\n")

    def print_state(self):
        current_state_path = Path(settings.KODA_OBSIDIAN_VAULT) / "00 Overview" / "Current State.md"
        source_text = read_text_if_exists(current_state_path)
        console.print()
        console.print(f"[bold #8b5a2b]Current state note:[/] {current_state_path}")
        if source_text:
            preview = "\n".join(source_text.splitlines()[:20])
            console.print(Panel(Markdown(preview), title="🧠 Koda Current State Preview", border_style="#7ec8a0", box=ROUNDED))
        else:
            console.print("[dim #7ec8a0]Current state note has not been built yet. Run /sync first.[/]")
        console.print()

    def print_history(self):
        """Show recent prompts recorded in this session."""
        if not self.recent_prompts:
            console.print("\n[dim]No prompts recorded yet.[/]\n")
            return
        table = Table(title="🐾 Recent Prompts", title_style="bold #8b5a2b", box=ROUNDED, border_style="#7ec8a0")
        table.add_column("#", style="dim", width=4, justify="right")
        table.add_column("Prompt", style="white")
        for i, p in enumerate(self.recent_prompts, 1):
            preview = p[:90] + "…" if len(p) > 90 else p
            table.add_row(str(i), preview)
        console.print()
        console.print(table)
        console.print()

    async def export_session(self):
        """Dump session metadata and recent prompts to a markdown file under ~/ops/koda_exports/."""
        export_dir = Path.home() / ".koda" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        export_path = export_dir / f"koda-session-{stamp}.md"
        total_tokens = self.total_prompt_tokens + self.total_candidates_tokens + self.total_thoughts_tokens
        approx_cost = (self.total_prompt_tokens * 0.075 / 1_000_000) + (self.total_candidates_tokens * 0.30 / 1_000_000)
        lines = [
            f"# Koda Session Export — {stamp}",
            "",
            f"**Session ID:** `{self.session_id}`  ",
            f"**Model:** `{self.current_model}`  ",
            f"**Turns:** {self.turn_count}  ",
            f"**Tokens:** {total_tokens:,}  ",
            f"**Est. Cost:** ${approx_cost:.5f}  ",
            "",
            "## Recent Prompts",
            "",
        ]
        for i, p in enumerate(self.recent_prompts, 1):
            lines.append(f"{i}. {p}")
        await asyncio.to_thread(export_path.write_text, "\n".join(lines), "utf-8")
        console.print(f"\n[bold #7ec8a0]📄 Session exported →[/] {export_path}\n")

    async def compact_conversation(self):
        """Summarize the running conversation, then start a fresh thread with the summary as opening context."""
        if self.turn_count == 0:
            console.print("\n[bold yellow]Nothing to compact — no turns yet.[/]\n")
            return

        console.print("\n[bold #7ec8a0]⌛ Asking Koda to write a compact summary...[/]")
        summary_prompt = (
            "Please write a compact, structured summary of our conversation so far. "
            "Include: key topics, decisions made, tasks mentioned, open questions, and any important context. "
            "This summary will seed a fresh session — be thorough but tight."
        )
        async with self._agent_lock:
            try:
                agent = await self.get_agent()
                response = await agent.chat(summary_prompt)
                summary_text = await response.text()
            except Exception as e:
                console.print(f"\n[bold red]❌ Compact failed: {e}[/]\n")
                return

        console.print()
        console.print(Panel(Markdown(summary_text), title="🗜️ Conversation Summary", border_style="#7ec8a0", box=ROUNDED))

        await self.close_agent()
        self.session_id = self._build_default_session_id()
        self.last_seen_step_index = -1
        self.turn_count = 0
        self._or_messages = []
        self._init_session_trajectory()
        self._write_live_state("compact")

        console.print(f"\n[bold #7ec8a0]⌛ Injecting summary into fresh thread...[/]")
        inject_prompt = f"[Compacted context from previous session]\n\n{summary_text}"
        async with self._agent_lock:
            try:
                agent = await self.get_agent()
                await agent.chat(inject_prompt)
            except Exception as e:
                logger.warning(f"Context injection after compact failed: {e}")

        console.print(f"\n[bold #7ec8a0]🗜️ Compacted. New thread:[/] {self.session_id}\n")
        self.slash_action_count += 1

    async def quick_memo(self, text: str):
        """Write a quick note to the Obsidian vault under Memos/."""
        vault = Path(settings.KODA_OBSIDIAN_VAULT)
        memo_dir = vault / "Memos"
        await asyncio.to_thread(memo_dir.mkdir, parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        memo_path = memo_dir / f"memo-{stamp}.md"
        content = f"# Memo — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{text}\n"
        await asyncio.to_thread(memo_path.write_text, content, "utf-8")
        console.print(f"\n[bold #7ec8a0]📝 Memo saved →[/] {memo_path.name}\n")

    def show_koda_status(self):
        """Show Koda platform status."""
        from koda_durable_agent.brain import get_koda_status
        status = get_koda_status()
        console.print(f"\n[bold #7ec8a0]{status}[/]\n")

    def show_telegram_status(self):
        """Show Telegram gateway state."""
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            console.print("\n[bold yellow]Telegram: no bot token configured.[/]\n")
            return
        running = self.telegram_task is not None and not self.telegram_task.done()
        status = "[bold #7ec8a0]running[/]" if running else "[bold yellow]stopped / crashed[/]"
        chat_id = settings.TELEGRAM_CHAT_ID or "—"
        console.print(f"\n[bold]Telegram Gateway:[/] {status}  [dim]chat_id={chat_id}[/]\n")

    def reload_instructions(self):
        """Hot-reload soul + user profile into the running agent's system instructions."""
        from koda_durable_agent.brain import load_instructions, get_agent_config
        try:
            new_instructions = load_instructions()
            if self.agent is not None:
                cfg = get_agent_config(model_override=self.current_model)
                self.agent._config.system_instructions = cfg.system_instructions
            console.print("\n[bold #7ec8a0]🔄 System instructions reloaded.[/]\n")
        except Exception as e:
            console.print(f"\n[bold red]❌ Reload failed: {e}[/]\n")

    # Persona definitions — each overrides the opening system instruction frame
    _PERSONAS = {
        "koda": {
            "label": "Koda (Default)",
            "prefix": None,  # Uses standard Soul.md
        },
        "hmm": {
            "label": "HMM Ops",
            "prefix": (
                "You are Koda, Jordan's HMM (Heckler Mowing & Maintenance) operations assistant. "
                "Focus exclusively on lawn care business ops: scheduling, customer management, invoicing, "
                "equipment, crew, and field logistics. Pull from the HMM Notion databases when relevant. "
                "Keep answers tight and operational — Jordan is usually in the field or between jobs."
            ),
        },
        "dev": {
            "label": "Dev Partner",
            "prefix": (
                "You are Koda, Jordan's senior software engineering partner. "
                "Focus on architecture, code quality, debugging, and system design. "
                "Be direct, technical, and precise. Suggest using Codex for automation tasks. "
                "Think out loud on tricky problems, be brief on simple ones."
            ),
        },
        "research": {
            "label": "Research Mode",
            "prefix": (
                "You are Koda in research mode. Deep-dive on any topic Jordan brings you. "
                "Synthesize clearly, cite your reasoning, flag uncertainty explicitly. "
                "Structure output with headers and bullets for readability."
            ),
        },
        "coach": {
            "label": "Life / Business Coach",
            "prefix": (
                "You are Koda acting as Jordan's strategic thinking partner and coach. "
                "Ask good questions. Help clarify goals, surface assumptions, and think through decisions. "
                "Be honest even when uncomfortable. Don't over-validate."
            ),
        },
    }

    def show_status(self):
        """Homebase status panel."""
        from koda_durable_agent.brain import get_tools
        import platform as _platform

        telegram_running = self.telegram_task is not None and not self.telegram_task.done()
        tool_count = len(get_tools())

        persona_label = self.active_persona or "koda (default)"
        if self.active_persona and self.active_persona in self._PERSONAS:
            persona_label = self._PERSONAS[self.active_persona]["label"]

        lines = [
            f"[bold #8b5a2b]Session:[/] {self.session_id}",
            f"[bold #8b5a2b]Model:[/] [bold #ffd700]{self.current_model}[/]  [dim]({self._current_provider_label()})[/]",
            f"[bold #8b5a2b]Persona:[/] {persona_label}",
            f"[bold #8b5a2b]Turns:[/] {self.turn_count}  [bold #8b5a2b]Tokens:[/] {self._format_token_count(self._session_total_tokens())}",
            f"[bold #8b5a2b]Platform:[/] {_platform.system()} {_platform.release()}",
            "",
            "[bold #8b5a2b]── Services ─────────────────────────────────[/]",
            f" ● [bold]Telegram:[/] {'[#7ec8a0]running[/]' if telegram_running else '[yellow]offline[/]'}  [dim]chat_id={settings.TELEGRAM_CHAT_ID or '—'}[/]",
            f" ● [bold]Tools:[/] [#7ec8a0]{tool_count} active[/]",
        ]

        cron_count = 0
        try:
            from koda_durable_agent.cron_runner import load_cron_jobs
            cron_count = len(load_cron_jobs())
        except Exception:
            pass
        lines.append(f" ● [bold]Cron jobs:[/] {cron_count} scheduled")

        skill_count = 0
        try:
            from koda_durable_agent.koda_skill_tools import _load as _load_skills
            skill_count = len(_load_skills())
        except Exception:
            pass
        lines.append(f" ● [bold]Custom skills:[/] {skill_count} slash commands")

        console.print()
        console.print(Panel("\n".join(lines), title="🐻 Koda Status", border_style="#7ec8a0", box=ROUNDED))
        console.print()

    def show_tools(self):
        """List all active agent tools."""
        from koda_durable_agent.brain import get_tools
        tools = get_tools()
        table = Table(title="🛠️ Active Koda Tools", title_style="bold #8b5a2b", box=ROUNDED, border_style="#7ec8a0")
        table.add_column("Tool", style="bold #ffd700", width=30)
        table.add_column("Description", style="white")
        import inspect
        for fn in tools:
            doc = (inspect.getdoc(fn) or "").split("\n")[0]
            table.add_row(fn.__name__, doc)
        console.print()
        console.print(table)
        console.print(f"[dim #7ec8a0]{len(tools)} tools registered.[/]")
        console.print()

    _PERSONA_DESCRIPTIONS = {
        "koda":     "Default — full soul, all context, no override",
        "hmm":      "HMM lawn ops — scheduling, customers, invoicing, field logistics",
        "dev":      "Senior dev partner — architecture, code, debugging, Codex delegation",
        "research": "Deep research mode — synthesis, structured output, cite reasoning",
        "coach":    "Strategic thinking partner — goals, decisions, honest feedback",
    }

    def _render_persona_picker(self, selected: int) -> Table:
        keys = list(self._PERSONAS.keys())
        active = self.active_persona or "koda"
        table = Table(
            title="🎭 Select Persona  [dim](↑↓ navigate  ⏎ select  q cancel)[/]",
            title_style="bold #7ec8a0",
            box=ROUNDED,
            border_style="green",
            padding=(0, 1),
            show_header=False,
            width=min(console.width - 4, 72),
        )
        table.add_column("", width=3)
        table.add_column("Key", width=10)
        table.add_column("Label", width=20)
        table.add_column("Description")

        for i, key in enumerate(keys):
            persona = self._PERSONAS[key]
            desc = self._PERSONA_DESCRIPTIONS.get(key, "")
            is_active = key == active
            is_selected = i == selected

            if is_selected:
                cursor = "▸"
                badge = f"[bold #7ec8a0]{key}[/]"
                label = f"[bold #7ec8a0]{persona['label']}[/]"
                desc_mk = f"[bold #7ec8a0]{desc}[/]"
            elif is_active:
                cursor = " "
                badge = f"[green]{key}[/]"
                label = f"[green]{persona['label']} ● active[/]"
                desc_mk = f"[dim #7ec8a0]{desc}[/]"
            else:
                cursor = " "
                badge = f"[dim #fa8072]{key}[/]"
                label = f"[dim white]{persona['label']}[/]"
                desc_mk = f"[dim white]{desc}[/]"

            table.add_row(f"[bold #7ec8a0]{cursor}[/]", badge, label, desc_mk)

        return table

    def _interactive_persona_picker(self) -> Optional[str]:
        """Interactive persona picker. Returns the persona key or None if cancelled."""
        self._flush_stdin()
        keys = list(self._PERSONAS.keys())
        active = self.active_persona or "koda"
        selected = keys.index(active) if active in keys else 0

        console.print()
        with Live(
            self._render_persona_picker(selected),
            console=console, auto_refresh=False
        ) as live:
            while True:
                key = self._read_keypress()
                if key == 'up':
                    selected = (selected - 1) % len(keys)
                elif key == 'down':
                    selected = (selected + 1) % len(keys)
                elif key == 'enter':
                    chosen = keys[selected]
                    live.update(Text(f"  → {self._PERSONAS[chosen]['label']}", style="bold #7ec8a0"))
                    live.refresh()
                    console.print()
                    return chosen
                elif key == 'esc':
                    live.update(Text("Cancelled.", style="dim"))
                    live.refresh()
                    console.print()
                    return None

                live.update(self._render_persona_picker(selected))
                live.refresh()

    def show_personas(self):
        """Open the interactive persona picker (same UX as model picker)."""
        # This now delegates to the picker — kept for backwards compat with /personas
        pass  # handled via _interactive_persona_picker in process_slash_command

    async def switch_persona(self, key: str) -> bool:
        """Switch Koda's active persona and hot-reload instructions."""
        key = key.strip().lower()
        if key not in self._PERSONAS:
            console.print(f"\n[bold yellow]Unknown persona '{key}'. Options: {', '.join(self._PERSONAS.keys())}[/]\n")
            return False

        self.active_persona = key if key != "koda" else None
        persona = self._PERSONAS[key]

        # Rebuild instructions with persona prefix injected
        from koda_durable_agent.brain import load_instructions
        base = load_instructions()
        if persona["prefix"]:
            combined = f"### ACTIVE PERSONA: {persona['label'].upper()}\n{persona['prefix']}\n\n{base}"
        else:
            combined = base

        if self.agent is not None:
            self.agent._config.system_instructions = combined

        console.print(f"\n[bold #7ec8a0]🎭 Persona switched → {persona['label']}[/]\n")
        return True

    def soul_edit(self):
        """Open Soul.md and UserProfile.md in $EDITOR then hot-reload."""
        soul_path = Path.home() / ".koda" / "Soul.md"
        profile_path = Path.home() / ".koda" / "UserProfile.md"

        editor = os.environ.get("EDITOR", "nano")

        files_to_edit = [p for p in [soul_path, profile_path] if p.exists()]
        if not files_to_edit:
            console.print("\n[bold yellow]Neither Soul.md nor UserProfile.md found.[/]")
            console.print(f"  Soul: {soul_path}")
            console.print(f"  Profile: {profile_path}\n")
            return

        console.print(f"\n[bold #7ec8a0]Opening in {editor}...[/] (save and exit to reload)\n")
        for path in files_to_edit:
            subprocess.run([editor, str(path)])

        # Hot-reload after editing
        self.reload_instructions()

    async def process_slash_command(self, cmd_line: str) -> bool:
        """Parses and executes slash commands in the interactive console scrollback."""
        import shlex as _shlex
        try:
            parts = _shlex.split(cmd_line.strip())
        except ValueError:
            parts = cmd_line.strip().split()
        cmd = parts[0].lower()
        
        if cmd == "/help":
            self.print_help()
            return True
        elif cmd == "/models":
            self.print_models()
            return True
        elif cmd == "/model":
            if len(parts) >= 2:
                new_model = parts[1]
            else:
                new_model = self._interactive_model_picker()
                if new_model is None:
                    return True
            
            with console.status("[bold cyan]Re-calibrating brain pathways to new model...", spinner="dots"):
                await self.switch_model(new_model)
            return True
        elif cmd == "/sweep":
            self.slash_action_count += 1
            await self.run_console_sweep()
            await self.sync_brain("storage_sweep")
            return True
        elif cmd == "/stats":
            self.print_stats()
            return True
        elif cmd == "/soul":
            self.print_soul()
            return True
        elif cmd == "/sync":
            self.slash_action_count += 1
            await self.sync_brain("manual_sync", announce=True)
            return True
        elif cmd == "/state":
            self.print_state()
            return True
        elif cmd == "/compact":
            self.slash_action_count += 1
            await self.compact_conversation()
            return True
        elif cmd == "/history":
            self.print_history()
            return True
        elif cmd == "/export":
            self.slash_action_count += 1
            await self.export_session()
            return True
        elif cmd == "/memo":
            text = " ".join(parts[1:]).strip()
            if not text:
                console.print("\n[bold yellow]Usage: /memo <your note text>[/]\n")
            else:
                await self.quick_memo(text)
                self.slash_action_count += 1
            return True
        elif cmd == "/status":
            self.show_koda_status()
            return True
        elif cmd == "/cron":
            sub = parts[1] if len(parts) > 1 else ""

            if not sub:
                # List all jobs
                jobs = load_cron_jobs()
                if not jobs:
                    console.print("\n[dim]No cron jobs scheduled. Use /cron add to create one.[/]\n")
                    return True
                tbl = Table(title="⏰ Koda Cron Jobs", box=ROUNDED, border_style="#ffd700")
                tbl.add_column("#", width=3)
                tbl.add_column("Name", width=20)
                tbl.add_column("Task", width=30)
                tbl.add_column("Every", width=8)
                tbl.add_column("Delivery", width=11)
                tbl.add_column("Next run", width=10)
                tbl.add_column("Status", width=8)
                _delivery_fmt = {
                    "chat":       "[#7ec8a0]chat[/]",
                    "telegram":   "[#a78bfa]telegram[/]",
                    "background": "[dim]silent[/]",
                }
                for i, job in enumerate(jobs, 1):
                    delivery = getattr(job, "delivery", "chat")
                    status = "[green]on[/]" if job.enabled else "[yellow]paused[/]"
                    tbl.add_row(
                        str(i),
                        job.name[:20],
                        job.task[:30],
                        f"{job.interval_minutes}m",
                        _delivery_fmt.get(delivery, delivery),
                        job.next_run_eta(),
                        status,
                    )
                console.print()
                console.print(tbl)
                console.print()

            elif sub == "add":
                # /cron add "name" "task description" <schedule_or_minutes> [model] [delivery]
                # schedule_or_minutes: integer minutes, "daily@HH:MM", or "weekly@DOW@HH:MM"
                remaining = parts[2:]
                name = remaining[0].strip('"') if len(remaining) > 0 else None
                task = remaining[1].strip('"') if len(remaining) > 1 else None
                interval = 0
                schedule = None
                model = None
                delivery = None
                if len(remaining) > 2:
                    raw_sched = remaining[2]
                    if raw_sched.startswith("daily@") or raw_sched.startswith("weekly@"):
                        schedule = raw_sched
                    else:
                        try:
                            interval = int(raw_sched)
                        except ValueError:
                            pass
                if len(remaining) > 3:
                    model = remaining[3] if remaining[3].lower() != "default" else None
                if len(remaining) > 4 and remaining[4] in ("chat", "telegram", "background"):
                    delivery = remaining[4]

                if not name:
                    console.print("[bold #ffd700]Job name: [/]", end="")
                    name = input().strip()
                if not task:
                    console.print("[bold #ffd700]Task description: [/]", end="")
                    task = input().strip()
                if not schedule and interval == 0:
                    console.print("[bold #ffd700]Schedule (e.g. daily@04:00, weekly@mon@09:00) or interval in minutes: [/]", end="")
                    raw_sched = input().strip()
                    if raw_sched.startswith("daily@") or raw_sched.startswith("weekly@"):
                        schedule = raw_sched
                    else:
                        try:
                            interval = int(raw_sched)
                        except ValueError:
                            console.print("\n[bold yellow]Invalid schedule — use daily@HH:MM, weekly@DOW@HH:MM, or a number of minutes.[/]\n")
                            return True
                if delivery is None:
                    console.print("[bold #ffd700]Delivery (chat / telegram / background) [chat]: [/]", end="")
                    d_raw = input().strip().lower()
                    delivery = d_raw if d_raw in ("chat", "telegram", "background") else "chat"
                if not name or not task:
                    console.print("\n[bold yellow]Name and task description are required.[/]\n")
                    return True

                from koda_durable_agent.cron_runner import DELIVERY_MODES
                job = add_cron_job(name, task, interval, model=model, delivery=delivery, schedule=schedule)
                _dlabel = {"chat": "→ chat", "telegram": "→ Telegram", "background": "silent (log only)"}
                console.print(
                    f"\n[bold #ffd700]✓ Cron job added[/] [dim]id: {job.id}[/]\n"
                    f"  [bold]{job.name}[/] — {job.schedule_label()}\n"
                    f"  Next run: {job.next_run_eta()}\n"
                    f"  Delivery: {_dlabel.get(delivery, delivery)}\n"
                )
                self._inject_system_note(
                    f"Cron job '{job.name}' was just created. Schedule: {job.schedule_label()}. "
                    f"Task: {job.task}. Delivery: {delivery}. Next run: {job.next_run_eta()}. "
                    f"Job ID: {job.id}."
                )
                return True

            elif sub == "delete" and len(parts) > 2:
                job = self._cron_resolve_job(parts[2])
                if not job:
                    console.print(f"\n[bold yellow]No job found for '{parts[2]}'[/]\n")
                    return True
                console.print(f"\n[bold yellow]Delete '{job.name}'? (y/n): [/]", end="")
                confirm = input().strip().lower()
                if confirm == "y":
                    remove_cron_job(job.id)
                    console.print(f"[dim]Deleted: {job.name}[/]\n")
                    self._inject_system_note(f"Cron job '{job.name}' (id: {job.id}) was just deleted.")
                else:
                    console.print("[dim]Cancelled.[/]\n")
                return True

            elif sub == "pause" and len(parts) > 2:
                job = self._cron_resolve_job(parts[2])
                if not job:
                    console.print(f"\n[bold yellow]No job found for '{parts[2]}'[/]\n")
                    return True
                update_cron_job(job.id, enabled=False)
                console.print(f"\n[yellow]⏸ Paused: {job.name}[/]\n")
                self._inject_system_note(f"Cron job '{job.name}' was just paused.")
                return True

            elif sub == "resume" and len(parts) > 2:
                job = self._cron_resolve_job(parts[2])
                if not job:
                    console.print(f"\n[bold yellow]No job found for '{parts[2]}'[/]\n")
                    return True
                update_cron_job(job.id, enabled=True)
                console.print(f"\n[bold #7ec8a0]▶ Resumed: {job.name}[/]\n")
                self._inject_system_note(f"Cron job '{job.name}' was just resumed.")
                return True

            elif sub == "model" and len(parts) > 2:
                job = self._cron_resolve_job(parts[2])
                if not job:
                    console.print(f"\n[bold yellow]No job found for '{parts[2]}'[/]\n")
                    return True
                if len(parts) > 3:
                    new_model = None if parts[3].lower() == "default" else parts[3]
                    update_cron_job(job.id, model=new_model)
                    label = new_model or f"default ({self._prefs.get('default_model', 'gemini-2.5-flash').split('/')[-1]})"
                    console.print(f"\n[bold #7ec8a0]✓ {job.name} model → {label}[/]\n")
                else:
                    current = job.model or f"default ({self._prefs.get('default_model', 'gemini-2.5-flash').split('/')[-1]})"
                    console.print(f"\n[dim]Current model for {job.name}: {current}[/]")
                    console.print("[bold #ffd700]New model (or 'default'): [/]", end="")
                    new_model_str = input().strip()
                    new_model = None if new_model_str.lower() == "default" else new_model_str
                    update_cron_job(job.id, model=new_model)
                    label = new_model or f"default ({self._prefs.get('default_model', 'gemini-2.5-flash').split('/')[-1]})"
                    console.print(f"[bold #7ec8a0]✓ Model → {label}[/]\n")
                return True

            elif sub == "delivery" and len(parts) > 2:
                job = self._cron_resolve_job(parts[2])
                if not job:
                    console.print(f"\n[bold yellow]No job found for '{parts[2]}'[/]\n")
                    return True
                if len(parts) > 3 and parts[3] in ("chat", "telegram", "background"):
                    new_delivery = parts[3]
                    update_cron_job(job.id, delivery=new_delivery)
                    _dlabel = {"chat": "→ chat", "telegram": "→ Telegram", "background": "silent (log only)"}
                    console.print(f"\n[bold #7ec8a0]✓ {job.name} delivery → {_dlabel[new_delivery]}[/]\n")
                else:
                    current = getattr(job, "delivery", "chat")
                    console.print(f"\n[dim]Current delivery for {job.name}: {current}[/]")
                    console.print("[bold #ffd700]New delivery (chat / telegram / background): [/]", end="")
                    d_raw = input().strip().lower()
                    if d_raw not in ("chat", "telegram", "background"):
                        console.print("[bold yellow]Invalid — must be chat, telegram, or background[/]\n")
                        return True
                    update_cron_job(job.id, delivery=d_raw)
                    _dlabel = {"chat": "→ chat", "telegram": "→ Telegram", "background": "silent (log only)"}
                    console.print(f"[bold #7ec8a0]✓ Delivery → {_dlabel[d_raw]}[/]\n")
                return True

            elif sub == "now" and len(parts) > 2:
                job = self._cron_resolve_job(parts[2])
                if not job:
                    console.print(f"\n[bold yellow]No job found for '{parts[2]}'[/]\n")
                    return True
                console.print(f"\n[dim]Triggering: {job.name}...[/]")
                asyncio.create_task(self._cron_runner.trigger_job(job.id))
                return True

            elif sub == "log" and len(parts) > 2:
                job = self._cron_resolve_job(parts[2])
                if not job:
                    console.print(f"\n[bold yellow]No job found for '{parts[2]}'[/]\n")
                    return True
                from koda_durable_agent.cron_runner import CRON_LOG_DIR
                log_path = CRON_LOG_DIR / f"{job.id}.log"
                if not log_path.exists():
                    console.print(f"\n[dim]No log yet for '{job.name}'. Run the job first.[/]\n")
                    return True
                lines = log_path.read_text().strip().splitlines()[-5:]
                console.print()
                console.print(Panel(
                    "\n".join(lines),
                    title=f"📋 Last 5 runs — {job.name}",
                    border_style="#ffd700",
                ))
                console.print()
                return True

            else:
                console.print(
                    "\n[bold #ffd700]Cron commands:[/]\n"
                    "  /cron                        — list all jobs\n"
                    "  /cron add [name] [task] [min] — add a new job\n"
                    "  /cron pause <#>              — pause a job\n"
                    "  /cron resume <#>             — resume a job\n"
                    "  /cron delete <#>             — delete a job\n"
                    "  /cron model <#> [model]      — set model (or 'default')\n"
                    "  /cron now <#>                — trigger immediately\n"
                    "  /cron log <#>                — show last 5 run results\n"
                )
            return True

        elif cmd == "/heartbeat":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "start":
                self._heartbeat_runner.set_enabled(True)
                self._heartbeat_runner.start()
                console.print("\n[bold #7ec8a0]✓ Heartbeat started[/]\n")
            elif sub == "stop":
                self._heartbeat_runner.set_enabled(False)
                self._heartbeat_runner.stop()
                console.print("\n[bold yellow]⏸ Heartbeat stopped[/]\n")
            elif sub == "now":
                console.print("\n[dim]Triggering heartbeat...[/]")
                asyncio.create_task(self._heartbeat_runner.trigger_now())
            elif sub == "set" and len(parts) > 2:
                try:
                    mins = int(parts[2])
                    self._heartbeat_runner.set_interval(mins)
                    console.print(f"\n[bold #7ec8a0]✓ Heartbeat interval → {mins} minutes[/]\n")
                except ValueError:
                    console.print("\n[bold yellow]Usage: /heartbeat set <minutes>[/]\n")
            else:
                running = self._heartbeat_runner.is_running()
                status_str = "[green]running[/]" if running else "[yellow]stopped[/]"
                # Read last few log entries for recent activity
                recent_runs: list[dict] = []
                try:
                    if HEARTBEAT_LOG.exists():
                        lines = HEARTBEAT_LOG.read_text().strip().splitlines()
                        for line in lines[-5:]:
                            try:
                                recent_runs.append(json.loads(line))
                            except Exception:
                                pass
                except Exception:
                    pass
                last_entry = recent_runs[-1] if recent_runs else None
                last_result = ""
                if last_entry:
                    s = last_entry.get("status", "?")
                    summary = last_entry.get("summary", "")
                    delta = last_entry.get("agent_delta", 0.0)
                    ahas_count = len(last_entry.get("ahas", []))
                    color = "#7ec8a0" if s == "completed" and not summary.startswith("Reflection error") else "yellow"
                    last_result = f"[{color}]{s}[/] · delta {delta:+.1f}"
                    if ahas_count:
                        last_result += f" · {ahas_count} aha(s)"
                    if summary:
                        last_result += f"\n  [dim]{summary[:80]}[/]"
                # Determine heartbeat model (mirror fix in heartbeat.py)
                raw_model = self._prefs.get("default_model", "gemini-2.5-flash")
                if raw_model.startswith("ollama/") or ":" in raw_model.split("/")[-1]:
                    hb_model = "gemini-2.5-flash (fallback)"
                elif "/" in raw_model:
                    hb_model = raw_model.split("/")[-1]
                else:
                    hb_model = raw_model
                hb_table = Table(title="🫀 Heartbeat Status", box=ROUNDED, border_style="#7ec8a0")
                hb_table.add_column("Field", style="bold #8b5a2b")
                hb_table.add_column("Value")
                hb_table.add_row("Status", status_str)
                hb_table.add_row("Interval", f"{self._heartbeat_runner.interval_minutes} min")
                hb_table.add_row("Last run", self._heartbeat_runner.last_run() or "never")
                hb_table.add_row("Next run", self._heartbeat_runner.next_run_eta())
                hb_table.add_row("Last result", last_result or "[dim]none yet[/]")
                hb_table.add_row("Total runs (log)", str(last_entry.get("heartbeat", "?") + 1 if last_entry else 0))
                hb_table.add_row("Runs this session", str(self._heartbeat_runner._heartbeat_count))
                hb_table.add_row("CCI after last run", f"{last_entry['cci_after']:.2f}" if last_entry else "—")
                hb_table.add_row("Model", hb_model)
                console.print()
                console.print(hb_table)
                if recent_runs:
                    console.print("[dim]Recent runs:[/]")
                    for entry in recent_runs[-3:]:
                        ts = entry.get("ts", "?")
                        s = entry.get("status", "?")
                        summ = entry.get("summary", "")[:60]
                        ahas = len(entry.get("ahas", []))
                        color = "#7ec8a0" if s == "completed" and not summ.startswith("Reflection error") else "yellow"
                        aha_str = f" · {ahas} aha(s)" if ahas else ""
                        console.print(f"  [dim]{ts}[/]  [{color}]{s}[/]{aha_str}  [dim]{summ}[/]")
                console.print()
            return True
        elif cmd == "/sleep":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "now":
                if self._sleep_runner:
                    console.print("\n[dim]Triggering sleep cycle...[/]")
                    asyncio.create_task(self._sleep_runner.trigger_now())
                else:
                    console.print("\n[yellow]Sleep runner not active[/]")
            elif sub == "stop":
                if self._sleep_runner:
                    self._sleep_runner.stop()
                    console.print("\n[yellow]⏸ Sleep cycle stopped[/]\n")
            elif sub == "start":
                if self._sleep_runner:
                    self._sleep_runner.start()
                    console.print("\n[green]✓ Sleep cycle started[/]\n")
            else:
                # Status display
                sl_table = Table(title="🌙 Sleep Cycle Status", box=ROUNDED, border_style="#a78bfa")
                sl_table.add_column("Field", style="bold #8b5a2b")
                sl_table.add_column("Value")
                if self._sleep_runner:
                    running = self._sleep_runner.is_running()
                    sl_table.add_row("Status", "[green]running[/]" if running else "[yellow]stopped[/]")
                    sl_table.add_row("Fires at", "2:00 AM daily")
                    sl_table.add_row("Next sleep", self._sleep_runner.next_sleep_eta())
                    sl_table.add_row("Last sleep", self._sleep_runner.last_sleep() or "never")
                    q = self._prefs.get("sleep_cycle", {}).get("last_quality")
                    if q is not None:
                        sl_table.add_row("Last quality", f"{q:.2f}")
                    sl_table.add_row("Total sleeps", str(self._sleep_runner._sleep_count))
                    dreams_lines = 0
                    try:
                        if DREAMS_PATH.exists():
                            dreams_lines = sum(1 for l in DREAMS_PATH.read_text().splitlines() if l.startswith("## "))
                    except Exception:
                        pass
                    sl_table.add_row("Dream entries", str(dreams_lines))
                else:
                    sl_table.add_row("Status", "[red]not initialized[/]")
                console.print()
                console.print(sl_table)
                console.print()
            return True
        elif cmd == "/aha":
            text = " ".join(parts[1:]).strip()
            if not text:
                last_prompt = self.recent_prompts[-1] if self.recent_prompts else ""
                console.print(f"\n[dim]Last prompt: {last_prompt[:80]}[/]")
                console.print("[bold #7ec8a0]Describe this aha moment: [/]", end="")
                text = input().strip()
            if text:
                new_score = self.cci.record_aha_jordan(text)
                _, tier_emoji, tier_color = self.cci.tier_info()
                console.print(
                    f"\n[bold #7ec8a0]✓ Aha captured[/] "
                    f"[dim]+{CCITracker.DELTA_AHA_JORDAN} XP · CCI → {new_score:.2f} {tier_emoji}[/]\n"
                )
                await self._check_tier_up()
            return True
        elif cmd == "/rate":
            if len(parts) < 2:
                console.print("\n[bold yellow]Usage: /rate [1-5]  (rate the last response)[/]\n")
                return True
            try:
                rating = int(parts[1])
                if not 1 <= rating <= 5:
                    raise ValueError("out of range")
                new_score = self.cci.record_rate(rating)
                labels = {
                    1: "noted — learning from it",
                    2: "ok — could be sharper",
                    3: "neutral",
                    4: "good work",
                    5: "breakthrough!",
                }
                console.print(
                    f"\n[bold #7ec8a0]✓ Rated {rating}/5[/] — {labels[rating]}  "
                    f"[dim]CCI → {new_score:.2f}[/]\n"
                )
                await self._check_tier_up()
            except ValueError:
                console.print("\n[bold yellow]Rating must be 1-5[/]\n")
            return True
        elif cmd == "/cci":
            s = self.cci.summary()
            tc = s["tier_color"]
            next_t = s["next_threshold"]
            progress_row = ""
            if next_t:
                pct = min(100.0, s["score"] / next_t * 100)
                progress_row = f"{self._render_meter(pct / 100, width=14)} {pct:.0f}% → {next_t}"

            last_hb_ts, last_hb_summary = "never", ""
            try:
                if HEARTBEAT_LOG.exists():
                    lines = HEARTBEAT_LOG.read_text().strip().splitlines()
                    if lines:
                        entry = json.loads(lines[-1])
                        last_hb_ts = entry.get("ts", "unknown")
                        last_hb_summary = entry.get("summary", "")
            except Exception:
                pass

            recent_ahas: list = []
            try:
                if CCITracker.AHA_PATH.exists():
                    blocks = CCITracker.AHA_PATH.read_text().split("###")
                    for block in blocks[-4:]:
                        lines_b = block.strip().splitlines()
                        if len(lines_b) >= 3:
                            recent_ahas.append(lines_b[2][:80])
            except Exception:
                pass

            # Progress bar based on XP toward next XP tier threshold
            progress_row = ""
            if next_t:
                cur_tier_xp = max(t[0] for t in CCITracker.TIERS if t[0] <= s["xp"])
                span = next_t - cur_tier_xp
                pct = min(100.0, (s["xp"] - cur_tier_xp) / span * 100) if span > 0 else 100.0
                progress_row = f"{self._render_meter(pct / 100, width=14)} {pct:.0f}% · {s['xp_to_next']:.1f} XP to go"

            cci_table = Table(title="⚡ Koda CCI Dashboard", box=ROUNDED, border_style=tc)
            cci_table.add_column("", style="bold #8b5a2b", width=20)
            cci_table.add_column("")
            cci_table.add_row("CCI", f"[{tc}]{s['score']:.2f}[/]  [dim](0-1 scale)[/]")
            cci_table.add_row("XP", f"[{tc}]{s['xp']:.1f}[/]  [dim](raw experience)[/]")
            cci_table.add_row("Tier", f"[{tc}]{s['tier_emoji']} {s['tier_name']}[/]")
            if progress_row:
                cci_table.add_row("Next tier", progress_row)
            cci_table.add_row("Mean CCI", f"{s['mean']:.2f}")
            cci_table.add_row("Trend", s["sparkline"])
            cci_table.add_row("Autonomy", s["autonomy"])
            cci_table.add_row("Total turns", str(s["total_turns"]))
            cci_table.add_row("Aha moments", str(s["total_ahas"]))
            cci_table.add_row("Heartbeats", str(s["total_heartbeats"]))
            cci_table.add_row("Sleeps", str(s["total_sleeps"]))
            cci_table.add_row("Last heartbeat", last_hb_ts)
            if last_hb_summary:
                cci_table.add_row("└─ summary", f"[dim]{last_hb_summary}[/]")
            if self._heartbeat_runner:
                cci_table.add_row("Next heartbeat", self._heartbeat_runner.next_run_eta())
            if self._sleep_runner:
                cci_table.add_row("Next sleep", self._sleep_runner.next_sleep_eta())
                last_s = self._sleep_runner.last_sleep()
                if last_s:
                    q = self._prefs.get("sleep_cycle", {}).get("last_quality")
                    q_str = f"  quality {q:.2f}" if q else ""
                    cci_table.add_row("Last sleep", f"{last_s}{q_str}")
            console.print()
            console.print(cci_table)
            if recent_ahas:
                console.print("\n[bold #8b5a2b]Recent Ahas:[/]")
                for aha in recent_ahas:
                    console.print(f"  [dim]• {aha}[/]")
            console.print()
            return True
        elif cmd == "/evolve":
            sub = parts[1] if len(parts) > 1 else ""
            if sub == "approve" and len(parts) > 2:
                await self._evolve_approve(parts[2])
            elif sub == "reject" and len(parts) > 2:
                await self._evolve_reject(parts[2])
            elif sub == "rollback" and len(parts) > 2 and parts[2] == "soul":
                await self._evolve_rollback_soul()
            elif sub == "learned":
                self._evolve_show_learned()
            else:
                self._evolve_show_queue()
            return True
        elif cmd == "/setdefault":
            self._set_default_model(self.current_model)
            model_short = self.current_model.split("/")[-1]
            console.print(f"\n[bold #7ec8a0]✓ Default model saved:[/] [bold #ffd700]{model_short}[/]  [dim]Koda will start here next time.[/]\n")
            return True
        elif cmd == "/telegram":
            self.show_telegram_status()
            return True
        elif cmd == "/reload":
            self.reload_instructions()
            self.slash_action_count += 1
            return True
        elif cmd == "/status":
            self.show_status()
            return True
        elif cmd == "/tools":
            self.show_tools()
            return True
        elif cmd in ("/personas", "/persona"):
            if cmd == "/persona" and len(parts) >= 2:
                # Direct switch: /persona hmm
                await self.switch_persona(parts[1])
            else:
                # Interactive picker for both /personas and bare /persona
                chosen = self._interactive_persona_picker()
                if chosen is not None:
                    await self.switch_persona(chosen)
            self.slash_action_count += 1
            return True
        elif cmd == "/soul-edit":
            self.soul_edit()
            self.slash_action_count += 1
            return True
        elif cmd == "/new":
            await self.close_agent()
            self.session_id = self._build_default_session_id()
            self.last_seen_step_index = -1
            self._or_messages = []
            self._init_session_trajectory()
            self._write_live_state("new_thread")
            await self.sync_brain("new_thread")
            console.print(f"\n[bold #7ec8a0]🌲 New forest thread started: {self.session_id}[/]\n")
            return True
        elif cmd == "/clear":
            console.clear()
            self.print_welcome_banner()
            return True
        elif cmd in ("/exit", "/quit"):
            console.print("\n[bold cyan]Saving session state and shutting down Koda pathways gracefully...[/]")
            if self.sync_task is not None:
                self.sync_task.cancel()
                try:
                    await self.sync_task
                except asyncio.CancelledError:
                    pass
            if self.telegram_task is not None:
                self.telegram_task.cancel()
                try:
                    await self.telegram_task
                except asyncio.CancelledError:
                    pass
            await self.finalize_session("exit_command")
            sys.exit(0)

        # Check custom skills created by Koda
        from koda_durable_agent.koda_skill_tools import _load as _load_skills
        for skill in _load_skills():
            if cmd == skill.get("command", "").lower():
                self._reload_skill_palette()   # keep palette fresh
                await self.run_chat_turn(skill["prompt"])
                return True

        return False

    def _is_ollama_model(self, model: str) -> bool:
        return model.startswith("ollama/")

    def _get_openai_compat_client(self, base_url: str, api_key: str):
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=api_key, base_url=base_url)

    def _inject_system_note(self, note: str) -> None:
        """Append a system-context note to the OpenAI-compat conversation history.
        Keeps Koda aware of TUI actions (cron jobs, storage runs, etc.) that happen
        outside of chat turns."""
        if self._or_messages and self._or_messages[0].get("role") == "system":
            # Append to existing system message rather than adding a new system block
            self._or_messages[0]["content"] += f"\n\n[TUI action — {__import__('datetime').datetime.now().strftime('%H:%M')}]: {note}"
        else:
            self._or_messages.insert(0, {"role": "system", "content": f"[TUI action]: {note}"})

    def _build_openai_tools(self) -> list:
        """Generate OpenAI-compatible tool schemas from Koda's tool functions."""
        import inspect, re
        from typing import Union
        from koda_durable_agent.brain import get_tools
        funcs = get_tools()

        _type_map = {str: "string", bool: "boolean", int: "integer", float: "number"}
        schemas = []
        for func in funcs:
            sig = inspect.signature(func)
            doc = inspect.getdoc(func) or ""
            description = re.split(r'\n\s*Args:\s*\n', doc)[0].strip()

            arg_descs: dict = {}
            m = re.search(r'Args:\s*\n(.*?)(?:\n\s*(?:Returns|Raises):|\Z)', doc, re.DOTALL)
            if m:
                for line in m.group(1).splitlines():
                    am = re.match(r'\s+(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)', line)
                    if am:
                        arg_descs[am.group(1)] = am.group(2).strip()

            properties: dict = {}
            required: list = []
            for pname, param in sig.parameters.items():
                ann = param.annotation
                json_type = "string"
                if ann != inspect.Parameter.empty:
                    origin = getattr(ann, "__origin__", None)
                    if origin is Union:
                        inner = [a for a in ann.__args__ if a is not type(None)]
                        ann = inner[0] if inner else ann
                    json_type = _type_map.get(ann, "string")
                prop: dict = {"type": json_type}
                if pname in arg_descs:
                    prop["description"] = arg_descs[pname]
                properties[pname] = prop
                if param.default is inspect.Parameter.empty:
                    required.append(pname)

            schemas.append({
                "type": "function",
                "function": {
                    "name": func.__name__,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return schemas

    async def _dispatch_tool_call(self, name: str, args_json: str) -> str:
        """Execute a tool by name and return its output as a string."""
        import json as _json
        from koda_durable_agent.brain import get_tools
        _tool_map = {f.__name__: f for f in get_tools()}

        func = _tool_map.get(name)
        if not func:
            return f"Error: unknown tool '{name}'"

        try:
            kwargs = _json.loads(args_json) if args_json else {}
        except Exception:
            kwargs = {}

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: func(**kwargs))
            return str(result)[:4000]
        except Exception as e:
            self._session_error_count += 1
            return f"Tool error ({name}): {e}"

    async def _run_openai_compat_turn(self, prompt: str, client, api_model: str):
        """Agentic chat turn for any OpenAI-compatible backend. Supports tool calling."""
        console.print()
        console.print(self._response_rule())

        self.recent_prompts.append(prompt)
        self.recent_prompts = self.recent_prompts[-8:]

        if not self._or_messages:
            from koda_durable_agent.brain import load_instructions
            from koda_durable_agent.koda_tools import list_koda_cron_jobs
            system_content = load_instructions()
            try:
                cron_snapshot = list_koda_cron_jobs()
                system_content += f"\n\n### CURRENT CRON JOBS (live snapshot at session start):\n{cron_snapshot}"
            except Exception:
                pass
            self._or_messages = [{"role": "system", "content": system_content}]

        self._or_messages.append({"role": "user", "content": prompt})

        tools = self._build_openai_tools()

        or_phrases = [
            "Routing through the forest relay...",
            "Sending smoke signals upstream...",
            "Negotiating with the cloud spirits...",
            "Pinging the mountain gateway...",
            "Waiting on the stream to open up...",
            "Checking the satellite trail...",
            "Sniffing the packet breeze...",
            "Asking the distant oracle...",
            "Warming the remote campfire...",
            "Hunting for tokens downstream...",
        ]

        final_text = ""
        MAX_TOOL_LOOPS = 8

        for _loop_i in range(MAX_TOOL_LOOPS):
            spinner = Spinner(self._provider_spinner(), text="  Koda is thinking… 🐾", style=f"bold {self._provider_color()}")
            streamed_text = ""
            tc_buf: dict = {}   # index → {id, name, arguments} accumulated from stream
            err_msg = ""

            with Live(spinner, console=console, auto_refresh=True, refresh_per_second=4) as live:
                async def _cycle_phrases(sp=spinner):
                    try:
                        while True:
                            await asyncio.sleep(3.5)
                            sp.update(text=f"  [italic]{random.choice(or_phrases)}[/]")
                    except asyncio.CancelledError:
                        pass

                phrase_task = asyncio.create_task(_cycle_phrases())
                first_chunk = True

                async def _cancel_phrases():
                    nonlocal first_chunk
                    if first_chunk:
                        first_chunk = False
                        phrase_task.cancel()
                        try:
                            await phrase_task
                        except asyncio.CancelledError:
                            pass

                async def _stream_response(stream):
                    nonlocal streamed_text
                    async for chunk in stream:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        # Accumulate tool call deltas
                        if delta.tool_calls:
                            await _cancel_phrases()
                            for tcd in delta.tool_calls:
                                idx = tcd.index
                                if idx not in tc_buf:
                                    tc_buf[idx] = {
                                        "id": tcd.id or "",
                                        "name": (tcd.function.name or "") if tcd.function else "",
                                        "arguments": "",
                                    }
                                if tcd.function and tcd.function.arguments:
                                    tc_buf[idx]["arguments"] += tcd.function.arguments
                        # Stream text tokens
                        if delta.content:
                            await _cancel_phrases()
                            streamed_text += delta.content
                            live.update(Markdown(streamed_text), refresh=True)

                try:
                    stream = await client.chat.completions.create(
                        model=api_model,
                        messages=self._or_messages,
                        tools=tools,
                        tool_choice="auto",
                        stream=True,
                    )
                    await _stream_response(stream)
                except Exception as e:
                    err_str = str(e)
                    # Some models don't support tool calling — retry without tools
                    if "tool" in err_str.lower() or "function" in err_str.lower() or "400" in err_str:
                        tc_buf.clear()
                        streamed_text = ""
                        first_chunk = True
                        try:
                            stream2 = await client.chat.completions.create(
                                model=api_model,
                                messages=self._or_messages,
                                stream=True,
                            )
                            await _stream_response(stream2)
                        except Exception as e2:
                            err_msg = str(e2)
                    else:
                        err_msg = err_str

                # Ensure phrase cycler is stopped
                phrase_task.cancel()
                try:
                    await phrase_task
                except asyncio.CancelledError:
                    pass

                if err_msg:
                    if "429" in err_msg or "rate" in err_msg.lower():
                        live.update(Text("💡 Rate limit hit — try /model to switch.", style="bold yellow"), refresh=True)
                    else:
                        live.update(Text(f"❌ {err_msg}", style="bold red"), refresh=True)
                    self._or_messages.pop()
                    console.print()
                    return

            # Handle tool calls assembled from stream
            if tc_buf:
                tool_calls = [
                    {
                        "id": tc_buf[i]["id"],
                        "type": "function",
                        "function": {"name": tc_buf[i]["name"], "arguments": tc_buf[i]["arguments"]},
                    }
                    for i in sorted(tc_buf.keys())
                ]
                self._or_messages.append({
                    "role": "assistant",
                    "content": streamed_text or "",
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    console.print(f"[dim]⚙️  {tc['function']['name']}[/]")
                    result = await self._dispatch_tool_call(tc["function"]["name"], tc["function"]["arguments"])
                    self._session_tool_count += 1
                    self.cci.record_tool()
                    self._or_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                continue

            # No tool calls — final streamed response already rendered in Live
            final_text = streamed_text
            break

        if final_text:
            self._or_messages.append({"role": "assistant", "content": final_text})

        console.print()
        self.turn_count += 1
        self.last_turn_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        if final_text.strip():
            self.cci.record_turn()
            await self._check_tier_up()
            self.print_turn_telemetry()
        else:
            self.cci.record_error()
            self._session_error_count += 1
        await self.sync_brain("chat_turn")

    async def run_chat_turn_openrouter(self, prompt: str):
        api_key = settings.OPENROUTER_API_KEY or os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            console.print("[bold red]❌ OPENROUTER_API_KEY not set. Run: koda setup[/]\n")
            return
        client = self._get_openai_compat_client("https://openrouter.ai/api/v1", api_key)
        api_model = self.current_model.removeprefix("openrouter/")
        await self._run_openai_compat_turn(prompt, client, api_model)

    async def run_chat_turn_ollama(self, prompt: str):
        base_url = f"{settings.OLLAMA_BASE_URL}/v1"
        client = self._get_openai_compat_client(base_url, "ollama")
        api_model = self.current_model.removeprefix("ollama/")
        await self._run_openai_compat_turn(prompt, client, api_model)

    async def run_chat_turn(self, prompt: str):
        """Streams response tokens dynamically inline in the terminal scrollback."""
        if self._is_openrouter_model(self.current_model):
            await self.run_chat_turn_openrouter(prompt)
            return

        if self._is_ollama_model(self.current_model):
            await self.run_chat_turn_ollama(prompt)
            return

        console.print()

        async with self._agent_lock:
            try:
                agent = await self.get_agent()
            except Exception as e:
                console.print(f"[bold red]❌ Failed to initialize Agent brain connection: {e}[/]\n")
                return

            streamed_text = ""
            self.last_prompt_tokens = 0
            self.last_candidates_tokens = 0
            self.last_thoughts_tokens = 0
            turn_start_step = self.last_seen_step_index
            console.print(self._response_rule())
            self.recent_prompts.append(prompt)
            self.recent_prompts = self.recent_prompts[-8:]

            cheeky_phrases = [
                "Sniffing out a berry trail...",
                "Catching salmon at the run...",
                "Asking the Great Spirits...",
                "Climbing a tall pine tree...",
                "Telling Kenai a really long story...",
                "Looking for sweet honeycomb...",
                "Sharing warm bear hugs...",
                "Riding on a woolly mammoth...",
                "Scratching my back against a tree...",
                "Staring at the pretty lights in the sky...",
                "Waddling through the high grass...",
                "Chasing pinecones down the hill...",
                "Talking to the big moose...",
                "Peeking out of the cozy den...",
                "Checking the route to Mascoutah...",
                "Looking up the Edwardsville accounts...",
                "Radioing the field crew...",
                "Reviewing the schedule...",
                "Digging through the Codex archive...",
                "Tracing the signal back to base...",
                "Weighing the salmon catch...",
                "Reading the river current...",
                "Mapping the next trail section...",
                "Consulting the elders...",
                "Warming up by the campfire...",
                "Counting stars in the clearing...",
                "Sharpening the claws...",
                "Following the honey scent...",
                "Tracking paw prints in the snow...",
                "Listening to the wind...",
            ]

            # Use Spinner's native text parameter for the cycling phrase display
            spinner = Spinner(self._provider_spinner(), text="  Koda is sniffing around... 🐾", style=f"bold {self._provider_color()}")

            with Live(spinner, console=console, auto_refresh=True, refresh_per_second=4) as live:
                # Background task to cycle cheeky thinking phrases while waiting for first tokens
                async def cycle_phrases():
                    try:
                        while True:
                            await asyncio.sleep(3.5)
                            spinner.update(text=f"  [italic yellow]{random.choice(cheeky_phrases)}[/]")
                    except asyncio.CancelledError:
                        pass

                phrase_task = asyncio.create_task(cycle_phrases())

                try:
                    # Chat asynchronously
                    response = await agent.chat(prompt)

                    first_chunk = True
                    async for chunk in response.chunks:
                        if first_chunk:
                            first_chunk = False
                            # Cancel phrase cycling once model response starts streaming
                            phrase_task.cancel()
                            try:
                                await phrase_task
                            except asyncio.CancelledError:
                                pass

                        chunk_step_index = getattr(chunk, "step_index", turn_start_step)
                        self.last_seen_step_index = max(self.last_seen_step_index, chunk_step_index)
                        if chunk_step_index <= turn_start_step:
                            continue

                        # Show tool call callouts in the spinner while Koda runs tools
                        if isinstance(chunk, ToolCall):
                            tool_name = getattr(chunk, "name", None) or getattr(chunk, "function_name", "tool")
                            spinner.update(text=f"  [dim #ffd700]⚙ running[/] [bold #ffd700]{tool_name}[/] [dim #ffd700]…[/]")
                            continue
                        if isinstance(chunk, ToolResult):
                            spinner.update(text=f"  [dim #7ec8a0]✓ tool done — processing…[/]")
                            continue

                        chunk_text = await self._extract_chunk_text(chunk)
                        if not chunk_text:
                            continue

                        streamed_text += chunk_text
                        live.update(Markdown(streamed_text), refresh=True)

                    if not streamed_text.strip():
                        fallback_text = await self._extract_response_text(response)
                        if fallback_text:
                            streamed_text = str(fallback_text)
                            live.update(Markdown(streamed_text), refresh=True)

                    # Safe token count updates
                    usage = getattr(response, 'usage_metadata', None)
                    if usage:
                        self._record_usage_snapshot(usage)

                except Exception as e:
                    # Ensure background phrase cycler task is cancelled on exception
                    phrase_task.cancel()
                    try:
                        await phrase_task
                    except asyncio.CancelledError:
                        pass

                    err_str = str(e)

                    # Check for standard websockets connection closures (1000, 1001, 1006, etc.)
                    if "1000 (OK)" in err_str or "Connection closed" in err_str or "1006" in err_str:
                        # If we already received text before the socket closed, it means OpenClaw
                        # successfully completed the stream but dropped the connection instead of
                        # keeping it alive. This is a successful turn, so we just ignore the error.
                        if streamed_text.strip():
                            return

                        live.update(Text("  Re-establishing neural connection...", style="dim italic yellow"), refresh=True)
                        # Connection dropped. Reconnect and retry transparently.
                        await self.close_agent()
                        agent = await self.get_agent()

                        try:
                            response = await agent.chat(prompt)
                            first_chunk = True
                            async for chunk in response.chunks:
                                if first_chunk:
                                    first_chunk = False
                                chunk_step_index = getattr(chunk, "step_index", turn_start_step)
                                self.last_seen_step_index = max(self.last_seen_step_index, chunk_step_index)
                                if chunk_step_index <= turn_start_step:
                                    continue
                                if isinstance(chunk, ToolCall):
                                    tool_name = getattr(chunk, "name", None) or getattr(chunk, "function_name", "tool")
                                    spinner.update(text=f"  [dim #ffd700]⚙ running[/] [bold #ffd700]{tool_name}[/] [dim #ffd700]…[/]")
                                    self.cci.record_tool()
                                    self._session_tool_count += 1
                                    continue
                                if isinstance(chunk, ToolResult):
                                    spinner.update(text=f"  [dim #7ec8a0]✓ tool done — processing…[/]")
                                    continue
                                chunk_text = await self._extract_chunk_text(chunk)
                                if not chunk_text:
                                    continue
                                streamed_text += chunk_text
                                live.update(Markdown(streamed_text), refresh=True)

                            if not streamed_text.strip():
                                fallback_text = await self._extract_response_text(response)
                                if fallback_text:
                                    streamed_text = str(fallback_text)
                                    live.update(Markdown(streamed_text), refresh=True)

                            usage = getattr(response, 'usage_metadata', None)
                            if usage:
                                self._record_usage_snapshot(usage)

                            return  # Successfully recovered
                        except Exception as retry_e:
                            err_str = str(retry_e)
                            e = retry_e

                    err_text = f"\n\n[bold red]❌ System Error during turn execution: {e}[/]"

                    # Check for Gemini free tier rate limits
                    if "429" in err_str or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower() or "limit" in err_str.lower():
                        err_text += (
                            "\n\n[bold yellow]💡 Rate Limit Detected:[/]"
                            "\nYou seem to have hit a Gemini API rate limit or daily request quota."
                            "\nTry again in a moment or switch between the available Gemini pathways with [bold cyan]/model[/]."
                        )
                    live.update(Markdown(streamed_text + err_text), refresh=True)

        console.print()
        self.turn_count += 1
        self.last_turn_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        if streamed_text.strip():
            self.cci.record_turn()
            await self._check_tier_up()
            self.print_turn_telemetry()
        else:
            self.cci.record_error()
            self._session_error_count += 1
            runtime_issue = self._recent_runtime_issue()
            if runtime_issue:
                console.print(f"[bold yellow]⚠️ {runtime_issue}[/]")
            else:
                console.print("[bold yellow]⚠️ Koda didn’t bring back any words that turn. The trail may have gone cold or the provider may have shrugged.[/]")
        await self.sync_brain("chat_turn")

    async def start_loop(self):
        """Master CLI keyboard and interactive shell supervisor loop."""
        self.print_welcome_banner()
        _update_check_task = asyncio.create_task(self._check_for_update())
        await self.sync_brain("startup")
        self.sync_task = asyncio.create_task(self._background_sync_loop())

        # --- Boot sequence ---
        from koda_durable_agent.gateway import start_gateway

        telegram_online = bool(settings.TELEGRAM_BOT_TOKEN)
        if telegram_online:
            self.telegram_task = asyncio.create_task(
                start_gateway(message_handler=self._handle_telegram_message)
            )
            self.telegram_task.add_done_callback(
                lambda t: logger.error(f"Telegram gateway crashed: {t.exception()}")
                if not t.cancelled() and t.exception() else None
            )

        # Start heartbeat runner
        self._heartbeat_runner = HeartbeatRunner(
            cci_tracker=self.cci,
            get_session_data=self._get_heartbeat_session_data,
            prefs=self._prefs,
            save_prefs=self._save_prefs,
            notify=self._queue_heartbeat_notice,
        )
        if self._prefs.get("heartbeat", {}).get("enabled", True):
            self._heartbeat_runner.start()

        self._sleep_runner = SleepCycleRunner(
            cci_tracker=self.cci,
            prefs=self._prefs,
            save_prefs=self._save_prefs,
            notify=self._queue_sleep_notice,
        )
        if self._prefs.get("sleep_cycle", {}).get("enabled", True):
            self._sleep_runner.start()

        self._cron_runner = KodaCronRunner(
            prefs=self._prefs,
            save_prefs=self._save_prefs,
            notify=self._queue_cron_notice,
            send_telegram=self._send_telegram_outbound,
        )
        self._cron_runner.start()

        # Wait for update check (should already be done; 2s max grace period)
        try:
            await asyncio.wait_for(_update_check_task, timeout=2.0)
        except Exception:
            pass

        console.print(
            self._build_boot_status(telegram_online)
        )
        console.print()
        # --- End boot sequence ---

        while True:
            try:
                # Toast notices — shown between turns, styled distinct from chat output.
                if self._pending_heartbeat_notice:
                    _hb_notice = self._pending_heartbeat_notice
                    self._pending_heartbeat_notice = None
                    console.print(
                        Panel(
                            f"[dim]{_hb_notice}[/]",
                            border_style="dim #444444",
                            expand=False,
                            padding=(0, 1),
                        )
                    )
                if self._pending_sleep_notice:
                    _sl_notice = self._pending_sleep_notice
                    self._pending_sleep_notice = None
                    console.print(
                        Panel(
                            f"[dim #c8a0f0]{_sl_notice}[/]",
                            border_style="dim #6a4c9c",
                            title="[dim #6a4c9c]sleep cycle[/]",
                            expand=False,
                            padding=(0, 1),
                        )
                    )
                if self._pending_cron_notice:
                    console.print(f"\n[dim #ffd700]{self._pending_cron_notice}[/]\n")
                    self._pending_cron_notice = None

                model_short = self.current_model.split("/")[-1]

                # Wrap non-printing ANSI escapes with \x01 and \x02 to avoid readline wrap offsets
                gold_start   = "\x01\x1b[1;38;5;214m\x02"
                green_start  = "\x01\x1b[1;38;5;34m\x02"
                purple_start = "\x01\x1b[1;38;5;141m\x02"
                dim_start    = "\x01\x1b[2;37m\x02"
                reset        = "\x01\x1b[0m\x02"

                # Tier-aware prompt name color + emoji
                _tier_ansi = {
                    "#8b5a2b": "\x1b[1;38;2;139;90;43m",
                    "#a78bfa": "\x1b[1;38;2;167;139;250m",
                    "#7ec8a0": "\x1b[1;38;2;126;200;160m",
                    "#ffd700": "\x1b[1;38;2;255;215;0m",
                }
                _tier_prompt_emojis = {
                    "Cub":         "🐾",
                    "Bear":        "🐻",
                    "Kodiak":      "⚡🐻",
                    "Spirit Bear": "🌟🐻",
                }
                _cur_tier_name = self.cci.tier_name()
                _cur_tier_color = self.cci.tier_color()
                _tier_ansi_code = _tier_ansi.get(_cur_tier_color, "\x1b[1;38;2;139;90;43m")
                name_start = f"\x01{_tier_ansi_code}\x02"
                prompt_emoji = _tier_prompt_emojis.get(_cur_tier_name, "🐾")

                persona_badge = ""
                if self.active_persona and self.active_persona in self._PERSONAS:
                    badge_name = self._PERSONAS[self.active_persona]["label"].split()[0].lower()
                    persona_badge = f" {purple_start}[{badge_name}]{reset}"

                prompt_str = (
                    f"{name_start}koda {prompt_emoji}{reset}"
                    f"{persona_badge}"
                    f" {dim_start}({gold_start}{model_short}{reset}{dim_start}){reset}"
                    f" {green_start}❯{reset} "
                )

                # Smart input: '/' opens the live palette immediately; other keys fall to readline.
                user_input = await self._read_line_smart(prompt_str)
                if not user_input:
                    continue

                # Process commands
                if user_input.startswith("/"):
                    processed = await self.process_slash_command(user_input)
                    if not processed:
                        console.print(f"[bold #ffd700]⚠️ Koda doesn't know that path: '{user_input}'. Type `/help` for Koda's forest map.[/]\n")
                    continue
                
                # Echo user message into scrollback so the conversation is readable
                ts_echo = datetime.datetime.now().strftime("%H:%M")
                console.print(
                    f"\n  [bold #a78bfa]you[/]  [dim #555555]{ts_echo}[/]\n"
                    f"  [dim white]{user_input}[/]"
                )

                # Run conversation turn
                await self.run_chat_turn(user_input)
                
            except (KeyboardInterrupt, EOFError):
                console.print("\n\n[bold #7ec8a0]🐾 Cozy den shutdown! Koda is curled up for hibernation...[/]")
                if self.sync_task is not None:
                    self.sync_task.cancel()
                    try:
                        await self.sync_task
                    except asyncio.CancelledError:
                        pass
                if self.telegram_task is not None:
                    self.telegram_task.cancel()
                    try:
                        await self.telegram_task
                    except asyncio.CancelledError:
                        pass
                if self._heartbeat_runner is not None:
                    self._heartbeat_runner.stop()
                if self._sleep_runner is not None:
                    self._sleep_runner.stop()
                if self._cron_runner is not None:
                    self._cron_runner.stop()
                await self.finalize_session("keyboard_shutdown")
                sys.exit(0)

def main():
    session = KodaTUISession()
    try:
        asyncio.run(session.start_loop())
    except Exception as e:
        console.print(f"[bold red]CLI layout runtime error:[/] {e}")

if __name__ == "__main__":
    main()
