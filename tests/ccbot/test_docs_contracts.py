from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_readme_points_to_strato_ops_runbook() -> None:
    readme = _read("README.md")

    assert "ontology/README.md" in readme
    assert "ontology/runtime.md" in readme
    assert "ontology/topic-control.md" in readme
    assert "ontology/delivery-surface.md" in readme
    assert "ontology/boundaries.md" in readme
    assert "specs/README.md" in readme
    assert "specs/ccbot-codex-adaptation-plan-4.md" in readme
    assert "doc/strato-ops-codex.md" in readme
    assert "doc/runtime-event-contract.md" in readme
    assert "doc/telegram-delivery-pipeline.md" in readme
    assert "doc/multi-runtime-regression-matrix.md" in readme
    assert "doc/multi-runtime-rollout.md" in readme
    assert "/home/tools/codex-tools/codex-session-scout" in readme
    assert "runtime conversation identity" in readme
    assert "replay evidence" in readme
    assert "binding(binding_scope=external)" in readme
    assert "external persisted Codex thread in read-only replay mode" in readme
    assert "/resume <thread-name|id>" in readme
    assert "/bind <thread-name|id>" in readme
    assert "/exit" in readme
    assert "/quit" in readme
    assert "explicitly rejected in favor of `/exit`" in readme
    assert "manual_bind_required" in readme
    assert "control surface" in readme
    assert "no-topics group main chat" in readme
    assert "ordinary text and `@bot` mentions stay silent until a command is used" in readme
    assert "Command entry paths also capture the Telegram group `chat_id`" in readme
    assert "`chat_id` as a Telegram routing\ncoordinate" in readme
    assert "CODEX_HOME=/data/iqdoctor/.codex" in readme
    assert "OMX_AUTO_UPDATE=0" in readme
    assert "`CCBOT_RESTORE_*` remains restore\nintent, not proof" in readme
    assert "LiveRuntimeProof" in readme
    assert "ResumeTargetProof" in readme
    assert "OMX HUD/question/update/helper panes" in readme
    assert "do not use\n`ccbot send` or copied `tmux paste-buffer`" in readme
    assert "does not inject a smoke message automatically" in readme
    assert "bind-time gate stops at `LiveRuntimeProof`" in readme
    assert "ComfyCodexBot" in readme
    assert "`ccbot.service`" in readme
    assert "`comfy` / `comfy-agent`" in readme
    assert "ImmArenaBot" in readme
    assert "`imm_arena_bot.service`" in readme
    assert "`imm_arena_bot` / `imm`" in readme
    assert "Telegram identity/routing" in readme
    assert "Both controller services now carry" in readme
    assert "`tmux-preserve.conf` with\n`KillMode=process`" in readme
    assert "tmux server PID and `tmux\nlist-sessions` output" in readme
    assert "Non-target tmux sessions/windows/panes must not be\nrestarted or killed" in readme
    assert "HUD\nshould remain a small bottom pane" in readme
    assert "must never be chosen\nas the restored Telegram binding target" in readme
    assert "Command handlers persist group routing metadata" in readme
    assert "shared group topics" in readme
    assert "queue" in readme
    assert "steer" in readme
    assert "`queue` mode" in readme
    assert "Raw terminal control" in readme
    assert "persisted JSONL turn event" in readme
    assert "replay-evidence ACK" in readme
    assert "Compact Telegram delivery" in readme
    assert "User echo" in readme
    assert "Commentary" in readme
    assert "Final assistant responses" in readme
    assert "may span multiple Telegram messages" in readme
    assert "fresh last message" in readme
    assert "Technical execution classes stay out of permanent bubbles by default" in readme
    assert "Queued follow-up preview" in readme
    assert "OMX interactive questions" in readme
    assert "omx.question/v1" in readme
    assert "`--state-path`" in readme
    assert "same-window OMX question renderer pane" in readme
    assert "active or recoverable" in readme
    assert "visibility-first" in readme
    assert "Ordinary user echo remains visible" in readme
    assert "queue is empty" in readme
    assert "the binding goes stale, or an explicit clear path runs" in readme
    assert "whole pre-final visible surface closes until the next user turn" in readme
    assert "mutable technical status surface" in readme
    assert "no pre-final visible artifact" in readme
    assert "late technical status" in readme
    assert "lifecycle `turn_started`" in readme
    assert "Generated-image terminal media result" in readme
    assert "terminal Telegram photo bubble with a caption" in readme
    assert "saved-path text remains the" in readme
    assert "Runtime image preview artifact" in readme
    assert "Codex `image_generation_end`" in readme
    assert "`view_image` / `Viewed Image`" in readme
    assert "latest-only pre-final mutable Telegram photo bubble" in readme
    assert "later same-turn\n  previews edit that media in place" in readme
    assert "uses the first image and audits the truncation" in readme
    assert "authorized replay-proven" in readme
    assert "does not close the assistant turn" in readme
    assert "photo, document, sticker, audio, or video media arrives before the topic" in readme
    assert "CCBOT_MAX_TELEGRAM_DOWNLOAD_BYTES" in readme
    assert "too large for Telegram bot" in readme
    assert "download” warning" in readme
    assert "ccbot --help" in readme
    assert "CCBOT_TELEGRAM_GET_UPDATES_POOL_SIZE" in readme
    assert "CCBOT_TELEGRAM_GET_UPDATES_POOL_TIMEOUT" in readme
    assert "CCBOT_TELEGRAM_POLL_HEALTH_ENABLED" in readme
    assert "CCBOT_TELEGRAM_POLL_HEALTH_FAILURE_THRESHOLD" in readme
    assert "event=telegram_polling_health_timeout_stalled" in readme
    assert "token/proxy credentials are redacted" in readme
    assert "pending_update_count" in readme
    assert "Verbose/debug paths may expose more raw execution surface" in readme


