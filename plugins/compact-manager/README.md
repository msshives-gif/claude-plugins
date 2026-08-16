# compact-manager

Long Claude Code sessions eventually fill their context window and get
compacted — and the model comes back from compaction disoriented, with
no reliable pointers to what it was doing. This plugin fixes that, in
two stages:

- **advisory** — warns the model *before* the window fills so it can
  write down its working state, then hands that state back to it right
  after compaction. Pure hooks, any platform, zero risk.
- **managed** (opt-in, per session) — additionally runs `/compact` for
  you at the right moment, by typing it into the session's tmux pane
  only when it has verified the pane is idle and safe. For autonomous
  sessions that must keep working past one context window.

## Quick start

```
/plugin marketplace add msshives-gif/claude-plugins
/plugin install compact-manager@claude-plugins
```

(or, without the plugin system: `./scripts/install.sh`)

Then turn it on — installing alone changes nothing:

```
echo '{"mode": "advisory"}' > ~/.claude/compact-manager.json
```

That's the whole setup for advisory mode. It applies to all sessions,
takes effect immediately (hooks re-read the file every time), and you
can override per session with the `COMPACT_MANAGER_MODE` environment
variable at launch.

## What you'll see in advisory mode

1. Nothing, most of the time. The hooks quietly measure the session's
   own transcript as it grows.
2. At **70%** of the context window, the model gets one message telling
   it to write its task state to a handoff file (the message includes
   the path) and to wrap up or compact at a natural boundary. At
   **80%** it gets one firmer warning. No nagging — one message per
   threshold crossing.
3. When compaction happens (you run `/compact`, or native auto-compact
   fires), the plugin saves the essentials: what triggered it, how big
   the session was, and an excerpt of the model's own handoff notes.
4. On the first opportunity after compaction, that saved state is
   injected back — so the model resumes knowing what it was doing
   instead of guessing from the summary.

The default window is 1M (the Claude 5 standard). If you run smaller
legacy models, tell it so percentages are right:

```json
{"mode": "advisory",
 "models": {"haiku": {"context_window": 200000}}}
```

(model names are matched by substring, longest match wins — `"haiku"`
catches any haiku model id). Getting this wrong is asymmetric: too
small a window warns and compacts far too early, which can wreck a
long workload; too large just means advisories never fire and native
compaction proceeds as if the plugin were off.

## Managed mode: it runs /compact for you

Advisory can only *advise* — Claude Code gives hooks no way to invoke
`/compact`, and native auto-compact fires only at effective window
exhaustion (measured ~109% of the nominal window; headless sessions
just start erroring instead). Managed mode closes the gap with a small
watcher daemon that you attach to a specific session:

```
plugins/compact-manager/bin/compact-manager start -- claude [args…]   # new session
plugins/compact-manager/bin/compact-manager adopt -t %pane --attended # existing pane
plugins/compact-manager/bin/compact-manager status                    # what's running
plugins/compact-manager/bin/compact-manager stop <session-id>
```

Set `mode` to `managed` (same file/env as above) so the hooks
cooperate, then attach a watcher to the session you care about.
Nothing is ever watched automatically.

The watcher waits until the session crosses the trigger threshold
(default: the 80% line) or until the model itself asks for compaction
(the advisory tells it how). Then it types `/compact` plus a short
instruction into the pane and presses Enter — but only after checking,
immediately before every keystroke, that it's the right pane, the
right claude process, the input line is empty and has stayed still,
and no other compaction is already underway. Any doubt at any step
means it backs off and tries later. It never deletes anything you
typed, and if anything is ambiguous *after* it has typed, it stops,
raises a persistent alert in `status`, and waits for you to look —
it never guesses.

Honest risk note, because you must pass `--attended` to acknowledge
it: on a pane you're actively typing in, there is an unavoidable
millisecond-scale window where, in a worst-case race, the `/compact`
text and one Enter could land in the shell underneath claude instead.
The text contains no shell metacharacters (enforced by test), but
your own concurrent keystrokes could change what the shell sees.
`start` avoids even that: it launches claude with no shell under the
pane at all. Managed mode is Linux/WSL-only in this release.

## Slash commands and the start-of-session status line

Three slash commands ship in `commands/`:

- **attach** — adopt the current tmux pane (`adopt --attended`, with
  the result interpreted for you)
- **detach** — stop this session's watcher
- **status** — mode, live watchers, and per-session context usage

