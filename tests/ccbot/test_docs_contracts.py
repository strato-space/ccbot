from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_readme_points_to_strato_ops_runbook() -> None:
    readme = _read("README.md")

    assert "doc/strato-ops-codex.md" in readme
    assert "doc/multi-runtime-regression-matrix.md" in readme
    assert "doc/multi-runtime-rollout.md" in readme
    assert "/home/tools/codex-tools/codex-session-scout" in readme
    assert "runtime conversation identity" in readme
    assert "replay evidence" in readme
    assert "/resume <thread-name|id>" in readme
    assert "manual_bind_required" in readme
    assert "queue" in readme
    assert "steer" in readme
    assert "`queue` mode" in readme
    assert "Raw terminal control" in readme


def test_strato_ops_runbook_captures_cutover_and_rollback_contract() -> None:
    runbook = _read("doc/strato-ops-codex.md")

    assert "CLAUDE_COMMAND" in runbook
    assert "~/.codex" in runbook
    assert "*.v1.bak" in runbook
    assert "/home/tools/codex-tools/codex-session-scout" in runbook
    assert "runtime process -> runtime conversation identity -> replay evidence" in runbook
    assert "`voice`, `task`, and `ACP-module`" in runbook
    assert "voice" in runbook
    assert "raw `/task`" in runbook
    assert "raw `/ACP`" in runbook


def test_multi_runtime_rollout_doc_requires_explicit_staged_enablement() -> None:
    doc = _read("doc/multi-runtime-rollout.md")

    assert "single configured launch lane per bot instance" in doc
    assert "Ring 0: Codex production baseline" in doc
    assert "Ring 1: Claude Code restore canary" in doc
    assert "Ring 2: fast-agent canary" in doc
    assert "changing `CLAUDE_COMMAND` in place on a shared production bot" in doc
    assert "silently reinterpreting existing production topics under a new runtime lane" in doc
    assert "`GO` for a runtime lane" in doc
    assert "`NO GO`" in doc
    assert "Current rollout inventory" in doc
    assert "ccbot.service" in doc
    assert "@ComfyCodexBot" in doc
    assert "ccbot-claude.service" in doc
    assert "ccbot-fast-agent.service" in doc
    assert "do not reuse the Ring 0 production service" in doc
    assert "Minimum cutover checklist" in doc
    assert "Rollback checklist" in doc
    assert "do not reboot the host" in doc


def test_runtime_ontology_note_uses_runtime_neutral_terms() -> None:
    ontology = _read("doc/runtime-ontology.md")

    assert "topic control policy" in ontology
    assert "semantic emitter / supervisor" in ontology
    assert "live semantic stream" in ontology
    assert "persisted replay evidence" in ontology
    assert "queue" in ontology
    assert "steer" in ontology
    assert "literal ACP-protocol-over-stdio" in ontology


def test_runtime_capability_registry_doc_describes_supported_profiles() -> None:
    doc = _read("doc/runtime-capabilities.md")

    assert "tmux is the live human control surface" in doc
    assert "Claude Code" in doc
    assert "Codex" in doc
    assert "fast-agent" in doc
    assert "queue" in doc
    assert "steer" in doc
    assert "safe degraded-mode behavior" in doc


def test_codex_command_semantics_doc_captures_resume_and_rename_contract() -> None:
    doc = _read("doc/codex-command-semantics.md")

    assert "/resume <thread-name|id>" in doc
    assert "codex resume <resolved-thread-id>" in doc
    assert "/rename" in doc
    assert "Naming Precedence" in doc
    assert "Telegram topic title" in doc
    assert "fast-agent" in doc
    assert "unsupported_degraded" in doc
    assert "duplicate thread names" in doc
    assert "tmux is the authoritative operator intervention surface" in doc


def test_russian_readme_matches_codex_fork_positioning() -> None:
    readme_ru = _read("README_RU.md")

    assert "codex" in readme_ru
    assert "doc/strato-ops-codex.md" in readme_ru
    assert "CLAUDE_COMMAND" in readme_ru
    assert "runtime conversation identity" in readme_ru
    assert "replay evidence" in readme_ru


def test_chinese_readme_stays_on_persisted_identity_language() -> None:
    readme_cn = _read("README_CN.md")

    assert "persisted identity" in readme_cn
    assert "tmux" in readme_cn


