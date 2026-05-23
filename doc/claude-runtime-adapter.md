# Claude Runtime Adapter

Claude Code remains a first-class runtime adapter on the new ontology.

The adapter is intentionally tmux-first:

- `tmux is the live human control surface`
- Claude commands are launched in a live pane
- `SessionStart hook` registration is used to resolve the persisted transcript
- transcript JSONL is the live semantic stream and persisted replay evidence

## What is preserved

- launch via `claude`
- resume via `claude --resume <session-id>`
- input routing through the runtime input driver
- prompt gating from terminal-surface observations
- Claude parity for progress, tool lifecycle, and final-result delivery

## What is not collapsed

- persisted transcript identity is not the same thing as the live pane
- terminal-surface observations are not treated as history
- `window=session` style shortcuts are not used

## Verification surface

- `tests/ccbot/test_claude_runtime_adapter.py`
- `tests/ccbot/test_claude_parity_contract.py`
- `tests/ccbot/test_runtime_registry.py`