def test_strato_ops_runbook_captures_cutover_and_rollback_contract() -> None:
    runbook = _read("doc/strato-ops-codex.md")

    assert "CLAUDE_COMMAND" in runbook
    assert "~/.codex" in runbook
    assert "*.v1.bak" in runbook
    assert "/home/tools/codex-tools/codex-session-scout" in runbook
    assert "runtime process -> runtime conversation identity -> replay evidence" in runbook
    assert "`voice`, `task`, and `ACP-module`" in runbook
    assert "voice" in runbook
    assert "raw `/task`" in runbook
    assert "raw `/ACP`" in runbook
    assert "Startup restore must inventory before action" in runbook
    assert "`CCBOT_RESTORE_*` declares intent only" in runbook
    assert "CODEX_HOME" in runbook
    assert "OMX_AUTO_UPDATE=0" in runbook
    assert "Do not blindly restart `imm_arena_bot.service`" in runbook
    assert "ComfyCodexBot: `ccbot.service`" in runbook
    assert "tmux `comfy:comfy-agent`" in runbook
    assert "ImmArenaBot: `imm_arena_bot.service`" in runbook
    assert "tmux\n    `imm_arena_bot:imm`" in runbook
    assert "Both controller services now have" in runbook
    assert "`tmux-preserve.conf` with `KillMode=process`" in runbook
    assert "tmux server PID and `tmux list-sessions` output" in runbook
    assert "Non-target tmux\n  sessions/windows/panes must not be restarted or killed" in runbook
    assert "HUD is allowed\n  only as a small bottom pane" in runbook
    assert "ccbot runtime-input" in runbook
    assert "replay-evidence ACK" in runbook
    assert "does not inject its own smoke message" in runbook
    assert "binds after `LiveRuntimeProof`" in runbook


def test_multi_runtime_rollout_doc_requires_explicit_staged_enablement() -> None:
    doc = _read("doc/multi-runtime-rollout.md")

    assert "single configured launch lane per bot instance" in doc
    assert "Ring 0: Codex production baseline" in doc
    assert "Ring 1: Claude Code restore canary" in doc
    assert "Ring 2: fast-agent canary" in doc
    assert "changing `CLAUDE_COMMAND` in place on a shared production bot" in doc
    assert "silently reinterpreting existing production topics under a new runtime lane" in doc
    assert "`GO` for a runtime lane" in doc
    assert "`NO GO`" in doc
    assert "Current rollout inventory" in doc
    assert "ccbot.service" in doc
    assert "@ComfyCodexBot" in doc
    assert "ccbot-claude.service" in doc
    assert "ccbot-fast-agent.service" in doc
    assert "do not reuse the Ring 0 production service" in doc
    assert "Minimum cutover checklist" in doc
    assert "Rollback checklist" in doc
    assert "do not reboot the host" in doc


