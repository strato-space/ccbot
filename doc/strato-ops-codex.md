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
- `CCBOT_COMMAND`
  - Runtime-neutral launcher command for new windows
  - Set this to `codex`, `omx --madmax`, or to a host-specific Codex launcher string
- `CLAUDE_COMMAND`
  - Legacy variable name retained as a fallback when `CCBOT_COMMAND` is unset
- `MONITOR_POLL_INTERVAL`
  - Default: `2.0`

Recommended `~/.ccbot/.env` shape:

```ini
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=123456789
TMUX_SESSION_NAME=ccbot
CCBOT_COMMAND=codex
```

If your host requires an explicit Codex wrapper, keep it in `CCBOT_COMMAND`. Example patterns:

- `CCBOT_COMMAND=codex`
- `CCBOT_COMMAND=omx --madmax`
- `CCBOT_COMMAND=/home/tools/bin/codex-wrapper`

Prefer explicit non-interactive Codex policy in that command when your host policy requires it. Do not make the release depend on a prompt that only exists in an attached terminal.

## tmux Policy

- One Telegram topic binds to one live tmux window at a time.
- The tmux session is the live control surface; do not bypass it with sidecar SDK sessions.
- Manual operator work should happen inside the same tmux session when possible.
- Reboot is forbidden by default. Restart only the scoped bot process or tmux window if needed.
- Startup restore must inventory before action: service process, controller
  env, tmux session/window/panes, work runtime process, runtime conversation
  identity, replay evidence, Telegram control-surface identity, and Telegram
  routing coordinates are distinct.  `CCBOT_RESTORE_*` declares intent only.
- For Codex restore, set `CODEX_HOME` in the controller service env so replay
  ACK/catalog lookup uses the intended root, and set `OMX_AUTO_UPDATE=0` so an
  OMX update prompt cannot block non-interactive startup.
- Autonomous controller restarts on `str` are scoped to exactly two
  bot-controller/tmux surfaces:
  - ComfyCodexBot: `ccbot.service`, `CCBOT_DIR=/data/iqdoctor/.ccbot`,
    tmux `comfy:comfy-agent`, user/surface `3045664/t:555`, chat
    `-1003685295814`, runtime cwd `/home/tools/server/comfy`,
    `CODEX_HOME=/data/iqdoctor/.codex`.
  - ImmArenaBot: `imm_arena_bot.service`,
    `CCBOT_DIR=/data/iqdoctor/.ccbot-imm_arena_bot`, tmux
    `imm_arena_bot:imm`, user/surface `3045664/t:3`, chat
    `-1003974721114`, runtime cwd `/home/tools/imm`,
    `CODEX_HOME=/home/tools/imm/.codex`.
- Both controller services now have the tmux-preserving user-systemd drop-in
  `tmux-preserve.conf` with `KillMode=process`.  Treat that as a blast
  radius mitigation, not as proof: before and after an approved controller
  restart, record the tmux server PID and `tmux list-sessions` output.
- Do not blindly restart `imm_arena_bot.service` or kill tmux to self-heal:
  on `str`, a shared tmux server has been observed under that service cgroup.
  Ambiguous live layers fail closed for manual inspection.  Non-target tmux
  sessions/windows/panes must not be restarted or killed as part of this
  recovery path.
- OMX HUD/helper panes are not bindable work-runtime panes.  The HUD is allowed
  only as a small bottom pane in the parent window; it is operator telemetry,
  not a restored binding target.
- Validate restored writability with `ccbot runtime-input` and same-runtime
  replay-evidence ACK, not with `ccbot send` and not with ad-hoc tmux paste/key
  commands.
- The service startup restore path does not inject its own smoke message; it
  binds after `LiveRuntimeProof`, and the explicit live-ops gate proves
  `ccbot runtime-input` ACK for ComfyCodexBot and ImmArenaBot.

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

Use this only if both `list_chats` calls still fail. Normal recovery is a
scoped systemd restart of the existing MCP proxy units:

```bash
systemctl status mcp@tg-ro.service mcp@tg.service --no-pager -l
journalctl -u mcp@tg-ro.service -u mcp@tg.service -n 120 --no-pager

systemctl restart mcp@tg-ro.service mcp@tg.service
sleep 5
systemctl status mcp@tg-ro.service mcp@tg.service --no-pager -l
ss -ltnp | grep -E ':20(3|6)\b' || true
```

Expected state:

- `mcp@tg-ro.service` is `active (running)` with
  `/usr/bin/mcp-proxy --host=127.0.0.1 --port=203`
- `mcp@tg.service` is `active (running)` with
  `/usr/bin/mcp-proxy --host=127.0.0.1 --port=206`
- `ss` shows listeners on `127.0.0.1:203` and `127.0.0.1:206`
- journal output includes Telethon connection messages and
  `starting server on port 203` / `starting server on port 206`

The proxy command is configured by `/etc/systemd/system/mcp@.service` and the
per-contour env files under `/home/tools/server/mcp/`:

```ini
# /home/tools/server/mcp/tg-ro.env
PORT=203
SHELL_CMD=uv --directory /home/tools/telegram-mcp-ro run main.py

# /home/tools/server/mcp/tg.env
PORT=206
SHELL_CMD=uv --directory /home/tools/telegram-mcp run main.py
```

Important: keep `SHELL_CMD` as one command string. Passing `--shell uv
--directory ... run main.py` as split shell arguments can fail with
`Failed to spawn: main.py`.

### Final proof after recovery

After the restart, prove the actual MCP tool path, not just process liveness:

```text
mcp__tg_ro__.list_chats({"limit": 5})
mcp__tg__.list_chats({"limit": 5})
```

Both must return chat lists. If either still returns `502` or `CHAT-ERR-*`, read
the systemd logs first:

```bash
journalctl -u mcp@tg-ro.service -u mcp@tg.service -n 200 --no-pager
```

### Emergency fallback only: clean up unsupervised manual proxies

Use this only when a previous manual recovery left unsupervised `setsid`/shell
processes occupying ports `203` or `206` and blocking the systemd units. Stop
only the explicit stale process tree, then restart via `systemctl` again. Avoid
broad `pkill -f ...` one-liners from an interactive recovery shell; they can
match the recovery command itself and abort the restart halfway.

```bash
ps -eo pid,ppid,stat,cmd --sort=pid \
  | grep -E 'telegram-mcp-ro|telegram-mcp run main.py|mcp-proxy --host=127\.0\.0\.1 --port=20[36]' \
  | grep -v grep || true

ss -ltnp | grep -E ':20(3|6)\b' || true

# Kill only confirmed stale manual roots, then let systemd own the restart.
kill <stale-mcp-proxy-pid>
systemctl restart mcp@tg-ro.service mcp@tg.service
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
