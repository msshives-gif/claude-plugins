# subagent-gauge

**A fuel gauge for your subagents.** Claude Code hooks that tell the
orchestrating agent how full each subagent's context is — and warn it
before it re-tasks an overloaded one.

## The problem

When a Claude Code session orchestrates subagents (background agents,
teammates, review panels), the orchestrator has no visibility into how
much context each subagent has consumed. Nothing in Claude Code reports
it — not task notifications, not the `/agents` UI, and hook payloads
carry no usage fields. So orchestrators routinely send "one more round"
to an agent sitting at 350k+ tokens. Long-context agents degrade: recall
drops, they anchor on their own earlier conclusions, and review passes
rubber-stamp. The orchestrator never finds out why.

(Feature requests for pieces of this have been closed as not-planned
upstream: [#5812](https://github.com/anthropics/claude-code/issues/5812),
[#22625](https://github.com/anthropics/claude-code/issues/22625).
[#16424](https://github.com/anthropics/claude-code/issues/16424) added
the hook payload fields that make this plugin possible, but no context
reporting.)

## What it does

Three small hooks, stdlib-Python only, all fail-open:

1. **Observer** (`SubagentStop`) — when a subagent stops, measures its
   context from its transcript (current, peak, compaction count), stores
   a per-agent state file, and queues a one-line report.
2. **Drain** (`PostToolUse`, parent session) — injects queued reports
   into the orchestrator's context on its next tool call:

   > `[subagent-gauge] w8d-builder (claude-opus-5): ~383k tokens — OVER
   > THRESHOLD: prefer spawning a fresh agent over re-tasking this one;
   > long-context agents degrade.`

3. **Guard** (`PreToolUse` on `SendMessage`) — if the orchestrator is
   about to message an agent whose last recorded context exceeds the
   threshold, injects a warning at exactly that moment (or, with
   `hard_block`, requires user confirmation).

No extra model turns, no cost added to the subagent, and the subagent's
final deliverable is never touched.

## Install

Requirements: a Claude Code version whose SubagentStop hook payload
carries `agent_id` / `agent_transcript_path` (present since the
[#16424](https://github.com/anthropics/claude-code/issues/16424) work;
verify with any subagent stop and `scripts/status.py`), and `python3`
(3.9+) on PATH. macOS/Linux only — on Windows the hooks exit silently
(the locking uses `fcntl`).

**As a plugin:**

```
/plugin marketplace add msshives-gif/subagent-gauge
/plugin install subagent-gauge@subagent-gauge
```

**Manual (no plugin system):**

```bash
git clone https://github.com/msshives-gif/subagent-gauge
cd subagent-gauge && ./scripts/install.sh   # merges into ~/.claude/settings.json, with backup
```

`./scripts/uninstall.sh` reverses it. Restart running sessions either
way; hooks are snapshotted at startup.

Verify: spawn any subagent, then check `python3 scripts/status.py` and
watch for a `[subagent-gauge]` line in the parent after its next tool
call.

## Configuration

Every knob works as an environment variable (`SUBAGENT_GAUGE_<NAME>`)
or a key in `~/.claude/subagent-gauge.json` (env wins; point
`SUBAGENT_GAUGE_CONFIG` at a different config path if you want one):

| Knob | Default | Meaning |
|---|---|---|
| `warn_tokens` | `150000` | At/above this, reports carry the OVER-THRESHOLD warning and the guard fires. Tune to your models: ~150k is conservative for 200k-window models; raise for 1M-context agents. |
| `report_min_tokens` | `0` | Only queue reports for agents at/above this size. `0` = report every stop. |
| `hard_block` | `false` | Guard requires user confirmation (`permissionDecision: ask`) instead of just warning. |
| `system_message` | `true` | Also show each report to the human in the UI. |
| `drain_batch_max` | `20` | Max reports injected per drain. |
| `flush_grace_ms` | `4000` | How long the observer waits for the transcript to finish flushing (see DESIGN). Keep well under the observer's 10s hook timeout. |
| `state_dir` | `~/.claude/subagent-gauge` | Where state, queues, and the ledger live. |
| `ledger` / `ledger_max_bytes` | `true` / 5MB | Append-only JSONL audit log, rotated once over size. |
| `state_ttl_days` | `7` | Stale session state/queues get pruned after this. |

## Pull-side status

The injected reports are push; for pull, or for a human:

```bash
python3 scripts/status.py             # all sessions, newest first
python3 scripts/status.py --session 02ff  # prefix match
```

(Plugin installs don't give you a clone; run it from the plugin cache —
the path shown by `/plugin` — or clone the repo just for the script.)

## Limitations (read before relying on it)

- **Undocumented internals.** Context is measured from the subagent
  transcript JSONL and its `meta.json` sidecar — internal Claude Code
  formats with no stability guarantee. The parser is defensive and the
  hooks fail open: a format change degrades to "no reports," never to
  broken sessions. Built and verified against Claude Code as of
  2026-08-01.
- **Reports are stop-time snapshots.** An agent mid-burst has grown past
  its last report. In our production use `SubagentStop` fires after
  every agentic burst (each time a teammate goes idle, not just final
  completion — observed behavior, not documented), so numbers refresh
  often, but they are floors, not ceilings.
- **Post-compaction counts.** After auto-compaction, `current` reflects
  the compacted (smaller) context. The report exposes `peak` and a
  `COMPACTED xN` flag — treat any compaction as a degradation signal.
- **Guard covers `SendMessage` re-tasking.** Fresh `Agent` spawns start
  at zero context and need no guard; in current Claude Code, resuming an
  existing agent goes through `SendMessage`, which is what the guard
  watches. If your setup has no `SendMessage` tool (no
  teammates/background agents), the guard simply never fires; the
  reports still work.
- **Root session only.** Reports and the guard serve the top-level
  orchestrator. A subagent that itself spawns sub-subagents doesn't
  receive gauge reports (its PostToolUse drains are deliberately
  suppressed to keep the parent's queue intact).
- **Delivery is best-effort.** A drained report that fails to inject
  (process killed mid-drain) is not redelivered; the per-agent state
  files — which the guard reads — are the durable record.
- **Overhead.** The drain runs on every parent tool call: one Python
  interpreter spawn, ~50ms, no model tokens. The observer runs once per
  subagent stop (bounded by `flush_grace_ms`).
- **Install one way, not both.** Plugin install *and* `install.sh`
  together would run every hook twice and duplicate reports.
- **Privacy.** State files and the ledger record agent names, models,
  session IDs, token counts, and transcript paths — locally, under
  `state_dir`. No network access anywhere.

## Uninstall

Plugin: `/plugin uninstall subagent-gauge`. Manual:
`./scripts/uninstall.sh`. Then delete `~/.claude/subagent-gauge/`.

## Development

```bash
python3 -m unittest discover tests
```

Design rationale, the empirically verified hook-channel behavior this
depends on, and rejected alternatives: [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT.
