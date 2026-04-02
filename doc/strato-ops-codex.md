# Strato Codex Ops Runbook

This runbook is the operator path for the Strato fork of `ccbot`.

Scope:

- Ship the Codex core lane: create, bind, monitor, send input, inspect history, resume.
- Keep `voice`, `task`, and `ACP` behavior stable, but do not expand them in this release.
- Do not rely on interactive approval prompts as part of the required operating path.

## Runtime Model

Use this chain when reasoning about the system:

`Telegram topic -> binding -> tmux window -> Codex process -> Codex thread -> rollout log`

Operational consequences:

- Telegram writes to the live tmux window.
- History and monitor notifications are read from Codex rollout logs under `~/.codex`.
- Resume attaches a live process to an existing thread; it does not resurrect an old process.

## Preflight

Before cutover, verify:

- `tmux` is installed and usable by the bot host user.
- `codex` is installed and callable in the shell used by tmux.
- `uv` is installed for local launch and validation.
- `~/.codex/session_index.jsonl` exists or will be created by the first local Codex run.
- `~/.codex/sessions/` exists or will be created by the first local Codex run.
- Telegram bot token and allowed user IDs are present in `~/.ccbot/.env`.

Useful checks:

```bash
command -v tmux
command -v codex
command -v uv
test -d ~/.codex && ls ~/.codex
```

## Config

CCBot still uses some legacy env var names for compatibility.

Required:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USERS`

Important optional values:

- `CCBOT_DIR`
  - Default: `~/.ccbot`
  - Holds `state.json`, `session_map.json`, and `monitor_state.json`
- `TMUX_SESSION_NAME`
  - Default: `ccbot`
  - All topic-bound windows live under this tmux session
- `CLAUDE_COMMAND`
  - Legacy variable name retained for compatibility
  - Set this to `codex` or to a host-specific Codex launcher string
- `MONITOR_POLL_INTERVAL`
  - Default: `2.0`

Recommended `~/.ccbot/.env` shape:

```ini
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=123456789
TMUX_SESSION_NAME=ccbot
CLAUDE_COMMAND=codex
```

If your host requires an explicit Codex wrapper, keep it in `CLAUDE_COMMAND`. Example patterns:

- `CLAUDE_COMMAND=codex`
- `CLAUDE_COMMAND=/home/tools/bin/codex-wrapper`

Prefer explicit non-interactive Codex policy in that command when your host policy requires it. Do not make the release depend on a prompt that only exists in an attached terminal.

## tmux Policy

- One Telegram topic binds to one live tmux window at a time.
- The tmux session is the live control surface; do not bypass it with sidecar SDK sessions.
- Manual operator work should happen inside the same tmux session when possible.
- Reboot is forbidden by default. Restart only the scoped bot process or tmux window if needed.

Useful commands:

```bash
tmux attach -t ccbot
tmux list-windows -t ccbot
tmux capture-pane -pt ccbot:__main__
```

## First Start And Migration

The Codex cutover uses one-time migration with reversible backups.

Files:

- `~/.ccbot/state.json`
- `~/.ccbot/session_map.json`
- `~/.ccbot/monitor_state.json`

Behavior on first start with legacy files present:

- legacy shape is accepted
- a sidecar backup is written as `*.v1.bak`
- the file is rewritten in the versioned runtime-aware shape

Launch:

```bash
cd /home/tools/ccbot
uv sync --extra dev
uv run ccbot
```

Expected result:

- the bot starts without losing existing topic bindings
- `*.v1.bak` files appear beside any migrated legacy state files
- new Codex windows register live process metadata immediately at launch

## Operator Tooling

For inspecting local Codex state outside Telegram, use:

- `/home/tools/codex-tools/codex-session-scout`

Examples:

```bash
cd /home/tools/codex-tools
uv run ./codex-session-scout list --view ops --active-within 24h
uv run ./codex-session-scout fetch <thread-id>
```

For bot restarts without touching the host, use:

```bash
/home/tools/ccbot/scripts/restart.sh
```

## Rollback

Use rollback only if Codex cutover is broken enough that the bot cannot safely serve Telegram topics.

1. Stop the running bot process.
2. Restore backup sidecars over the migrated files:

```bash
cp ~/.ccbot/state.json.v1.bak ~/.ccbot/state.json
cp ~/.ccbot/session_map.json.v1.bak ~/.ccbot/session_map.json
cp ~/.ccbot/monitor_state.json.v1.bak ~/.ccbot/monitor_state.json
```

3. If the host must return to Claude-era launch behavior, reset:

```bash
export CLAUDE_COMMAND=claude
```

4. Start the bot again and verify topic bindings before resuming normal use.

Rollback notes:

- Restoring the backups reverts only bot-side persisted state, not Codex thread files.
- Do not delete `~/.codex`; it is read-only evidence for the Codex lane.

## Release Boundary

This release does not expand:

- `voice`
- `task`
- `ACP`

Those surfaces are preserved by regression coverage, but they are not part of the new Codex core-lane contract.

## Smoke Checklist

Before declaring the bot ready:

1. Start the bot with the production-like `CLAUDE_COMMAND=codex` configuration.
2. Create a fresh Telegram topic and bind it to a project directory.
3. Start a fresh Codex thread and verify output reaches Telegram.
4. Send a follow-up text message and verify it reaches the same live tmux window.
5. Open `/history` and confirm rollout-backed history rendering.
6. Resume an existing thread from the picker and verify the new live process binds to the persisted thread.
7. Restart the bot process and confirm bindings plus monitor offsets survive.
8. Check that `voice`, raw `/task`, and raw `/ACP` behavior still matches the preserved compatibility surface.