def test_runtime_ontology_note_uses_runtime_neutral_terms() -> None:
    ontology = _read("doc/runtime-ontology.md")

    assert "ontology/README.md" in ontology
    assert "ontology/runtime.md" in ontology
    assert "ontology/topic-control.md" in ontology
    assert "ontology/delivery-surface.md" in ontology
    assert "ontology/boundaries.md" in ontology
    assert "derived maintainer note" in ontology
    assert "control-surface policy" in ontology
    assert "semantic emitter / supervisor" in ontology
    assert "live semantic stream" in ontology
    assert "persisted replay evidence" in ontology
    assert "binding_scope=external" in ontology
    assert "Input injection plane" in ontology
    assert "Input acknowledgement" in ontology
    assert "pane reaction is diagnostic only" in ontology
    assert "read-only rather than pretending to send into tmux" in ontology
    assert "control surface" in ontology
    assert "surface_key=t:<thread_id>" in ontology
    assert "queue" in ontology
    assert "steer" in ontology
    assert "literal ACP-protocol-over-stdio" in ontology


def test_ontology_folder_collects_project_core_nouns() -> None:
    index = _read("ontology/README.md")
    runtime = _read("ontology/runtime.md")
    topic_control = _read("ontology/topic-control.md")
    delivery = _read("ontology/delivery-surface.md")
    boundaries = _read("ontology/boundaries.md")

    assert "compact source of truth" in index
    assert "ontology/runtime.md" in index
    assert "ontology/topic-control.md" in index
    assert "ontology/delivery-surface.md" in index
    assert "ontology/boundaries.md" in index
    assert "External replay-only variant" in index
    assert "control surface -> control-surface identity" in index
    assert "control-surface identity -> (user_id, surface_key)" in index
    assert "surface_key -> local product key, not a global identity" in index
    assert "whitelisted autonomous recovery target" in index
    assert "Current `str` recovery target boundary" in index
    assert "ComfyCodexBot: `ccbot.service`" in index
    assert "ImmArenaBot: `imm_arena_bot.service`" in index
    assert "`tmux-preserve.conf` with\n`KillMode=process`" in index
    assert "must not restart or kill non-target tmux sessions/windows/panes" in index
    assert "semantic emitter / supervisor" in runtime
    assert "runtime conversation identity" in runtime
    assert "persisted replay evidence" in runtime
    assert "Bot-controller service process" in runtime
    assert "Autonomous recovery target" in runtime
    assert "ComfyCodexBot: `ccbot.service`" in runtime
    assert "ImmArenaBot: `imm_arena_bot.service`" in runtime
    assert "tmux-preserving controller restart" in runtime
    assert "current `str` fact: both whitelisted controller services now carry this" in runtime
    assert "tmux server PID and `tmux list-sessions` must be checked" in runtime
    assert "OMX HUD pane" in runtime
    assert "never a\n    restored Telegram binding target" in runtime
    assert "binding_scope=external" in runtime
    assert "Telegram control surface" in runtime
    assert "Surface key" in runtime
    assert "Control-surface identity" in runtime
    assert "(user_id, surface_key)" in runtime
    assert "Telegram group routing coordinates" in runtime
    assert "command-only entry paths such as `/bind` and `/resume`" in runtime
    assert "Control-surface policy" in runtime
    assert "surface-scoped maps are canonical" in runtime
    assert "Topic And Surface Control Ontology" in topic_control
    assert "Surface key" in topic_control
    assert "full persisted identity" in topic_control
    assert "equal `thread_id` values in different groups are not the same control surface" in topic_control
    assert "Telegram group routing coordinates" in topic_control
    assert "group_chat_ids[user_id:thread_id] -> Telegram group chat_id" in topic_control
    assert "command-only entry must not depend on prior text, mention, or callback input" in topic_control
    assert "photo/document/sticker/audio/video ingress is not an addressed entry" in topic_control
    assert "HUD/helper pane" in topic_control
    assert "not itself a\n    control surface, delivery source, runtime conversation identity" in topic_control
    assert "must never be selected as the restored binding target" in topic_control
    assert "pending slot" in topic_control
    assert "no-topics main-chat control surface" in topic_control
    assert "legacy `topic_*` maps are compatibility mirrors" in topic_control
    assert "terminal turn artifact" in delivery
    assert "pre-final visible artifact" in delivery
    assert "technical status artifact" in delivery
    assert "Command-like tool output belongs to command execution" in delivery
    assert "Genuine non-command tool results remain" in delivery
    assert "Pending input artifact" in delivery
    assert "Interactive question artifact" in delivery
    assert "omx.question/v1" in delivery
    assert "`--state-path`" in delivery
    assert "same-window OMX question" in delivery
    assert "same-window renderer pane that is still alive" in delivery
    assert "active or recoverable" in delivery
    assert "Warning artifact" in delivery
    assert "control surface" in delivery
    assert "usage-limit / quota-exhaustion notices" in delivery
    assert "serialized into multiple Telegram" in delivery
    assert "fresh message sequence" in delivery
    assert "before a new user turn advances" in delivery
    assert "repeat counter only when `N > 2`" in delivery
    assert "visibility-first mutable" in delivery
    assert "must drop rather than reopen the lane" in delivery
    assert "terminal media result artifacts" in delivery
    assert "generated-image preview/photo bubbles" in delivery
    assert "Runtime image preview artifact" in delivery
    assert "latest-only mutable Telegram media artifact" in delivery
    assert "edit the media\n    in place instead of stacking additional preview bubbles" in delivery
    assert "visible\n  pre-final progress media, not durable terminal/history bubbles" in delivery
    assert "first paired replay-embedded image" in delivery
    assert "paired replay-embedded image bytes" in delivery
    assert "local path" in delivery and "not authorization to read files" in delivery
    assert "Inbound media artifact path" in delivery
    assert "Telegram Bot API download guardrails pass" in delivery
    assert "Audio artifact: /path" in delivery
    assert "Video artifact: /path" in delivery
    assert "Telegram photo bubble with a" in delivery
    assert "terminal fallback" in delivery
    assert "queue-empty, binding-stale, or explicit clear" in delivery
    assert "lifecycle `turn_started` may reopen the lanes idempotently" in delivery
    assert "ACP-protocol" in boundaries
    assert "ACP-module" in boundaries
    assert "control surface == topic" in boundaries
    assert "surface policy == binding state" in boundaries
    assert "surface key == full control-surface identity" in boundaries
    assert "Inbound Telegram audio/video handling is a third boundary" in boundaries
    assert "Bot API download guardrails pass" in boundaries
    assert "Replay evidence is written by" in boundaries


