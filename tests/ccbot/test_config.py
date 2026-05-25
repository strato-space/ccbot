"""Unit tests for Config — env var loading, validation, and user access."""

from pathlib import Path

import pytest

from ccbot.config import Config


@pytest.fixture
def _base_env(monkeypatch, tmp_path):
    # chdir to tmp_path so load_dotenv won't find the real .env in repo root
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    monkeypatch.setenv("ALLOWED_USERS", "12345")
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))


@pytest.mark.usefixtures("_base_env")
class TestConfigValid:
    def test_valid_config(self):
        cfg = Config()
        assert cfg.telegram_bot_token == "test:token"
        assert cfg.allowed_users == {12345}

    def test_custom_tmux_session_name(self, monkeypatch):
        monkeypatch.setenv("TMUX_SESSION_NAME", "mysession")
        cfg = Config()
        assert cfg.tmux_session_name == "mysession"

    def test_custom_monitor_poll_interval(self, monkeypatch):
        monkeypatch.setenv("MONITOR_POLL_INTERVAL", "5.0")
        cfg = Config()
        assert cfg.monitor_poll_interval == 5.0

    def test_is_user_allowed_true(self):
        cfg = Config()
        assert cfg.is_user_allowed(12345) is True

    def test_is_user_allowed_false(self):
        cfg = Config()
        assert cfg.is_user_allowed(99999) is False

    def test_telegram_draft_preview_defaults_to_off(self):
        cfg = Config()
        assert cfg.telegram_draft_preview_mode == "off"
        assert cfg.telegram_draft_preview_allowed_surfaces == set()
        assert cfg.telegram_draft_preview_min_interval_seconds == 1.5

    def test_telegram_draft_preview_env(self, monkeypatch):
        monkeypatch.setenv("CCBOT_TELEGRAM_DRAFT_PREVIEW", "probe")
        monkeypatch.setenv("CCBOT_TELEGRAM_DRAFT_ALLOWED_SURFACES", "t:-100:42,c:123")
        monkeypatch.setenv("CCBOT_TELEGRAM_DRAFT_MIN_INTERVAL_SECONDS", "0.25")
        cfg = Config()
        assert cfg.telegram_draft_preview_mode == "probe"
        assert cfg.telegram_draft_preview_allowed_surfaces == {"t:-100:42", "c:123"}
        assert cfg.telegram_draft_preview_min_interval_seconds == 0.25


