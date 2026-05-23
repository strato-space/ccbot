# Runtime Capability Registry

`ccbot` now models supported runtimes through a registry instead of hard-coding
Claude-era assumptions into launch, resume, and input routing code.

The registry preserves the operating rule that tmux is the live human control surface
and `stdin/stdout` must not be repurposed as machine transport.

## Required fields per runtime

- launch command name
- resume syntax
- tmux rename support
- identity rename mode
- live-stream discovery
- replay-evidence discovery
- progress source
- final-result source
- prompt detection mode
- blocked-input policy
- message-routing support for `queue` and `steer`
- interactive-control support
- safe degraded-mode behavior

## Current profiles

- `Claude Code`
  - launch: `claude`
  - resume: `--resume <id>`
  - replay evidence: transcript JSONL
- `Codex`
  - launch: `codex`
  - resume: `codex resume <id>`
  - replay evidence: rollout JSONL
- `fast-agent`
  - launch: `fast-agent`
  - resume: `--resume <id>`
  - replay evidence: `acp_log.jsonl`

The registry is intentionally capability-shaped. Different runtimes can expose
different launch or resume syntax while still participating in the same
Telegram topic control plane.
