# Runtime-Neutral Naming Audit

This audit classifies the remaining Claude/session-shaped names that survived the
ontology cleanup pass. The rule used here is narrow:

- runtime-native adapter terms are not shared-core naming debt
- compatibility surfaces stay until external callers and persisted artifacts are
  fully migrated
- wrapper aliases remain transitional until the underlying call sites move

## Scope Reviewed

- [src/ccbot/config.py](/home/tools/ccbot/src/ccbot/config.py)
- [src/ccbot/session.py](/home/tools/ccbot/src/ccbot/session.py)
- [src/ccbot/runtime_types.py](/home/tools/ccbot/src/ccbot/runtime_types.py)
- [src/ccbot/tmux_manager.py](/home/tools/ccbot/src/ccbot/tmux_manager.py)
- [src/ccbot/hook.py](/home/tools/ccbot/src/ccbot/hook.py)
- [doc/claude-runtime-adapter.md](/home/tools/ccbot/doc/claude-runtime-adapter.md)
- [doc/fast-agent-runtime-adapter.md](/home/tools/ccbot/doc/fast-agent-runtime-adapter.md)
- [doc/runtime-ontology.md](/home/tools/ccbot/doc/runtime-ontology.md)
- [doc/state-migration.md](/home/tools/ccbot/doc/state-migration.md)

## Classifications

| Name | Classification | Reason | Source refs |
| --- | --- | --- | --- |
| `claude_projects_path` | runtime-native adapter term | This names the Claude transcript/project tree that the Claude adapter reads from. It is runtime-specific filesystem layout, not shared-core vocabulary, and the monitor/session code explicitly treats it as adapter-local configuration. | [config.py:76-87](/home/tools/ccbot/src/ccbot/config.py#L76), [session_monitor.py:50-52](/home/tools/ccbot/src/ccbot/session_monitor.py#L50) |
| `ClaudeSession` | transitional alias | This is a direct type alias to `ThreadLocator` for legacy callers. The code already exposes the runtime-neutral type, so this survives only as a compatibility bridge. | [session.py:76](/home/tools/ccbot/src/ccbot/session.py#L76), [session.py:835-839](/home/tools/ccbot/src/ccbot/session.py#L835) |
| `list_sessions_for_directory` | transitional alias | This method is a backward-compatible wrapper over `list_threads_for_directory`, so the legacy name is retained only until callers switch. | [session.py:843-907](/home/tools/ccbot/src/ccbot/session.py#L843) |
| `clear_window_session` | removable naming debt | The behavior is just "clear the persisted thread association for a window". The method is only kept because `bot.py` still calls it; the name itself is no longer required for compatibility. | [session.py:757-762](/home/tools/ccbot/src/ccbot/session.py#L757), [bot.py:1142-1146](/home/tools/ccbot/src/ccbot/bot.py#L1142) |
| `session_map` | required compatibility surface | This is a persisted file and hook contract, not a mere variable name. The file is read, versioned, backed up, and rewritten by multiple paths, so renaming it would break operator workflows and cutover recovery. | [config.py:68-73](/home/tools/ccbot/src/ccbot/config.py#L68), [state_schema.py:100-123](/home/tools/ccbot/src/ccbot/state_schema.py#L100), [hook.py:238-295](/home/tools/ccbot/src/ccbot/hook.py#L238), [session.py:310-338](/home/tools/ccbot/src/ccbot/session.py#L310) |
| `resume_session_id` | runtime-native adapter term | Resume is runtime-specific control-plane vocabulary. The launcher adapter needs a resume token parameter that maps to the runtime's own resumption shape, so this is adapter-local rather than shared-core debt. | [runtime_types.py:269-288](/home/tools/ccbot/src/ccbot/runtime_types.py#L269), [tmux_manager.py:383-413](/home/tools/ccbot/src/ccbot/tmux_manager.py#L383) |

## Notes

- `session_map.json` remains legacy-shaped in filename and workflow, but the
  versioned payload and backup path already make the compatibility boundary
  explicit.
- `resume_session_id` stays because the adapter must pass the runtime's own
  resume token through to the launch command. That is a runtime concern, not a
  shared-core ontology leak.
- `ClaudeSession` and `list_sessions_for_directory` are safe to treat as
  transitional aliases because the neutral counterparts already exist in the
  same module.

## Outcome

No surviving name in this audit was left unclassified. The only name that should
be considered cleanup debt rather than compatibility is `clear_window_session`,
and even that is retained intentionally until the next control-path vocabulary
cleanup task.
