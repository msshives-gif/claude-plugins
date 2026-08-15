# Changelog

## 0.5.0 — 2026-08-15

- **BEHAVIOR CHANGE — the block tier now denies once instead of
  asking.** New knob `block_style` (default `deny_once`): past
  `block_tokens`, or on a compacted target with `compaction_action:
  block`, the send is DENIED with a reason the model sees; an
  immediate retry passes, and the challenge re-arms after
  `deny_once_ttl_seconds` (default 900). Rationale: the old
  confirmation dialog hung unattended/autonomous sessions waiting for
  a human, and was an unanswerable refusal in headless runs.
  Restore the old behavior with `"block_style": "ask"`.
- Because deny-once is model-facing, the block tier now also applies
  to subagent senders (ask remains root-only when selected).
- The deny is only issued when the challenge latch is durably
  recorded; an unwritable state dir degrades to warn-only rather
  than an unbreakable deny loop.

## 0.4.0 — 2026-08-15

- **Guard fresh-read.** The `SendMessage` guard now re-scans the
  target's transcript at decision time, so a re-tasked agent is judged
  on its current size, not the number recorded at its last stop (which
  could be half an hour and hundreds of thousands of tokens stale).
  One bounded parse inside the hook budget; any failure falls back to
  the stored reading, and the warning says which it presents. Fresh
  reads are skipped for transcripts over ~50MB.
- **New `compaction_action` knob** (`off` | `warn` | `block`,
  per-model overridable). **Behavior change: default `block`** — a
  compacted agent now requires your confirmation before being
  re-tasked (root session only; headless runs treat it as a refusal),
  where 0.3.0 only warned. Set `compaction_action: "warn"` to keep the
  old behavior; `off` stops compaction from triggering anything.
- Hook commands now end in `|| true` so a missing script or broken
  interpreter can never surface a blocking exit code (fresh installs
  and the plugin; existing manual installs keep their old command).
- Repo restructure: this repository is now also the "claude context
  tools" marketplace; sibling plugins land under `plugins/`.
  `uninstall.sh` markers narrowed to this plugin's own hook paths so a
  subagent-context uninstall can never remove a sibling's hooks.

## 0.3.0 — 2026-08-02

Renamed `subagent-gauge` → `subagent-context` (the fuel-gauge analogy is
gone). This touches every user-facing surface: plugin name, report
prefix (`[subagent-context]`), env prefix (`SUBAGENT_CONTEXT_*`), config
file (`~/.claude/subagent-context.json`), and state dir
(`~/.claude/subagent-context`). Old state is simply abandoned — delete
`~/.claude/subagent-gauge/` after upgrading; `uninstall.sh` still
removes pre-rename hook entries.

- `warn_tokens` default raised 150k → 250k.
- New `models` config key: per-model overrides for `warn_tokens`,
  `block_tokens`, and `report_min_tokens`, keyed by model-ID substring
  (longest match wins). Observer, guard, and status all honor them.

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
