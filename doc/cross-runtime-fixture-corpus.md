# Cross-Runtime Fixture Corpus

This note documents the redacted fixture corpus used by the multi-runtime
adaptation work.

The goal is to keep the runtime ontology testable without requiring live
sessions. The fixtures are intentionally small and reproducible. They preserve
shape, field names, and control-flow distinctions, but redact sensitive text
and long command output.

## Corpus layout

The corpus lives under:

- `tests/fixtures/cross_runtime/claude`
- `tests/fixtures/cross_runtime/codex`
- `tests/fixtures/cross_runtime/fast-agent`

Each runtime manifest contains the same three fixture families:

- `live_semantic_stream`
- `persisted_replay_evidence`
- `terminal_surface_observation`

## Runtime intent

- Claude Code uses transcript-style live semantic events plus prompt-state
  observations from the terminal surface.
- Codex reuses the already curated rollout/session-index/pane evidence from the
  earlier fixture corpus.
- fast-agent uses ACP-shaped live progress events, session/history replay
  evidence, and prompt-visible terminal observations.

## Why the split matters

The tests need to prove these distinctions remain separate:

- live semantic stream is not the same thing as persisted replay evidence
- terminal-surface observation is not the same thing as persisted history
- resume metadata is not the same thing as a live process

The fixture corpus therefore includes:

- fresh-launch metadata
- resume cases
- progress/status streams
- tool-progress and tool-result transitions
- degraded or failure cases for deterministic tests
- blocked-input and prompt-visible terminal observations that must not be
  promoted to history

## Validation surface

`tests/ccbot/test_cross_runtime_fixture_corpus.py` reads the manifests, checks
that the file graph is complete, and asserts that each runtime corpus can be
consumed as structured data without touching any live session state.