def test_spec_corpus_is_subordinate_to_ontology_vocabulary() -> None:
    specs_index = _read("specs/README.md")
    plan2 = _read("specs/ccbot-codex-adaptation-plan-2.md")
    plan4 = _read("specs/ccbot-codex-adaptation-plan-4.md")

    assert "Plan files in this directory are execution/history artifacts" in specs_index
    assert "the ontology folder wins" in specs_index
    assert "control-surface vocabulary" in specs_index
    assert "Telegram control surface -> binding -> tmux window" in plan2
    assert "Vocabulary note" in plan2
    assert "Control-surface policy" in plan2
    assert "`bind_flow`" in plan2
    assert "`binding_in_progress`" not in plan2
    assert "Telegram topic -> binding -> tmux window" not in plan2
    assert "per-control-surface turn generations" in plan4
    assert "warning on the same control surface" in plan4
    assert "T77: OMX Durable Question Delivery" in plan4
    assert "omx.question/v1" in plan4


def test_runtime_capability_registry_doc_describes_supported_profiles() -> None:
    doc = _read("doc/runtime-capabilities.md")

    assert "tmux is the live human control surface" in doc
    assert "Claude Code" in doc
    assert "Codex" in doc
    assert "fast-agent" in doc
    assert "queue" in doc
    assert "steer" in doc
    assert "safe degraded-mode behavior" in doc


def test_codex_command_semantics_doc_captures_resume_and_rename_contract() -> None:
    doc = _read("doc/codex-command-semantics.md")

    assert "/resume <thread-name|id>" in doc
    assert "codex resume <resolved-thread-id>" in doc
    assert "/rename" in doc
    assert "Naming Precedence" in doc
    assert "Telegram topic title" in doc
    assert "fast-agent" in doc
    assert "unsupported_degraded" in doc
    assert "duplicate thread names" in doc
    assert "Non-interactive helper sessions such as `originator=codex_exec`" in doc
    assert "raw thread id is only a last-resort label" in doc
    assert "tmux is the authoritative operator intervention surface" in doc
    assert "Codex Conversational Submit ACK" in doc
    assert "turn-acceptance" in doc


