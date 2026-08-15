# compact-manager

Lets a long-running Claude Code session survive its own context window:
warns the model as its context fills, captures a wake packet before
compaction, and reorients the model after it.

## The problem

An autonomous orchestrator eventually fills its context. Native
auto-compact is a last-resort backstop — measured on a real machine it
fired only at effective window exhaustion (preTokens ~218k on a
nominal 200k window), and a headless session gets "Prompt is too long"
errors instead of compaction. Worse, after any compaction the model
resumes from a summary with no guaranteed pointers to its working
state. Nothing warns the model beforehand, and nothing reorients it
afterward.

## What it does (Layer 1 — advisory)

Five thin hooks, all fail-open, all inert until you set a mode:

1. **advisor** (`PostToolUse`) — incrementally measures the session's
   own transcript (normally a byte-offset resume; a replaced or
   shrunken file triggers one full reparse). Crossing
   `soft_pct` (default 70%) injects ONE advisory telling the model to
   write its task state to a session-specific handoff file and wrap up
   / compact at a natural boundary; crossing `hard_pct` (80%) injects a
   firmer one. Hysteresis: one advisory per genuine crossing.
2. **reorient** (`UserPromptSubmit`) — same measurement + drain at
   prompt time, so tool-free conversation is covered too.
3. **precompact** (`PreCompact`, manual + auto) — persists the wake
   packet: mechanical facts (pre-compaction size, trigger, `/compact`
   instructions) plus a bounded excerpt of the model-written handoff
   file. Never blocks a compaction.
4. **session_start** (`SessionStart`, compact) — injects the
   reorientation immediately after compaction (verified working on
   current interactive Claude Code). Opportunistic: it never consumes
   the packet.
5. The durable delivery is the next `PostToolUse`/`UserPromptSubmit`
   after a compaction is detected in the transcript
   (`compactMetadata` boundary rows — deterministic, source-attributed).
   At-most-once per packet; the SessionStart copy may duplicate it
   (harmless by design).

`stop_marker` (`Stop`) exists for the future managed mode and is a
no-op otherwise.

## Modes

| Mode | Meaning |
|---|---|
| `off` (default) | Installing changes nothing. |
| `advisory` | Everything described above. Cross-platform (pure Python hooks). |
| `managed` | Reserved for Layer 2 (a watcher that types `/compact` into your tmux pane at safe moments). NOT BUILT YET — currently behaves as `advisory` plus an activity marker written at each Stop (nothing reads it yet). |

Enable per config (`~/.claude/compact-manager.json`) or env:
`COMPACT_MANAGER_MODE=advisory`.

## Install

```
/plugin marketplace add msshives-gif/subagent-context
/plugin install compact-manager@subagent-context
```

Manual (no plugin system): `./scripts/install.sh` merges the five
hooks into `~/.claude/settings.json` (backup taken;
`./scripts/uninstall.sh` reverses it, touching only this plugin's
entries). Either way the hooks stay inert until you set a mode.

## Configuration

Env `COMPACT_MANAGER_<NAME>` or `~/.claude/compact-manager.json`
(env wins; `COMPACT_MANAGER_CONFIG` points elsewhere):

| Knob | Default | Meaning |
|---|---|---|
| `mode` | `off` | `off` \| `advisory` \| `managed`. |
| `soft_pct` | `0.70` | First advisory at this fraction of the window. |
| `hard_pct` | `0.80` | Firm advisory here. Native auto-compact fires far later (measured ~1.09× window). |
| `rearm_band_pct` | `0.08` | Re-arm an advisory only after dropping this far below its threshold. |
| `context_window` | `200000` | Window tokens; per-model via `models` (e.g. `{"[1m]": {"context_window": 1000000}}`). |
| `models` | `{}` | Per-model overrides for `soft_pct`/`hard_pct`/`context_window`. |
| `system_message` | `true` | Show injections to the human too. |
| `handoff_excerpt_bytes` | `4000` | Byte cap on the handoff excerpt embedded in the wake packet (delivery is capped to match). |
| `state_dir` | `~/.claude/compact-manager` | Measurements, packets, handoff files. |
| `ledger` / `ledger_max_bytes` | `true` / 5MB | Audit log of injections and packets, rotated at the size limit. |
| `state_ttl_days` | `7` | Per-session files older than this are cleaned up (checked at most once a day). |

## Limitations

- Built on undocumented transcript internals (measured shapes pinned in
  `docs/spikes/compact-manager.md`); format drift degrades to silence.
- Measurement reflects the last completed model call.
- The advisory can only *advise*: neither hooks nor the model can
  invoke `/compact` in current Claude Code. The model can ask you, or
  wrap up cleanly and let native compaction hit a prepared session.
- Headless (`-p`) sessions never see the SessionStart copy; the
  durable drain covers them on their next prompt.

MIT.
