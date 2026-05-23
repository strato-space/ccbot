# CCBot

[English README](README.md)
[中文文档](README_CN.md)

Удалённое управление `codex`-процессами в `tmux` через Telegram. Этот fork
ориентирован на runtime-neutral core lane: создать окно, привязать topic,
читать replay evidence, отправлять ввод, смотреть историю и возобновлять
persisted identity.

https://github.com/user-attachments/assets/15ffb38e-5eb9-4720-93b9-412e4961dc93

## Что это такое

CCBot не поднимает отдельную SDK-сессию. Он работает поверх живого терминала:

- Telegram topic управляет одним `tmux`-окном
- в окне работает живой процесс `codex`
- история и уведомления читаются из replay evidence в `~/.codex/session_index.jsonl` и `~/.codex/sessions/...`

Ключевая модель:

`Telegram topic -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`

Важно:

- окно не равно runtime conversation identity
- живой процесс не равен persisted identity
- replay evidence не равен процессу; это только файловый след

Подробный операторский путь:

- [`doc/runtime-ontology.md`](doc/runtime-ontology.md)
- [`doc/state-migration.md`](doc/state-migration.md)
- [`doc/strato-ops-codex.md`](doc/strato-ops-codex.md)

## Возможности

- topic-based control: один topic привязывается к одному live tmux window
- real-time notifications: ответы ассистента, reasoning, tool use/result, вывод локальных команд
- prompt-safe input lane: бот различает `input_ready`, `busy`, `blocked_prompt`
- voice: голосовые сообщения транскрибируются и отправляются как текст
- raw Codex slash passthrough: часть `/command` можно слать напрямую в живой `codex`
- identity picker: можно возобновить существующую Codex identity в выбранной директории
- persistent state: bindings и monitor offsets переживают перезапуск

## Границы релиза

Этот релиз расширяет только Codex core lane.

Не входят в scope новой функциональности:

- `voice`
- `task`
- `ACP-module`

Эти поверхности должны оставаться совместимыми, но не считаются частью нового
операторского контракта.

## Требования

- `tmux`
- `codex`
- `uv`
- Telegram bot с включённым Threaded Mode
- доступ к `~/.codex`

Быстрые проверки:

```bash
command -v tmux
command -v codex
command -v uv
ls ~/.codex
```

## Установка

### Вариант 1: из GitHub

```bash
uv tool install git+https://github.com/strato-space/ccbot.git
```

или

```bash
pipx install git+https://github.com/strato-space/ccbot.git
```

### Вариант 2: из исходников

```bash
git clone https://github.com/strato-space/ccbot.git
cd ccbot
uv sync
```

## Конфигурация

Создайте `~/.ccbot/.env`:

```ini
TELEGRAM_BOT_TOKEN=...
ALLOWED_USERS=123456789
TMUX_SESSION_NAME=ccbot
CLAUDE_COMMAND=codex
```

Обязательные:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USERS`

Ключевые optional:

- `CCBOT_DIR`
  - по умолчанию `~/.ccbot`
- `TMUX_SESSION_NAME`
  - по умолчанию `ccbot`
- `CLAUDE_COMMAND`
  - legacy-имя переменной сохранено для совместимости
  - сюда надо ставить `codex` или ваш явный wrapper для Codex
- `MONITOR_POLL_INTERVAL`
  - по умолчанию `2.0`
- `OPENAI_API_KEY`
  - для voice transcription

Важно: имя переменной `CLAUDE_COMMAND` историческое. В Codex fork это именно
команда запуска `codex`, а не Claude.

## Запуск

```bash
cd /home/tools/ccbot
uv run ccbot
```

## Команды

Команды самого бота:

- `/start`
- `/history`
- `/screenshot`
- `/esc`
- `/unbind`

Команды Codex, которые бот честно рекламирует в Telegram menu:

- `/clear`
- `/compact`
- `/diff`
- `/init`
- `/review`
- `/status`

`/usage` сохранён только как legacy helper для Claude-runtime. В Codex окнах
бот не делает вид, что такая команда поддерживается, а направляет оператора к
`/status`.

## Workflow

1. Создайте новый topic в Telegram-группе.
2. Отправьте любое сообщение.
3. Бот покажет browser директорий.
4. Если в директории есть existing Codex identities, появится identity picker.
5. После выбора создаётся live tmux window и запускается `codex`.
6. Следующие сообщения в этом topic уходят в тот же live process.

## Данные на диске

- `~/.ccbot/state.json`
- `~/.ccbot/session_map.json`
- `~/.ccbot/monitor_state.json`
- `~/.codex/session_index.jsonl`
- `~/.codex/sessions/`

При первом старте поверх legacy state бот делает one-time migration и кладёт
backup sidecars как `*.v1.bak`.

## Cutover и rollback

Полный операторский runbook:

- [`doc/strato-ops-codex.md`](doc/strato-ops-codex.md)

Там описано:

- как запускать Codex через legacy env var `CLAUDE_COMMAND`
- как проходит миграция `state.json`, `session_map.json`, `monitor_state.json`
- как пользоваться `/home/tools/codex-tools/codex-session-scout`
- как делать rollback через `*.v1.bak`
- почему нельзя полагаться на remote approval prompts как на обязательный путь

## tmux policy

- один topic = один live tmux binding
- терминал остаётся основным write-path
- reboot по умолчанию запрещён; перезапускайте только процесс бота или нужное окно

Полезные команды:

```bash
tmux attach -t ccbot
tmux list-windows -t ccbot
/home/tools/ccbot/scripts/restart.sh
```