def test_russian_readme_matches_codex_fork_positioning() -> None:
    readme_ru = _read("README_RU.md")

    assert "codex" in readme_ru
    assert "doc/strato-ops-codex.md" in readme_ru
    assert "CLAUDE_COMMAND" in readme_ru
    assert "runtime conversation identity" in readme_ru
    assert "replay evidence" in readme_ru


def test_chinese_readme_stays_on_persisted_identity_language() -> None:
    readme_cn = _read("README_CN.md")

    assert "persisted identity" in readme_cn
    assert "tmux" in readme_cn


def test_execution_review_policy_requires_code_and_ontology_review() -> None:
    policy = _read("doc/execution-review-policy.md")

    assert "self-review" in policy
    assert "independent code review" in policy
    assert "ontology re-check" in policy
    assert "core nouns" in policy


def test_topic_policy_migration_doc_captures_nonce_and_stale_callback_rules() -> None:
    doc = _read("doc/topic-policy-migration.md")

    assert "topic_bind_flow_versions" in doc
    assert "topic_bind_flow_nonces" in doc
    assert "Legacy callbacks without credentials are treated as stale." in doc
    assert "explicit `/unbind`" in doc


def test_runtime_event_contract_doc_names_semantic_and_delivery_layers() -> None:
    doc = _read("doc/runtime-event-contract.md")

    assert "per-control-surface ordering generation" in doc
    assert "ontology/delivery-surface.md" in doc
    assert "semantic_kind" in doc
    assert "delivery_class" in doc
    assert "status_message_eligible" in doc
    assert "ACP-protocol" in doc
    assert "semantic eligibility in the runtime-neutral contract" in doc
    assert "product projection onto the Telegram delivery surface" in doc
    assert "orchestration" in doc
    assert "history-worthy semantic facts" in doc
    assert "product surface chooses to" in doc
    assert "collapse them into compact status delivery" in doc
    assert "terminal turn artifact" in doc
    assert "pre-final visible artifact" in doc
    assert "technical status artifact" in doc
    assert "pending input artifact" in doc
    assert "warning" in doc
    assert "usage-limit / quota-exhaustion notices are warning artifacts too" in doc
    assert "latest-warning dedup semantics" in doc
    assert "user turn opener" in doc
    assert "ordinary user-visible user echo remains eligible for compact Telegram" in doc
    assert "turn generation" in doc
    assert "must be suppressed or dropped if they would otherwise appear below" in doc
    assert "canonical `response_item.message` wins over duplicate lightweight `event_msg`" in doc
    assert "buffer lightweight `event_msg` copies" in doc
    assert "later idle poll" in doc
    assert "event_msg.user_message" in doc
    assert "reopening the turn a second time" in doc
    assert "replay delivery capability" in doc
    assert "input injection capability" in doc
    assert "explicit read-only" in doc
    assert "queue-owned lifecycle changes" in doc
    assert "visibility-first mutable updates" in doc


