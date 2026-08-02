# Changelog

## 0.2.0 — 2026-08-02

Reports now reach every orchestrator, not just the root session:

- Nested spawns (subagents spawning subagents): a stopping agent whose
  sidecar names a `parentAgentId` reports to that spawner, injected into
  the spawner's own context. Unknown parents degrade to root delivery
  and are never owner-matched.
- Workflow agents: reports route to the root session labeled with the
  workflow run id.
- Guard: lookups are owner-scoped (sender's own children only); the
  confirmation ask stays root-only — inside a subagent the guard warns
  but never blocks.
- Fixes from review: delivery no longer depends on the ledger write
  succeeding; the agent id shown in UI messages is sanitized; the
  SubagentStop hook timeout now fits the worst-case flush wait (15s).
- Hook payloads and sidecars captured from live probes are pinned in
  `tests/fixtures/`, with resolver, routing, and prune tests over them.

## 0.1.0 — 2026-08-01

Initial release: observer (SubagentStop measurement → state + queue),
drain (PostToolUse injection into the orchestrator), guard (PreToolUse
on SendMessage: warns above `warn_tokens`, asks for confirmation above
`block_tokens`), status CLI, plugin packaging plus manual
install/uninstall scripts, unit tests, design doc.
Supersedes the private v1 relay-based hook (see docs/DESIGN.md for why
the relay was dropped).
