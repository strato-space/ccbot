import json
import os
import subprocess
import sys
from types import SimpleNamespace

from ccbot import binding_preflight_cli as cli
from ccbot.runtime_types import LiveProcessDescriptor
from ccbot.session import SessionManager


def _manager_without_io(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


def _bind(
    manager: SessionManager,
    *,
    user_id: int = 3045664,
    surface_key: str = "t:555",
    window_id: str = "@1",
    window_name: str = "comfy-agent",
    cwd: str = "/home/tools/mediagen-comfy",
    runtime_kind: str = "codex",
) -> None:
    manager.bind_surface(
        user_id,
        window_id,
        surface_key=surface_key,
        window_name=window_name,
    )
    manager.window_states[window_id] = LiveProcessDescriptor(
        cwd=cwd,
        window_name=window_name,
        runtime_kind=runtime_kind,
    )


def _run(manager: SessionManager, argv: list[str]) -> cli.RuntimeBindingPreflightResult:
    args = cli._build_parser().parse_args(argv)
    return cli.run_binding_preflight(manager, args)


def test_binding_preflight_help_documents_read_only_semantics():
    help_text = cli._build_parser().format_help()

    assert "usage: ccbot binding-preflight " in help_text
    assert "Read-only ccbot binding/workspace preflight" in help_text
    assert "never calls send_to_window" in help_text
    assert "--expected-cwd" in help_text


def test_binding_preflight_malformed_args_do_not_require_telegram_config(tmp_path):
    env = os.environ.copy()
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env["CCBOT_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = "src"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ccbot.binding_preflight_cli",
            "--user-id",
            "not-int",
            "--json",
        ],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "--user-id must be an integer" in result.stderr
    assert "TELEGRAM_BOT_TOKEN" not in result.stderr


def test_binding_preflight_canonical_comfy_target_passes(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(manager)
    manager.send_to_window = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("preflight must not inject runtime input")
    )

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:555",
            "--expected-user-id",
            "3045664",
            "--expected-surface-key",
            "t:555",
            "--expected-window-name",
            "comfy-agent",
            "--expected-runtime-kind",
            "codex",
            "--expected-cwd",
            "/home/tools/mediagen-comfy",
            "--json",
        ],
    )

    assert result.ok is True
    assert result.classification == "ok"
    assert result.resolved
    assert result.resolved.window_id == "@1"
    assert result.resolved.cwd == "/home/tools/mediagen-comfy"


def test_binding_preflight_resolves_legacy_topic_with_chat_coordinates(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    manager.set_group_chat_id(3045664, 555, -1003685295814)
    _bind(manager, surface_key="t:555")

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:-1003685295814:555",
            "--expected-user-id",
            "3045664",
            "--expected-surface-key",
            "t:-1003685295814:555",
            "--expected-window-name",
            "comfy-agent",
            "--expected-runtime-kind",
            "codex",
            "--expected-cwd",
            "/home/tools/mediagen-comfy",
        ],
    )

    assert result.ok is True
    assert result.resolved
    assert result.resolved.surface_key == "t:-1003685295814:555"


def test_binding_preflight_deduplicates_canonical_and_legacy_topic_mirrors(
    monkeypatch,
):
    manager = _manager_without_io(monkeypatch)
    manager.set_group_chat_id(3045664, 555, -1003685295814)
    _bind(manager, surface_key="t:555")
    _bind(manager, surface_key="t:-1003685295814:555")

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:-1003685295814:555",
            "--expected-user-id",
            "3045664",
            "--expected-surface-key",
            "t:-1003685295814:555",
            "--expected-cwd",
            "/home/tools/mediagen-comfy",
        ],
    )

    assert result.ok is True
    assert result.classification == "ok"
    assert result.resolved
    assert result.resolved.surface_key == "t:-1003685295814:555"


def test_binding_preflight_rejects_stale_server_comfy_cwd(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(manager, cwd="/home/tools/server/comfy")

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:555",
            "--expected-surface-key",
            "t:555",
            "--expected-cwd",
            "/home/tools/mediagen-comfy",
        ],
    )

    assert result.ok is False
    assert result.classification == "cwd_mismatch"
    payload = json.dumps(result.to_dict())
    assert "/home/tools/server/comfy" in payload
    assert "/home/tools/mediagen-comfy" in payload
    assert "TELEGRAM_BOT_TOKEN" not in payload


