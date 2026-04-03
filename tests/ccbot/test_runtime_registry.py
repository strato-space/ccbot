from ccbot.runtime_types import runtime_capability_registry


def test_registry_exposes_runtime_specific_capabilities() -> None:
    claude = runtime_capability_registry.get("claude")
    codex = runtime_capability_registry.get("codex")
    fast_agent = runtime_capability_registry.get("fast-agent")

    assert claude.launch_command_name == "claude"
    assert claude.resume_style == "flag"
    assert claude.replay_evidence_discovery == "transcript_jsonl"
    assert claude.supports_message_routing_mode("queue")

    assert codex.launch_command_name == "codex"
    assert codex.resume_style == "subcommand"
    assert codex.replay_evidence_discovery == "rollout_jsonl"
    assert codex.progress_source == "replay_evidence"

    assert fast_agent.launch_command_name == "fast-agent"
    assert fast_agent.resume_style == "flag"
    assert fast_agent.replay_evidence_discovery == "acp_log_jsonl"
    assert fast_agent.rename_identity_mode == "title_only"
    assert runtime_capability_registry.supports_interactive_control("fast-agent")


def test_registry_builds_runtime_specific_resume_commands() -> None:
    assert runtime_capability_registry.build_launch_command(
        "claude", resume_session_id="session-1"
    ) == "claude --resume session-1"
    assert runtime_capability_registry.build_launch_command(
        "codex", resume_session_id="thread-1"
    ) == "codex resume thread-1"
    assert runtime_capability_registry.build_launch_command(
        "fast-agent", resume_session_id="session-9"
    ) == "fast-agent --resume session-9"


def test_registry_infers_runtime_kind_from_command_aliases() -> None:
    assert (
        runtime_capability_registry.infer_runtime_kind_from_command(
            "fast-agent-acp --transport acp"
        )
        == "fast-agent"
    )