@pytest.mark.usefixtures("_base_env")
class TestConfigMissingEnv:
    def test_missing_telegram_bot_token(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            Config()

    def test_missing_allowed_users(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_USERS", raising=False)
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            Config()

    def test_non_numeric_allowed_users(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_USERS", "abc")
        with pytest.raises(ValueError, match="non-numeric"):
            Config()


@pytest.mark.usefixtures("_base_env")
class TestConfigClaudeProjectsPath:
    def test_default_claude_projects_path(self, monkeypatch):
        """Default path is ~/.claude/projects when no env vars are set."""
        # Ensure no custom path env vars are set
        monkeypatch.delenv("CCBOT_CLAUDE_PROJECTS_PATH", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        cfg = Config()
        assert cfg.claude_projects_path == Path.home() / ".claude" / "projects"

    def test_custom_claude_projects_path(self, monkeypatch):
        """CCBOT_CLAUDE_PROJECTS_PATH overrides the default path."""
        custom_path = "/custom/projects/path"
        monkeypatch.setenv("CCBOT_CLAUDE_PROJECTS_PATH", custom_path)
        cfg = Config()
        assert cfg.claude_projects_path == Path(custom_path)

    def test_claude_config_dir_projects_path(self, monkeypatch):
        """CLAUDE_CONFIG_DIR sets path to $CLAUDE_CONFIG_DIR/projects."""
        custom_config_dir = "/custom/claude/config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", custom_config_dir)
        cfg = Config()
        assert cfg.claude_projects_path == Path(custom_config_dir) / "projects"

    def test_ccbot_projects_path_takes_priority(self, monkeypatch):
        """CCBOT_CLAUDE_PROJECTS_PATH takes priority over CLAUDE_CONFIG_DIR."""
        monkeypatch.setenv("CCBOT_CLAUDE_PROJECTS_PATH", "/priority/path")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/lower/priority")
        cfg = Config()
        assert cfg.claude_projects_path == Path("/priority/path")


@pytest.mark.usefixtures("_base_env")
class TestConfigOpenAI:
    def test_openai_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        cfg = Config()
        assert cfg.openai_api_key == ""
        assert cfg.openai_base_url == "https://api.openai.com/v1"

    def test_openai_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
        cfg = Config()
        assert cfg.openai_api_key == "sk-test-123"

    def test_openai_base_url(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example.com/v1")
        cfg = Config()
        assert cfg.openai_base_url == "https://proxy.example.com/v1"

    def test_openai_api_key_scrubbed_from_env(self, monkeypatch):
        import os

        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        Config()
        assert os.environ.get("OPENAI_API_KEY") is None


@pytest.mark.usefixtures("_base_env")
class TestConfigVoiceSTT:
    def test_voice_stt_defaults(self, monkeypatch):
        monkeypatch.delenv("CCBOT_VOICE_STT_PROVIDER", raising=False)
        cfg = Config()
        assert cfg.voice_stt_provider == "openai"
        assert cfg.local_stt_language == "ru"
        assert cfg.local_stt_timeout_seconds == 300
        assert cfg.local_stt_max_concurrency == 1
        assert cfg.local_stt_keep_artifacts is False

    def test_voice_stt_local_settings(self, monkeypatch):
        monkeypatch.setenv("CCBOT_VOICE_STT_PROVIDER", "local_command")
        monkeypatch.setenv(
            "CCBOT_LOCAL_STT_COMMAND", "/bin/stt --input_path {input_path}"
        )
        monkeypatch.setenv("CCBOT_LOCAL_STT_LANGUAGE", "ru")
        monkeypatch.setenv("CCBOT_LOCAL_STT_TIMEOUT_SECONDS", "120")
        monkeypatch.setenv("CCBOT_LOCAL_STT_MAX_CONCURRENCY", "2")
        monkeypatch.setenv("CCBOT_LOCAL_STT_KEEP_ARTIFACTS", "true")
        cfg = Config()
        assert cfg.voice_stt_provider == "local_command"
        assert cfg.local_stt_command.startswith("/bin/stt")
        assert cfg.local_stt_timeout_seconds == 120
        assert cfg.local_stt_max_concurrency == 2
        assert cfg.local_stt_keep_artifacts is True

    def test_voice_stt_invalid_provider_falls_back(self, monkeypatch):
        monkeypatch.setenv("CCBOT_VOICE_STT_PROVIDER", "bogus")
        cfg = Config()
        assert cfg.voice_stt_provider == "openai"

    def test_voice_stt_bounds_numeric_values(self, monkeypatch):
        monkeypatch.setenv("CCBOT_LOCAL_STT_TIMEOUT_SECONDS", "0")
        monkeypatch.setenv("CCBOT_LOCAL_STT_MAX_CONCURRENCY", "999")
        cfg = Config()
        assert cfg.local_stt_timeout_seconds == 1
        assert cfg.local_stt_max_concurrency == 8


def test_resolve_ccbot_command_prefers_ccbot_command_over_legacy() -> None:
    from ccbot.config import resolve_ccbot_command

    assert (
        resolve_ccbot_command(
            {"CCBOT_COMMAND": "omx --madmax", "CLAUDE_COMMAND": "codex"}
        )
        == "omx --madmax"
    )


def test_resolve_ccbot_command_falls_back_to_legacy_and_default() -> None:
    from ccbot.config import resolve_ccbot_command

    assert resolve_ccbot_command({"CLAUDE_COMMAND": "codex"}) == "codex"
    assert resolve_ccbot_command({}) == "claude"