def test_binding_preflight_rejects_wrong_surface_even_when_cwd_matches(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(
        manager,
        surface_key="t:8227",
        window_id="@6",
        window_name="comfy-agent-ops",
    )

    result = _run(
        manager,
        [
            "--window-id",
            "@6",
            "--expected-surface-key",
            "t:555",
            "--expected-cwd",
            "/home/tools/mediagen-comfy",
        ],
    )

    assert result.ok is False
    assert result.classification == "surface_mismatch"
    assert result.resolved
    assert result.resolved.surface_key == "t:8227"


def test_binding_preflight_rejects_wrong_window_name(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(
        manager,
        surface_key="t:8227",
        window_id="@6",
        window_name="comfy-agent-ops",
    )

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:8227",
            "--expected-surface-key",
            "t:8227",
            "--expected-window-name",
            "comfy-agent",
        ],
    )

    assert result.ok is False
    assert result.classification == "window_name_mismatch"


def test_binding_preflight_rejects_wrong_runtime_kind(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(manager, runtime_kind="claude")

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:555",
            "--expected-surface-key",
            "t:555",
            "--expected-runtime-kind",
            "codex",
        ],
    )

    assert result.ok is False
    assert result.classification == "runtime_kind_mismatch"


def test_binding_preflight_rejects_inactive_bound_window(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(manager)
    manager.window_states.pop("@1")

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:555",
            "--expected-user-id",
            "3045664",
            "--expected-surface-key",
            "t:555",
        ],
    )

    assert result.ok is False
    assert result.classification == "inactive_binding"
    assert result.resolved
    assert result.resolved.window_id == "@1"


def test_binding_preflight_rejects_placeholder_runtime_metadata(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    manager.bind_surface(
        3045664,
        "@1",
        surface_key="t:555",
        window_name="comfy-agent",
    )

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:555",
            "--expected-user-id",
            "3045664",
            "--expected-surface-key",
            "t:555",
        ],
    )

    assert result.ok is False
    assert result.classification == "missing_runtime_metadata"
    assert result.resolved
    assert result.resolved.window_id == "@1"


def test_binding_preflight_rejects_helper_window(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(manager)
    monkeypatch.setattr(manager, "_is_codex_helper_window", lambda _window_id: True)

    result = _run(
        manager,
        [
            "--user-id",
            "3045664",
            "--surface-key",
            "t:555",
            "--expected-user-id",
            "3045664",
            "--expected-surface-key",
            "t:555",
            "--expected-cwd",
            "/home/tools/mediagen-comfy",
        ],
    )

    assert result.ok is False
    assert result.classification == "helper_binding"
    assert result.resolved
    assert result.resolved.window_id == "@1"


def test_binding_preflight_window_id_cannot_bypass_expected_user(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(manager, user_id=111, surface_key="t:555", window_id="@1")

    result = _run(
        manager,
        [
            "--window-id",
            "@1",
            "--expected-user-id",
            "3045664",
            "--expected-surface-key",
            "t:555",
        ],
    )

    assert result.ok is False
    assert result.classification == "user_mismatch"
    assert result.resolved
    assert result.resolved.user_id == 111


def test_binding_preflight_expected_user_can_come_from_env(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(manager, user_id=111, surface_key="t:555", window_id="@1")
    monkeypatch.setenv("CCBOT_PREFLIGHT_EXPECTED_USER_ID", "3045664")

    result = _run(
        manager,
        [
            "--window-id",
            "@1",
            "--expected-surface-key",
            "t:555",
        ],
    )

    assert result.ok is False
    assert result.classification == "user_mismatch"
    assert result.expected
    assert result.expected.user_id == 3045664


def test_binding_preflight_window_id_with_selectors_requires_matching_binding(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    _bind(
        manager,
        user_id=3045664,
        surface_key="t:8227",
        window_id="@6",
        window_name="comfy-agent-ops",
    )

    result = _run(
        manager,
        [
            "--window-id",
            "@6",
            "--user-id",
            "3045664",
            "--surface-key",
            "t:555",
            "--expected-cwd",
            "/home/tools/mediagen-comfy",
        ],
    )

    assert result.ok is False
    assert result.classification == "no_binding"
    assert result.resolved is None


def test_binding_preflight_main_json_failure_is_nonzero(monkeypatch, capsys):
    fake_session_module = SimpleNamespace(session_manager=_manager_without_io(monkeypatch))

    monkeypatch.setitem(sys.modules, "ccbot.session", fake_session_module)

    exit_code = cli.binding_preflight_main(
        ["--user-id", "3045664", "--surface-key", "t:555", "--json"]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["classification"] == "no_binding"
