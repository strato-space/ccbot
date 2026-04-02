from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_readme_points_to_strato_ops_runbook() -> None:
    readme = _read("README.md")

    assert "doc/strato-ops-codex.md" in readme
    assert "/home/tools/codex-tools/codex-session-scout" in readme


def test_strato_ops_runbook_captures_cutover_and_rollback_contract() -> None:
    runbook = _read("doc/strato-ops-codex.md")

    assert "CLAUDE_COMMAND" in runbook
    assert "~/.codex" in runbook
    assert "*.v1.bak" in runbook
    assert "/home/tools/codex-tools/codex-session-scout" in runbook
    assert "`voice`, `task`, and `ACP`" in runbook


def test_russian_readme_matches_codex_fork_positioning() -> None:
    readme_ru = _read("README_RU.md")

    assert "codex" in readme_ru
    assert "doc/strato-ops-codex.md" in readme_ru
    assert "CLAUDE_COMMAND" in readme_ru