def test_telegram_delivery_pipeline_doc_captures_status_and_teardown_rules() -> None:
    doc = _read("doc/telegram-delivery-pipeline.md")

    assert "ontology/topic-control.md" in doc
    assert "ontology/delivery-surface.md" in doc
    assert "ontology/boundaries.md" in doc
    assert "status artifact" in doc
    assert "The default Telegram surface is `compact`, not `verbose`." in doc
    assert "latest human-facing commentary remains visible as a dedicated artifact" in doc
    assert "pending-input artifact" in doc
    assert "human-facing orchestration milestones stay as ordinary content" in doc
    assert "warning artifacts stay visible as durable system notices" in doc
    assert "usage-limit / quota-exhaustion banners are warning artifacts" in doc
    assert "repeat counter only when `N > 2`" in doc
    assert "control surface" in doc
    assert "ordinary user echo remains visible in compact mode" in doc
    assert "reasoning and thinking summaries are routed through the mutable status" in doc
    assert "artifact" in doc
    assert "including Claude-style `local_command`" in doc
    assert "placeholder reasoning such as `[reasoning]` is suppressed" in doc
    assert "raw tool payloads, giant command stdout dumps, and full file bodies must be summarized before they reach Telegram" in doc
    assert "fenced `sh` blocks" in doc
    assert "fenced `json` blocks" in doc
    assert "truncation footers outside the fenced block" in doc
    assert "outcome metadata rendered as a separate footer" in doc
    assert "visibility-first mutable updates" in doc
    assert "`tool_use` summaries" in doc
    assert "`tool_result` summaries" in doc
    assert "when tool lifecycle is materialized as content, `tool_result` may edit the earlier `tool_use` message in place" in doc
    assert "generated-image success output with a saved artifact" in doc
    assert "terminal media result artifact" in doc
    assert "terminal saved-path text" in doc
    assert "Codex `image_generation_end`" in doc
    assert "`view_image` / `Viewed Image`" in doc
    assert "pre-final runtime image preview artifacts" in doc
    assert "latest-only mutable Telegram media artifact" in doc
    assert "later same-turn previews edit that\nmedia in place" in doc
    assert "uses the first image only when a preview payload contains multiple images" in doc
    assert "never read local path arguments" in doc
    assert "pre-final visible surface" in doc
    assert "Telegram/history prefers the canonical copy." in doc
    assert "lightweight copy may be buffered briefly" in doc
    assert "idle poll rather than on an unrelated non-idle poll" in doc
    assert "whole pre-final visible" in doc
    assert "mutable technical status" in doc
    assert "turn generation" in doc
    assert "hidden internal prompt scaffold" in doc
    assert "lifecycle `turn_started` is allowed to reopen turn generation" in doc
    assert "orchestration milestone, plan update, or surfaced preview bubble" in doc
    assert "durable Telegram content bubbles are" in doc
    assert "deliberately narrow" in doc
    assert "latest-only visible commentary artifact" in doc
    assert "Commentary is not clipped by the internal status-helper ceiling" in doc
    assert "fresh Telegram message sequence" in doc
    assert "latest-only pending-input artifact" in doc
    assert "queue-owned lifecycle changes" in doc
    assert "drop it instead of reopening the closed pre-final lane" in doc
    assert "must not add a second preview footer" in doc
    assert "spawned/waiting/completed subagent summaries" in doc
    assert "External-thread bind follows the same split" in doc
    assert "read-only warning and reattach hint" in doc
    assert "Late delivery must fail closed." in doc
    assert "queue" in doc
    assert "steer" in doc
    assert "Raw terminal control is not part of this equal message layer." in doc


def test_telegram_bot_features_doc_describes_resume_and_manual_bind_policy() -> None:
    doc = _read("doc/telegram-bot-features.md")

    assert "/bind <thread-name|id>" in doc
    assert "/resume <token>" in doc
    assert "| `/exit` |" in doc
    assert "| `/quit` |" not in doc
    assert "`/quit` is runtime-rejected in favor of `/exit`" in doc
    assert "/rename <name>" in doc
    assert "manual_bind_required" in doc
    assert "queue" in doc
    assert "steer" in doc
    assert "workspace `.fast-agent` root" in doc
    assert "**Compact delivery policy**" in doc
    assert "Queued follow-up preview" in doc
    assert "user echo, orchestration milestones, and final assistant text as durable bubbles" in doc
    assert "visibility wins when silence would make runtime state ambiguous" in doc
    assert "latest commentary stays visible as a dedicated artifact" in doc
    assert "multiple Telegram messages while remaining" in doc
    assert "Ordinary user echo stays visible in compact mode" in doc
    assert "Warning artifacts remain durable and visible" in doc
    assert "Usage-limit / quota-exhaustion notices are warning artifacts too" in doc
    assert "`×N` counter when `N > 2`" in doc
    assert "This section is a derived summary of" in doc
    assert "ontology/topic-control.md" in doc
    assert "ontology files remain the master source" in doc
    assert "surface_policy" in doc
    assert "(user_id, surface_key)" in doc
    assert "no-topics group chat" in doc or "no-topics group" in doc
    assert "bot-addressed `@mention` messages in an unbound topic must stay silent" in doc
    assert "unbound photo, document, sticker, audio, and video messages must also" in doc
    assert "do not download media" in doc
    assert "Identical numeric `thread_id` values in different groups are different control surfaces" in doc
    assert "explicit `/bind` and explicit `/resume` remain valid explicit entry paths" in doc
    assert "group routing metadata" in doc
    assert "the group `chat_id`" in doc
    assert "**Pre-final terminal surface barrier**" in doc
    assert "Codex-style command/tool previews" in doc
    assert "Polling liveness guard" in doc
    assert "service-alive-but-polling-dead" in doc
    assert "OMX interactive question artifacts" in doc
    assert "whole pre-final visible surface is" in doc
    assert "mutable technical status artifact are both closed" in doc
    assert "In external read-only bind mode" in doc
    assert "read-only warning and a hint to reattach writable live control" in doc
    assert "Draft answer artifact" in doc
    assert "fresh Telegram message sequence" in doc
    assert "Expandable blockquote for debug reasoning" in doc


