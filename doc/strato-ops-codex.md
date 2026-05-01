# Strato Codex Ops Runbook

This runbook is the operator path for the Strato fork of `ccbot`.

Scope:

- Ship the Codex core lane: create, bind, monitor, send input, inspect history, resume.
- Keep `voice`, `task`, and `ACP-module` behavior stable, but do not expand them in this release.
- Do not rely on interactive approval prompts as part of the required operating path.

## Runtime Model

Use this chain when reasoning about the system:

`Telegram control surface -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`

Operational consequences:

- Telegram writes to the live tmux window.
- History and monitor notifications are read from replay evidence under `~/.codex`.
- Resume attaches a live process to an existing identity; it does not resurrect an old process.

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

## Telegram MCP Recovery

This section covers the local Telegram MCP sidecars used by operator tooling,
not the production `ccbot.service` / `imm_arena_bot.service` Telegram bots.

The two MCP contours are:

- `tg-ro`: read-only Telegram MCP, proxy on `127.0.0.1:203`, repo
  `/home/tools/telegram-mcp-ro`
- `tg`: read/write Telegram MCP, proxy on `127.0.0.1:206`, repo
  `/home/tools/telegram-mcp`

### Symptoms

Treat these as MCP sidecar/cache symptoms:

- `mcp__tg_ro__.list_chats` or `mcp__tg__.list_chats` returns
  `CHAT-ERR-*`
- topic/user helpers return generic `GEN-ERR-*` after a previously working
  session
- contact helpers return `CONTACT-ERR-*`
- Codex tool transport reports an HTML `502 Bad Gateway` from nginx instead of
  JSON/tool output
- `list_topics` / `resolve_username` fails until the Telethon entity cache is
  warmed

Do not diagnose this from a bare `curl http://127.0.0.1:203/` or `:206/`
alone. The proxy may return `503` to a plain GET even when the real MCP stream
path is usable. The proof of recovery is a successful MCP `list_chats` call.

### First recovery attempt: warm both entity caches

Run both list calls before restarting anything:

```text
mcp__tg_ro__.list_chats({"limit": 5})
mcp__tg__.list_chats({"limit": 5})
```

Expected result: both return chat lists. This warms the Telethon entity cache
and often fixes follow-up `get_entity(...)` failures for group/topic helpers.

### Restart both Telegram MCP sidecars

Use this only if both `list_chats` calls still fail.

1. Inspect current processes and ports:

```bash
ps -eo pid,ppid,stat,cmd --sort=pid \
  | grep -E 'telegram-mcp-ro|telegram-mcp run main.py|mcp-proxy --host=127\.0\.0\.1 --port=20[36]' \
  | grep -v grep || true

ss -ltnp | grep -E ':20(3|6)\b' || true
```

2. Stop stale processes by explicit PID, or use a script that avoids killing
   its own shell. Avoid broad `pkill -f ...` one-liners from an interactive
   recovery shell; they can match the recovery command itself and abort the
   restart halfway.

```bash
python3 - <<'PY'
import os
import signal
import time

patterns = [
    'node /usr/bin/mcp-proxy --host=127.0.0.1 --port=203 ',
    'node /usr/bin/mcp-proxy --host=127.0.0.1 --port=206 ',
    '/bin/sh -c uv --directory /home/tools/telegram-mcp-ro run main.py',
    '/bin/sh -c uv --directory /home/tools/telegram-mcp run main.py',
    'uv --directory /home/tools/telegram-mcp-ro run main.py',
    'uv --directory /home/tools/telegram-mcp run main.py',
    '/home/tools/telegram-mcp-ro/.venv/bin/python3 main.py',
    '/home/tools/telegram-mcp/.venv/bin/python3 main.py',
]
self_pid = os.getpid()
targets = []

for name in os.listdir('/proc'):
    if not name.isdigit():
        continue
    pid = int(name)
    if pid == self_pid:
        continue
    try:
        cmdline = (
            open(f'/proc/{pid}/cmdline', 'rb')
            .read()
            .replace(b'\0', b' ')
            .decode('utf-8', 'ignore')
            .strip()
        )
    except OSError:
        continue
    if any(pattern in cmdline for pattern in patterns):
        targets.append(pid)

for pid in targets:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

time.sleep(1)

for pid in targets:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        continue
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
PY
```

3. Restart detached from the recovery shell with `setsid`; otherwise the proxy
   can die when the shell/tool session exits and the client will keep seeing
   `502 Bad Gateway`.

```bash
mkdir -p /tmp/mcp-restart-logs

setsid -f sh -c 'exec /usr/bin/mcp-proxy \
  --host=127.0.0.1 --port=203 \
  --server=stream --streamEndpoint=/ --stateless \
  --shell "uv --directory /home/tools/telegram-mcp-ro run main.py" \
  >/tmp/mcp-restart-logs/telegram-mcp-ro.log 2>&1'

setsid -f sh -c 'exec /usr/bin/mcp-proxy \
  --host=127.0.0.1 --port=206 \
  --server=stream --streamEndpoint=/ --stateless \
  --shell "uv --directory /home/tools/telegram-mcp run main.py" \
  >/tmp/mcp-restart-logs/telegram-mcp.log 2>&1'
```

Important: keep the `--shell` value as one quoted command string. Passing
`--shell uv --directory ... run main.py` as split shell arguments can fail with
`Failed to spawn: main.py`.

4. Verify process and port state:

```bash
ps -eo pid,ppid,stat,cmd --sort=pid \
  | grep -E 'telegram-mcp-ro|telegram-mcp run main.py|mcp-proxy --host=127\.0\.0\.1 --port=20[36]' \
  | grep -v grep || true

ss -ltnp | grep -E ':20(3|6)\b' || true

tail -80 /tmp/mcp-restart-logs/telegram-mcp-ro.log
tail -80 /tmp/mcp-restart-logs/telegram-mcp.log
```

Expected logs include a Telethon connection and:

```text
starting server on port 203
starting server on port 206
```

5. Final proof:

```text
mcp__tg_ro__.list_chats({"limit": 5})
mcp__tg__.list_chats({"limit": 5})
```

Both must return chat lists. If either still returns `502`, re-check that the
proxy process is still parented to PID 1 or another long-lived supervisor, not
to the recovery shell that just exited.

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

- Restoring the backups reverts only bot-side persisted state, not Codex identity files.
- Do not delete `~/.codex`; it is read-only evidence for the Codex lane.

## Release Boundary

This release does not expand:

- `voice`
- `task`
- `ACP-module`

Those surfaces are preserved by regression coverage, but they are not part of the new Codex core-lane contract.

## Smoke Checklist

Before declaring the bot ready:

1. Start the bot with the production-like `CLAUDE_COMMAND=codex` configuration.
2. Create a fresh Telegram topic and bind it to a project directory.
3. Start a fresh Codex identity and verify output reaches Telegram.
4. Send a follow-up text message and verify it reaches the same live tmux window.
5. Open `/history` and confirm rollout-backed history rendering.
6. Resume an existing identity from the picker and verify the new live process binds to the persisted identity.
7. Restart the bot process and confirm bindings plus monitor offsets survive.
8. Check that `voice`, raw `/task`, and raw `/ACP` behavior still matches the preserved compatibility surface.