Plugin-system installs get them namespaced (`/compact-manager:attach`
etc.). `scripts/install.sh` installs substituted, marker-tagged copies
beside the settings file as `/compact-manager-attach` etc.; rerun
install.sh after editing `commands/`. An existing file without the
marker is never overwritten, and `uninstall.sh` removes only files
carrying the marker (or legacy symlinks resolving into this clone).
`${CLAUDE_PLUGIN_ROOT}` substitution in command markdown is
version- and install-form-dependent (script installs never get it;
plugin installs have open issues against it), so the command bodies
locate the CLI defensively rather than trusting the placeholder.

The data-gathering lives in the CLI, not in command prose:
`bin/compact-manager overview` prints the runtime readout (watchers
with attention flags, per-session usage, and a recency-based
current-session marker with an age column to verify it) that `status`
relays — also usable directly, with no model turn at all. The monorepo's `tools/status.py` is the
complementary dev-side readout (knob-by-knob config provenance and
hook wiring across the whole suite); the two deliberately do not
overlap.

In managed mode, session start/resume also injects one status line
saying whether a watcher demonstrably holds this session (fresh lease
heartbeat, or its pid verified alive — ambiguity reads as NOT
attached, inverting the lease-reclaim default), with the attach
command if not. Without it, "mode is managed but nobody adopted this
pane" is indistinguishable from fully-enabled until the 80% line.
Advisory and off modes stay silent, as before.

## Configuration reference

Config file `~/.claude/compact-manager.json`, or environment variables
`COMPACT_MANAGER_<NAME>` (env wins; `COMPACT_MANAGER_CONFIG` points at
an alternate file — handy for per-session configs).

| Knob | Default | Meaning |
|---|---|---|
| `mode` | `off` | `off` \| `advisory` \| `managed`. |
| `soft_pct` | `0.70` | First warning at this fraction of the window. |
| `hard_pct` | `0.80` | Firm warning here. |
| `rearm_band_pct` | `0.08` | How far usage must drop below a threshold before that warning can fire again. |
| `context_window` | `1000000` | Window size in tokens (the Claude 5 standard). Override smaller legacy models via `models`. |
| `models` | `{}` | Per-model overrides of the three knobs above, keyed by model-id substring. |
| `system_message` | `true` | Also show injected messages to the human. |
| `handoff_excerpt_bytes` | `4000` | How much of the handoff file rides along in the saved state. |
| `state_dir` | `~/.claude/compact-manager` | Where measurements, saved state, and handoff files live. |
| `ledger` / `ledger_max_bytes` | `true` / 5MB | Audit log of everything injected, size-rotated. |
| `state_ttl_days` | `7` | Old per-session files are cleaned up after this. |

Managed-mode knobs (all clamped to safe ranges):

| Knob | Default | Meaning |
|---|---|---|
| `managed_trigger_pct` | `hard_pct` | Watcher compacts at this fraction of the window. |
| `managed_stable_ms` | `300` | Pane must be unchanged this long before typing. |
| `managed_poll_s` | `15` | How often the watcher checks the session. |
| `managed_ack_timeout_s` | `120` | No confirmation the command was received → one retry, then stop and alert. |
| `managed_completion_timeout_s` | `300` | Command received but compaction never finished → stop and alert. |
| `managed_deadline_hours` | `24` | Watcher retires unconditionally after this. |
| `managed_pane_commands` | `["claude"]` | Only type into a pane running one of these. |

## How it works / limitations

Five thin hooks (PostToolUse, UserPromptSubmit, PreCompact,
SessionStart, and a reserved Stop no-op), all fail-open — any error
means silence, never a blocked session. Measurement parses the
session's transcript file incrementally; compaction is detected from
the transcript's own boundary records, and in managed mode the typed
command carries a one-time tag that must round-trip through the
PreCompact hook before the watcher believes its compaction happened.
Watcher ownership is leased per session and pane, so two watchers can
never fight over one pane, and a crashed watcher is recovered
conservatively — anything uncertain becomes an alert for a human, not
a retry.

- Built on undocumented Claude Code internals (transcript format,
  session registry); format drift degrades to silence, and the
  measured shapes are pinned in `docs/spikes/compact-manager.md`.
- Measurement reflects the last completed model call.
- Headless (`-p`) sessions get the post-compaction state on their next
  prompt rather than instantly.
- Design details and the full safety analysis: the audit trail in
  `docs/plans/m3-managed-mode.md` and the live test suite in
  `tools/live-managed/`.

MIT.
