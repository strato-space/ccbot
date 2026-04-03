# fast-agent Runtime Adapter

fast-agent is integrated as a tmux-first runtime adapter.

The adapter keeps three concerns separate:

- `tmux` is the live human control surface
- persisted `session_id` is the resumable conversation identity
- replay evidence comes from `acp_log.jsonl` when present, otherwise from the
  latest rotating history file

## What is supported

- launch via `fast-agent`
- resume via `fast-agent --resume <session-id>`
- persisted session discovery from `.fast-agent/sessions`
- optional title metadata surfaced as the user-facing summary
- title-only rename semantics

## What is intentionally degraded

- direct persisted `session_id` rename is unsupported
- literal ACP-protocol transport over the runtime stdio is not used as the
  primary operator model

## Why tmux stays first

`ccbot` keeps tmux as the authoritative operator intervention surface.
Human observability and direct terminal control outrank protocol purity, so the
adapter consumes ACP-equivalent replay evidence without surrendering the live
CLI stdio.

## Verification surface

- `tests/ccbot/test_fast_agent_sessions.py`
- `tests/ccbot/test_runtime_registry.py`
