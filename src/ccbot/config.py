"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCBOT_DIR/.env (default ~/.ccbot).
The module-level `config` instance is imported by nearly every other module.

Key class: Config (singleton instantiated as `config`).
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .utils import ccbot_dir
from .state_schema import DEFAULT_RUNTIME_KIND, SCHEMA_VERSION, LEGACY_BACKUP_SUFFIX

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux)
SENSITIVE_ENV_VARS = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TOKEN",
    "ALLOWED_USERS",
    "CCBOT_REBOOT_ADMIN_USERS",
    "OPENAI_API_KEY",
}

VOICE_STT_PROVIDERS = {"openai", "local_command", "auto", "disabled"}


def _bounded_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %.2f", name, value, default)
        return default
    if parsed < minimum:
        logger.warning(
            "%s=%s below minimum %s; using %s", name, parsed, minimum, minimum
        )
        return minimum
    if parsed > maximum:
        logger.warning(
            "%s=%s above maximum %s; using %s", name, parsed, maximum, maximum
        )
        return maximum
    return parsed


def _bounded_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, value, default)
        return default
    if parsed < minimum:
        logger.warning(
            "%s=%d below minimum %d; using %d", name, parsed, minimum, minimum
        )
        return minimum
    if parsed > maximum:
        logger.warning(
            "%s=%d above maximum %d; using %d", name, parsed, maximum, maximum
        )
        return maximum
    return parsed


def _csv_env_set(name: str) -> set[str]:
    value = os.getenv(name, "").strip()
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _csv_env_list(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default).strip()
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]

