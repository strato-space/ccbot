# Topic Policy Migration And Callback Invalidation

This note closes `T20.1` for the multi-runtime topic-control plan.

## Purpose

The runtime-neutral topic model introduced two persisted axes:

- `topic_policy`
- `binding_state`

That alone was not sufficient to invalidate stale picker callbacks after:

- restart
- explicit `/unbind`
- picker cancel
- a superseding bind flow

To make stale UI fail closed, the persisted topic state now also tracks:

- `topic_bind_flow_versions`
- `topic_bind_flow_nonces`

## Migration Rules

Backward-compatible reads remain enabled for older state files that only carry:

- `thread_bindings`
- `topic_policies`
- `topic_binding_states`

On load:

- missing `topic_bind_flow_versions` defaults to `0`
- missing `topic_bind_flow_nonces` defaults to `""`
- any persisted topic still in `binding_state=bind_flow` is upgraded with a
  fresh version and nonce

Forward writes always persist the new fields.

## Bind-Flow Credentials

Every active bind flow is identified by:

- `version`
- `nonce`

Those credentials are embedded into picker callback payloads and validated on
every bind-flow callback. Legacy callbacks without credentials are treated as stale.

This gives deterministic invalidation across:

- restart
- old inline keyboards
- explicit `/unbind`
- cancel paths
- bind flow replacement

## Rotation Points

Credentials rotate when:

- a bind flow starts
- a topic is explicitly unbound
- manual-bind mode is forced after cancel
- a successful bind completes

The old callback payloads then stop matching the persisted topic credentials
and are rejected with a stale-bind-flow message.

## Non-Goals

This migration does not change the equal-message routing model and does not
turn raw terminal actions into callback-routed messages.