def test_execution_review_policy_requires_code_and_ontology_review() -> None:
    policy = _read("doc/execution-review-policy.md")

    assert "self-review" in policy
    assert "independent code review" in policy
    assert "ontology re-check" in policy
    assert "core nouns" in policy


def test_topic_policy_migration_doc_captures_nonce_and_stale_callback_rules() -> None:
    doc = _read("doc/topic-policy-migration.md")

    assert "topic_bind_flow_versions" in doc
    assert "topic_bind_flow_nonces" in doc
    assert "Legacy callbacks without credentials are treated as stale." in doc
    assert "explicit `/unbind`" in doc


def test_runtime_event_contract_doc_names_semantic_and_delivery_layers() -> None:
    doc = _read("doc/runtime-event-contract.md")

    assert "semantic_kind" in doc
    assert "delivery_class" in doc
    assert "status_message_eligible" in doc
    assert "ACP-protocol" in doc


def test_telegram_delivery_pipeline_doc_captures_status_and_teardown_rules() -> None:
    doc = _read("doc/telegram-delivery-pipeline.md")

    assert "status artifact" in doc
    assert "`tool_result` may edit the earlier `tool_use` message in place" in doc
    assert "Late delivery must fail closed." in doc
    assert "queue" in doc
    assert "steer" in doc
    assert "Raw terminal control is not part of this equal message layer." in doc


def test_telegram_bot_features_doc_describes_resume_and_manual_bind_policy() -> None:
    doc = _read("doc/telegram-bot-features.md")

    assert "/resume <token>" in doc
    assert "/rename <name>" in doc
    assert "manual_bind_required" in doc
    assert "queue" in doc
    assert "steer" in doc
    assert "workspace `.fast-agent` root" in doc


def test_multi_runtime_regression_matrix_doc_captures_required_gates() -> None:
    doc = _read("doc/multi-runtime-regression-matrix.md")

    assert "Per-runtime launch" in doc
    assert "Per-runtime resume" in doc
    assert "Claude Code explicit `/resume`" in doc
    assert "fast-agent explicit `/resume`" in doc
    assert "`voice`, `task`, `ACP-module`" in doc
    assert "independent code review" in doc
    assert "ontology review" in doc


def test_claude_runtime_adapter_doc_describes_first_class_adapter() -> None:
    doc = _read("doc/claude-runtime-adapter.md")

    assert "first-class runtime adapter" in doc
    assert "SessionStart hook" in doc
    assert "transcript JSONL" in doc
    assert "tmux is the live human control surface" in doc


def test_fast_agent_runtime_adapter_doc_describes_title_only_semantics() -> None:
    doc = _read("doc/fast-agent-runtime-adapter.md")

    assert "fast-agent --resume <session-id>" in doc
    assert "acp_log.jsonl" in doc
    assert "title-only rename semantics" in doc
    assert "session_id` rename is unsupported" in doc


def test_consumer_audit_by_kind_doc_is_source_backed() -> None:
    doc = _read("doc/consumer-audit-by-kind.md")

    assert "T41 Consumer Audit By Kind" in doc
    assert "src/ccbot/monitor_state.py:27-71, 88-183" in doc
    assert "src/ccbot/hook.py:238-299" in doc
    assert "src/ccbot/session.py:310-338, 500-715" in doc
    assert "Documentation witnesses" in doc


def test_multi_runtime_regression_matrix_doc_freezes_verification_surface() -> None:
    doc = _read("doc/multi-runtime-regression-matrix.md")

    assert "Per-runtime launch" in doc
    assert "Per-runtime resume" in doc
    assert "Bind / unbind / topic policy" in doc
    assert "Progress / result delivery" in doc
    assert "History pollution guards" in doc
    assert "Rename behavior" in doc
    assert "Topic rename vs `/rename` precedence" in doc
    assert "Stale callback invalidation" in doc
    assert "Late-event / stale-binding guards" in doc
    assert "Claude parity against upstream" in doc
    assert "queue / steer semantics" in doc
    assert "Raw operator control separation" in doc
    assert "Non-regression: `voice`, `task`, `ACP-module`" in doc
    assert "Review gates" in doc
    assert "tests/ccbot/test_claude_parity_contract.py" in doc
    assert "doc/execution-review-policy.md" in doc
    assert "/home/tools/ccbot-upstream" in doc
