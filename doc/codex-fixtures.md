# Codex Fixture Corpus

These fixtures capture real Codex evidence needed for the Codex adaptation work. They are intentionally small, redacted excerpts, not full raw transcripts.

## Provenance

- Rollout evidence came from the operator host's `~/.codex/sessions/**/*.jsonl`.
- Session-index evidence came from `~/.codex/session_index.jsonl`.
- Prompt evidence came from a live pane in `tmux` session `0` using `tmux capture-pane -epJS - -t 0:0.0`.
- Sensitive instruction blocks, data URIs, long command outputs, and encrypted reasoning payloads were trimmed or replaced with `[redacted-...]` placeholders.

## Why this corpus exists

Later tasks need stable evidence for:

- distinguishing fresh threads from resumed/forked threads
- handling same-cwd ambiguity without guessing
- reading rollout logs instead of conflating them with live tmux processes
- surviving stale session-index rows and missing rollout files
- recognizing reasoning, commentary, tool calls, command output, interrupted turns, and prompt-visible terminal states

## Coverage map

- `session_index_rows.json`: real session-index rows plus one synthetic stale row
- `thread_metadata.json`: curated real `session_meta` summaries and same-cwd groups
- `monitor_state_missing_rollout.json`: tracked-session state with a missing non-root-shaped rollout path
- `rollouts/fresh_home_thread.jsonl`: fresh thread on `/home`
- `rollouts/resumed_home_thread.jsonl`: resumed/forked thread on `/home`
- `rollouts/nonroot_reasoning_turn.jsonl`: non-root cwd with reasoning and commentary
- `rollouts/root_tool_call_and_output.jsonl`: root cwd plus `exec_command` call/output
- `rollouts/interrupted_turn_nonroot.jsonl`: interrupted turn on `/home/strato-space`
- `panes/tmux_session_0_resume_prompt.json`: live tmux prompt snapshot for `codex resume`

## Redaction rules

- Preserve field names and payload shapes.
- Preserve actual cwd/path forms when they matter for implementation:
  - `/root`
  - `/home`
  - `/home/strato-space`
  - synthetic missing path under `/home/service-user/.codex/...`
- Do not preserve secrets, giant instruction bodies, or full command output dumps.
- Keep at least one realistic `function_call_output` chunk so later parsing code can see the command-output envelope.

## Gotchas

- The live tmux pane for `codex resume` was attached to session `0`, but the current pane path was `/home` while the visible resumed Codex directory was `/home/strato-space`. That mismatch is intentional evidence; later code must not assume pane path and thread cwd always coincide.
- The prompt snapshot includes failure states (`refresh token was already used`, `usage limit`) together with an input-ready prompt. This is useful for fail-closed prompt classification work.
- The stale index entry is synthetic by design. Real stale rows are hard to guarantee reproducibly; the fixture explicitly documents this.
