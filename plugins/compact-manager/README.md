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

`stop_marker` (`Stop`) is a reserved no-op (kept so the hook surface
is stable across versions).

## What it does (Layer 2 — managed)

`managed` mode adds a per-session watcher daemon that restores what
the advisory alone cannot: actually running `/compact` at the right
time. Hooks cannot invoke `/compact`, so the watcher types it into
the session's tmux pane — only after a six-rail safety ladder proves
the pane is the right pane, the composer is empty and stable, and
nothing changed underneath it between verification and the final
Enter. Any doubt at any rail aborts before a keystroke; ambiguity
after typing raises a persistent alert (`compact-manager status`)
rather than guessing. The watcher never clears your composer and
never restarts itself.

```
plugins/compact-manager/bin/compact-manager start -- claude [args…]
plugins/compact-manager/bin/compact-manager adopt [-S socket] -t %pane --attended
plugins/compact-manager/bin/compact-manager status | stop <sid> | resolve <sid>
```

- **start** launches claude in a fresh tmux session with no shell
  underneath (tmux gets the command as an argument vector), so a
  worst-case race has nowhere to land — the pane dies with claude.
- **adopt** attaches a watcher to an EXISTING pane you point it at —
  including an attended session you are typing into. That is why it
  requires `--attended`: in a worst-case race the fixed `/compact …`
  bytes and one Enter could reach the shell under claude, and
  concurrent input can alter the executed line. The instruction text
  is metacharacter-free by construction and enforced by test, but the
  residual is real and adopt makes you acknowledge it.
- Triggers: crossing `managed_trigger_pct` (defaults to `hard_pct`),
  or the model writing a request file (the advisory tells it the
  path), restoring model-chosen timing.
- Compaction is confirmed end-to-end: a nonce in the injected text
  round-trips through the PreCompact wake packet, and completion
  requires the transcript's compaction boundary. Missing either
  raises a safety latch that never re-injects; `resolve <sid>` clears
  it after you have checked (and cleared) the composer yourself.
- Ownership is leased (session + pane) under one lock file with
  heartbeats, so two watchers can never share a pane; a crashed
  watcher's journal is recovered conservatively on the next adopt.
- Bounded lifetime: `managed_deadline_hours` (default 24) retires the
  watcher unconditionally.

## Modes

| Mode | Meaning |
|---|---|
| `off` (default) | Installing changes nothing. |
| `advisory` | Everything described above. Cross-platform (pure Python hooks). |
| `managed` | Everything above, plus an opt-in per-session watcher (started explicitly via the CLI below) that types `/compact` into your tmux pane at verified-idle moments. Linux/WSL only in this release. |

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

Managed-mode knobs (same file/env, all clamped to safe ranges):

| Knob | Default | Meaning |
|---|---|---|
| `managed_trigger_pct` | `hard_pct` | Watcher injects at this fraction of the window; must be above `soft_pct`. |
| `managed_stable_ms` | `300` | Pane must be byte-identical across this window before typing (min 200). |
| `managed_poll_s` | `15` | Watcher tick (min 5; backs off to 60 far from threshold). |
| `managed_ack_timeout_s` | `120` | No PreCompact ack within this → exactly one retry, then a safety latch (min 30). |
| `managed_completion_timeout_s` | `300` | Ack but no transcript boundary within this → safety latch (min: the ack timeout). |
| `managed_deadline_hours` | `24` | Unconditional watcher lifetime cap (1–72). |
| `managed_pane_commands` | `["claude"]` | `pane_current_command` whitelist; anything else defers. |

## Limitations

- Built on undocumented transcript internals (measured shapes pinned in
  `docs/spikes/compact-manager.md`); format drift degrades to silence.
- Measurement reflects the last completed model call.
- The advisory can only *advise*: neither hooks nor the model can
  invoke `/compact` in current Claude Code. `managed` mode closes that
  gap via tmux; outside managed mode the model can ask you, or wrap up
  cleanly and let native compaction hit a prepared session.
- Managed mode is Linux/WSL + tmux only (it reads `/proc` to bind the
  pane to the exact claude process). Advisory mode is unaffected.
- Headless (`-p`) sessions never see the SessionStart copy; the
  durable drain covers them on their next prompt.

MIT.
