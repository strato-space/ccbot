from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_readme_points_to_strato_ops_runbook() -> None:
    readme = _read("README.md")

    assert "doc/strato-ops-codex.md" in readme
    assert "/home/tools/codex-tools/codex-session-scout" in readme
    assert "runtime conversation identity" in readme
    assert "replay evidence" in readme


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
