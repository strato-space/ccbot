# Claude Parity Contract

This note captures the Claude-only behavioral contract that must survive the
multi-runtime rewrite.

It is intentionally narrow:

- it describes the Claude delivery baseline that existed in upstream
- it fixes the Telegram-visible categories and order
- it states the interactive prompt gate that suppresses normal delivery
- it stays compatible with the runtime-neutral ontology introduced in the
  main plan

The executable baseline lives in:

- `tests/fixtures/claude/parity_contract.json`
- `tests/fixtures/claude/parity_transcript.jsonl`
- `tests/ccbot/test_claude_parity_contract.py`

## Upstream sources

The contract is derived from upstream Claude behavior in:

- `src/ccbot/transcript_parser.py`
- `src/ccbot/handlers/message_queue.py`
- `src/ccbot/handlers/interactive_ui.py`
- `src/ccbot/handlers/status_polling.py`

## Telegram delivery categories

Claude transcript parsing delivers these content categories into Telegram:

- `text`
- `thinking`
- `tool_use`
- `tool_result`
- `local_command`

The contract is not "all transcript data". It is the specific category set that
the Telegram bridge uses to present Claude output and tool activity to the
topic chat.

## Status-message lifecycle

The status message is a mutable chat artifact, not a separate conversation.
Upstream behavior is:

1. send a status message when the terminal reports a status line
2. edit the same message in place while the status text changes
3. convert the status message into the first content part when actual content
   arrives
4. clear the status message when the turn is complete or no longer relevant

This lifecycle must survive the rewrite because it is the user-visible progress
surface for Claude turns.

## Tool-use / tool-result editing

Claude tool updates are paired by `tool_use_id`.

Baseline behavior:

- `tool_use` is emitted when the assistant starts a tool call
- the bot remembers the message id for that `tool_use`
- `tool_result` edits the earlier `tool_use` message in place when the pair is
  known
- if the original `tool_use` message cannot be found or edited, the bot falls
  back to sending a new message

This in-place edit behavior is part of the Claude UX contract and must stay
intact in the final multi-runtime flow.

## Final-result delivery

The final assistant result is delivered into the bound topic chat as ordinary
content after the status/tool lifecycle has settled.

The ordering contract is:

1. status updates
2. tool_use / tool_result edits
3. final assistant text into the topic chat

The point is not just that the final answer appears. The point is that it
appears in the topic as the last semantic content for the Claude turn.

## Interactive-tool gate

Claude has blocked prompt surfaces that must suppress normal message delivery
until the prompt is resolved.

The baseline blocked prompts are:

- `AskUserQuestion`
- `ExitPlanMode`
- `PermissionPrompt`
- `RestoreCheckpoint`

While a blocked prompt surface is visible:

- normal message delivery is gated
- the bot should surface the prompt instead of treating it as ordinary content
- read-only surfaces must not pretend to be normal chat output

This gate is the Claude-side safety contract that prevents the bridge from
blasting ordinary messages into a prompt that is waiting for a decision.

## Executable contract fixture

`tests/fixtures/claude/parity_contract.json` contains the machine-readable
baseline and prompt samples.

`tests/fixtures/claude/parity_transcript.jsonl` contains a minimal transcript
sample that exercises the Claude delivery categories and the tool-pairing
contract.

`tests/ccbot/test_claude_parity_contract.py` is the executable check that the
current parser and terminal classifier still match this baseline.