def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def resolve_ccbot_command(environ: dict[str, str] | None = None) -> str:
    """Resolve the configured runtime launcher command.

    ``CCBOT_COMMAND`` is the runtime-neutral name.  ``CLAUDE_COMMAND`` remains
    a compatibility fallback for existing deployments.
    """
    source = os.environ if environ is None else environ
    return source.get("CCBOT_COMMAND") or source.get("CLAUDE_COMMAND", "claude")


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccbot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccbot")
        self.tmux_main_window_name = "__main__"

        # Optional per-bot Telegram surface ownership guard.  When
        # CCBOT_OWNED_SURFACES is set, shared group updates outside those
        # chat-qualified surface keys are hard-ignored before user-visible
        # side effects (typing, replies, downloads, runtime input).
        self.owned_surface_keys = _csv_env_set("CCBOT_OWNED_SURFACES")
        self.ignored_surface_keys = _csv_env_set("CCBOT_IGNORED_SURFACES")

        reboot_admins = os.getenv("CCBOT_REBOOT_ADMIN_USERS", "").strip()
        try:
            self.reboot_admin_users: set[int] = {
                int(uid.strip()) for uid in reboot_admins.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"CCBOT_REBOOT_ADMIN_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e
        self.reboot_process_patterns: list[str] = _csv_env_list(
            "CCBOT_REBOOT_PROCESS_PATTERNS",
            "omx,oh-my-codex",
        )
        self.reboot_systemd_units: list[str] = _csv_env_list(
            "CCBOT_REBOOT_SYSTEMD_UNITS",
            "ccbot.service,hermes-gateway.service,hermes-gateway-comfy.service,"
            "hermes-gateway-hercules.service,hermes-gateway-imm.service,"
            "imm_arena_bot.service",
        )
        self.reboot_schedule_delay_seconds = _bounded_float_env(
            "CCBOT_REBOOT_SCHEDULE_DELAY_SECONDS",
            1.5,
            minimum=0.0,
            maximum=60.0,
        )

        draft_preview_mode = os.getenv("CCBOT_TELEGRAM_DRAFT_PREVIEW", "off").strip().lower()
        if draft_preview_mode not in {"off", "probe", "on"}:
            logger.warning(
                "Unknown CCBOT_TELEGRAM_DRAFT_PREVIEW=%s, falling back to off",
                draft_preview_mode,
            )
            draft_preview_mode = "off"
        self.telegram_draft_preview_mode = draft_preview_mode
        self.telegram_draft_preview_allowed_surfaces = _csv_env_set(
            "CCBOT_TELEGRAM_DRAFT_ALLOWED_SURFACES"
        )
        self.telegram_draft_preview_min_interval_seconds = _bounded_float_env(
            "CCBOT_TELEGRAM_DRAFT_MIN_INTERVAL_SECONDS",
            1.5,
            minimum=0.1,
            maximum=30.0,
        )
        self.telegram_draft_preview_retry_cooldown_seconds = _bounded_int_env(
            "CCBOT_TELEGRAM_DRAFT_RETRY_COOLDOWN_SECONDS",
            30,
            minimum=1,
            maximum=3600,
        )
        self.telegram_draft_preview_timeout_cooldown_seconds = _bounded_int_env(
            "CCBOT_TELEGRAM_DRAFT_TIMEOUT_COOLDOWN_SECONDS",
            10,
            minimum=1,
            maximum=3600,
        )

        # Runtime command to run in new windows.  Keep the legacy
        # claude_command attribute as a compatibility alias while call sites
        # migrate to ccbot_command.
        self.ccbot_command = resolve_ccbot_command()
        self.claude_command = self.ccbot_command

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"
        self.telegram_delivery_audit_file = self.config_dir / "telegram_delivery_audit.jsonl"
        self.state_schema_version = SCHEMA_VERSION
        self.state_backup_suffix = LEGACY_BACKUP_SUFFIX
        self.default_runtime_kind = DEFAULT_RUNTIME_KIND

        # Claude Code session monitoring configuration
        # Support custom projects path for Claude variants (e.g., cc-mirror, zai)
        # Priority: CCBOT_CLAUDE_PROJECTS_PATH > CLAUDE_CONFIG_DIR/projects > default
        custom_projects_path = os.getenv("CCBOT_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")

        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = (
            os.getenv("CCBOT_SHOW_USER_MESSAGES", "true").lower() != "false"
        )

        # Show tool call notifications (tool_use/tool_result) in Telegram
        # When False, only text responses, thinking, and interactive prompts are sent
        self.show_tool_calls = (
            os.getenv("CCBOT_SHOW_TOOL_CALLS", "true").lower() != "false"
        )

        # Telegram delivery policy:
        # - compact: human-facing chat output only (default)
        # - verbose: expose more raw execution surface for debugging
        delivery_mode = os.getenv("CCBOT_TELEGRAM_DELIVERY_MODE", "compact").lower()
        if delivery_mode not in {"compact", "verbose"}:
            logger.warning(
                "Unknown CCBOT_TELEGRAM_DELIVERY_MODE=%s, falling back to compact",
                delivery_mode,
            )
            delivery_mode = "compact"
        self.telegram_delivery_mode = delivery_mode

        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("CCBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # OpenAI API for voice message transcription (optional)
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
        voice_stt_provider = (
            os.getenv("CCBOT_VOICE_STT_PROVIDER", "openai").strip().lower()
        )
        if voice_stt_provider not in VOICE_STT_PROVIDERS:
            logger.warning(
                "Unknown CCBOT_VOICE_STT_PROVIDER=%s, falling back to openai",
                voice_stt_provider,
            )
            voice_stt_provider = "openai"
        self.voice_stt_provider = voice_stt_provider
        self.local_stt_command = os.getenv("CCBOT_LOCAL_STT_COMMAND", "").strip()
        self.local_stt_model = os.getenv(
            "CCBOT_LOCAL_STT_MODEL",
            "antony66/whisper-large-v3-russian",
        ).strip()
        self.local_stt_language = (
            os.getenv("CCBOT_LOCAL_STT_LANGUAGE", "ru").strip() or "ru"
        )
        self.local_stt_timeout_seconds = _bounded_int_env(
            "CCBOT_LOCAL_STT_TIMEOUT_SECONDS",
            300,
            minimum=1,
            maximum=3600,
        )
        self.local_stt_max_concurrency = _bounded_int_env(
            "CCBOT_LOCAL_STT_MAX_CONCURRENCY",
            1,
            minimum=1,
            maximum=8,
        )
        self.local_stt_keep_artifacts = _bool_env("CCBOT_LOCAL_STT_KEEP_ARTIFACTS")

        # Scrub sensitive vars from os.environ so child processes never inherit them.
        # Values are already captured in Config attributes above.
        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.claude_projects_path,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users

    def __setattr__(self, name: str, value: object) -> None:
        """Keep legacy claude_command and ccbot_command aliases in sync."""
        object.__setattr__(self, name, value)
        if name == "ccbot_command":
            object.__setattr__(self, "claude_command", value)
        elif name == "claude_command":
            object.__setattr__(self, "ccbot_command", value)


config = Config()