def test_multi_runtime_regression_matrix_doc_captures_required_gates() -> None:
    doc = _read("doc/multi-runtime-regression-matrix.md")

    assert "Per-runtime launch" in doc
    assert "Per-runtime resume" in doc
    assert "Claude Code explicit `/resume`" in doc
    assert "fast-agent explicit `/resume`" in doc
    assert "`voice`, `task`, `ACP-module`" in doc
    assert "independent code review" in doc
    assert "ontology review" in doc


def test_claude_runtime_adapter_doc_describes_first_class_adapter() -> None:
    doc = _read("doc/claude-runtime-adapter.md")

    assert "first-class runtime adapter" in doc
    assert "SessionStart hook" in doc
    assert "transcript JSONL" in doc
    assert "tmux is the live human control surface" in doc


def test_fast_agent_runtime_adapter_doc_describes_title_only_semantics() -> None:
    doc = _read("doc/fast-agent-runtime-adapter.md")

    assert "fast-agent --resume <session-id>" in doc
    assert "acp_log.jsonl" in doc
    assert "title-only rename semantics" in doc
    assert "session_id` rename is unsupported" in doc


def test_consumer_audit_by_kind_doc_is_source_backed() -> None:
    doc = _read("doc/consumer-audit-by-kind.md")

    assert "T41 Consumer Audit By Kind" in doc
    assert "src/ccbot/monitor_state.py:27-71, 88-183" in doc
    assert "src/ccbot/hook.py:238-299" in doc
    assert "src/ccbot/session.py:310-338, 500-715" in doc
    assert "Documentation witnesses" in doc


def test_monitor_state_schema_strategy_doc_freezes_compatibility_envelope() -> None:
    doc = _read("doc/monitor-state-schema-strategy.md")

    assert "compatibility envelope" in doc
    assert "`monitor_state.json` keeps legacy tracked-session keys" in doc
    assert "`session_id` and `file_path` remain the persisted transport fields" in doc
    assert "`schema v2` is not selected in this tranche" in doc
    assert "T43 is closed as not selected" in doc


def test_state_migration_doc_matches_monitor_state_schema_strategy() -> None:
    doc = _read("doc/state-migration.md")

    assert "`monitor_state.json` keeps a versioned top-level envelope" in doc
    assert "`tracked_sessions` entries remain on the compatibility envelope" in doc
    assert "`session_id` and `file_path` stay on disk" in doc
    assert "`thread_id` / `replay_path` are API aliases, not persisted schema keys" in doc


def test_runtime_ontology_doc_assigns_replay_writes_to_runtime_or_emitter() -> None:
    doc = _read("doc/runtime-ontology.md")

    assert "semantic emitter / supervisor -> persisted replay evidence" in doc
    assert "runtime conversation identity scopes/indexes the live semantic stream and persisted replay evidence" in doc


def test_multi_runtime_regression_matrix_doc_freezes_verification_surface() -> None:
    doc = _read("doc/multi-runtime-regression-matrix.md")

    assert "Per-runtime launch" in doc
    assert "Per-runtime resume" in doc
    assert "Bind / unbind / topic policy" in doc
    assert "Progress / result delivery" in doc
    assert "History pollution guards" in doc
    assert "Rename behavior" in doc
    assert "Topic rename vs `/rename` precedence" in doc
    assert "Stale callback invalidation" in doc
    assert "Late-event / stale-binding guards" in doc
    assert "Claude parity against upstream" in doc
    assert "queue / steer semantics" in doc
    assert "Raw operator control separation" in doc
    assert "Non-regression: `voice`, `task`, `ACP-module`" in doc
    assert "Review gates" in doc
    assert "tests/ccbot/test_claude_parity_contract.py" in doc
    assert "doc/execution-review-policy.md" in doc
    assert "/home/tools/ccbot-upstream" in doc
