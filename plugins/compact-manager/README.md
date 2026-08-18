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
  only when it has verified the composer is input-safe: either the
  whole pane is idle, or — for a model-requested or hard-threshold
  compaction — the foreground turn has provably ended (paired hook
  markers) and the composer is empty, even while background tasks keep
  the pane busy. For autonomous sessions that must keep working past
  one context window.

## Quick start

```
/plugin marketplace add msshives-gif/claude-plugins
/plugin install compact-manager@claude-plugins
```

(or, without the plugin system: `./scripts/install.sh`)

Then turn it on — installing alone changes nothing. Either run the
`setup` slash command for a guided choice, or write the config
directly:

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

Five slash commands ship in `commands/`:

- **setup** — first-run walkthrough: pick a mode, set window
  overrides, learn the other commands (nothing runs automatically at
  install time — this is the guided way in)
- **attach** — adopt the current tmux pane (`adopt --attended`, with
  the result interpreted for you)
- **detach** — stop this session's watcher
- **status** — mode, live watchers, and per-session context usage
- **set** — relay a spoken threshold request ("compact this session at
  60%") into the `override` subcommand for the current session

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
`bin/compact-manager overview` prints the runtime readout (effective
thresholds global and per-model-override, watchers with attention
flags, per-session usage, and a recency-based current-session marker
with an age column to verify it) that `status`
relays — also usable directly, with no model turn at all. Each
session row also carries an `alive` verdict (`live` / `GONE` / `?`):
a row is only "state file touched in the last 24h", so the CLI
cross-checks the harness's sessions registry against `/proc`
(pid + start-time proof, mirroring the lease-reclaim discipline) to
say whether the claude process behind it is actually still running.
`GONE` rows are normal — dead sessions' state lingers until the 24h
window and the TTL reaper age it out. In JSON (`overview --json`)
this is `session_live: true/false/null` per session row. Session rows
also carry their EFFECTIVE thresholds: the advisor stamps its own
env-honoring, per-model-merged window/soft/hard/trigger into the
state file, and the overview prefers those stamps over re-deriving
from its own config — so per-session env overrides
(`COMPACT_MANAGER_*` at session launch) and per-model overrides both
display truthfully. Keys set via the `override` subcommand (see the
configuration reference) beat even the stamps, so a mid-session
change reads truthfully immediately rather than at the session's next
tool call. The text table's `trig` column and the JSON's
per-row `soft_pct`/`hard_pct`/`trigger_pct` carry the result.
The `cm` column (JSON: `cm_compacts`) counts the compactions the
manager itself fired for that session, from the watcher journal:
attempts whose `[cm-…]` nonce provably reached PreCompact — journaled
as ACKED, or as any record stamped `own_packet_proof` (a fast
completion where packet and boundary landed inside one poll, or a
retry aborted at R6′ by its first submission's late own packet) —
deduped per attempt so retries and recovery replays count once. The
proof is kept even when another abort outranks the own-packet detail
at R6′, and watcher recovery re-classifies the packet once before
journaling a terminal mapping. Native auto-compactions and human
`/compact`s never carry the nonce proof and are not counted; an
attempt that safety-latched or terminalized without its packet ever
being observed (rare crash/abort races) is conservatively not
counted — the number is a floor, never an overcount.
Blank/`null` means the count is unavailable: no retained watcher
journal (unmanaged, or long since reaped) or a journal that could
not be read — distinct from a watched session at `0`. The managed TTL reaper defers deleting a dead watcher's
journal while the session's state file is still inside the
overview's 24h window, so a displayed count can't flip to `null`
mid-display. The monorepo's `tools/status.py` is the
complementary dev-side readout (knob-by-knob config provenance and
hook wiring across the whole suite); the two deliberately do not
overlap.

In managed mode, session start/resume also injects one status line
saying whether a watcher demonstrably holds this session (fresh lease
heartbeat, or its pid verified alive — ambiguity reads as NOT
attached, inverting the lease-reclaim default), with the attach
command if not. A watcher whose pane rotates its session id under it
(`/clear`, in-app `/resume` — same claude process, new id) retires
with `reason=session_rotated` rather than watching the dead id's
frozen transcript; re-attach to cover the new session. Without it, "mode is managed but nobody adopted this
pane" is indistinguishable from fully-enabled until the 80% line.
Advisory and off modes stay silent, as before.

## Configuration reference

Config file `~/.claude/compact-manager.json`, or environment variables
`COMPACT_MANAGER_<NAME>` (env wins; `COMPACT_MANAGER_CONFIG` points at
an alternate file — handy for per-session configs).

One session's thresholds can also be changed mid-flight, without a
restart:

```
bin/compact-manager override <session-id> trigger=60% soft=50% hard=55% window=500000
```

Any subset of the four keys (percentages accept `60%`, `60`, or
`0.6`); `--clear` resets, and no assignments at all just shows the
current state plus the resulting effective thresholds. This writes
`<state_dir>/overrides/<session-id>.json`, which the advisor, a
RUNNING watcher, and both readouts all honor — the watcher re-reads it
within one poll. Values are range-validated (fractions in (0, 1],
window 10k–1e9) and `soft` is kept ≤ `hard`, but there is no other
clamping: this is the human's lever. In managed
mode the start-of-session status line bakes the exact command with the
real session id, and the `set` slash command translates a spoken
request into it. Session-id prefixes work here like they do for
`stop`/`resolve`.

| Knob | Default | Meaning |
|---|---|---|
| `mode` | `off` | `off` \| `advisory` \| `managed`. |
| `soft_pct` | `0.70` | First warning at this fraction of the window. |
| `hard_pct` | `0.80` | Firm warning here. |
| `rearm_band_pct` | `0.08` | How far usage must drop below a threshold before that warning can fire again. |
| `context_window` | `1000000` | Window size in tokens (the Claude 5 standard). Override smaller legacy models via `models`. |
| `models` | `{}` | Per-model overrides of the three knobs above (plus `managed_trigger_pct`), keyed by model-id substring. |
| `system_message` | `true` | Also show injected messages to the human. |
| `handoff_excerpt_bytes` | `4000` | How much of the handoff file rides along in the saved state. |
| `state_dir` | `~/.claude/compact-manager` | Where measurements, saved state, and handoff files live. |
| `ledger` / `ledger_max_bytes` | `true` / 5MB | Audit log of everything injected, size-rotated. |
| `state_ttl_days` | `7` | Old per-session files are cleaned up after this. |

Managed-mode knobs (all clamped to safe ranges):

| Knob | Default | Meaning |
|---|---|---|
| `managed_trigger_pct` | `hard_pct` | Watcher compacts at this fraction of the window. Also valid inside a `models` entry as a per-model override; an override not above the model's effective `soft_pct` falls back to the global trigger. Trigger overrides resolve independently of the window/soft/hard override matching, so a trigger-only pattern never shadows another pattern's other values. |
| `managed_stable_ms` | `300` | Pane must be unchanged this long before typing. |
| `managed_poll_s` | `15` | How often the watcher checks the session. |
| `managed_ack_timeout_s` | `120` | No confirmation the command was received → one retry, then stop and alert. |
| `managed_completion_timeout_s` | `300` | Command received but compaction never finished → stop and alert. |
| `managed_deadline_hours` | `24` | Watcher retires unconditionally after this. |
| `managed_pane_commands` | `["claude"]` | Only type into a pane running one of these. |

## How it works / limitations

Five thin hooks (PostToolUse, UserPromptSubmit, PreCompact,
SessionStart, and Stop), all fail-open — any error means silence,
never a blocked session. UserPromptSubmit and Stop additionally write
a paired turn marker (running/ended, keyed by the turn's `prompt_id`):
the watcher trusts "the foreground turn ended" only when the two
halves pair, which lets a requested compaction type into a session
whose background tasks never leave the pane idle — the composer must
still be exactly empty (dim ghost-suggestion text counts as empty; a
half-typed prompt or a modal never does), and input-opportunity
retries stay at the poll cadence instead of decaying into exponential
backoff. If a requested or hard-threshold compaction stays blocked
for two minutes, the watcher journals a starvation alert and flags
ATTENTION in `status`/`overview` while continuing to retry.
Measurement parses the session's transcript file incrementally;
compaction is detected from the transcript's own boundary records,
and in managed mode the typed command carries a one-time tag that
must round-trip through the PreCompact hook before the watcher
believes its compaction happened.
Watcher ownership is leased per session and pane, so two watchers can
never fight over one pane, and a crashed watcher is recovered
conservatively — anything uncertain becomes an alert for a human, not
a retry.

- Built on undocumented Claude Code internals (transcript format,
  session registry); format drift degrades to silence, and the
  measured shapes are pinned in `docs/spikes/compact-manager.md`.
- The turn-boundary lane's turn-end proof cannot survive a
  co-installed BLOCKING Stop hook (one that returns
  `decision: "block"` to force the turn to continue): the ended
  marker is written before the block takes effect, so the lane may
  treat a forcibly-continued turn as ended. Worst case is bounded —
  the typed `/compact` queues to the real turn boundary — but if you
  run blocking Stop hooks, don't rely on the boundary lane's timing.
  The marker records `stop_hook_active` for diagnosis. Conversely, a
  flaky Stop hook (missed or timed-out writes) leaves the marker
  reading "running" until the next turn completes, deferring both
  lanes in the interim — the safe direction, surfaced by the
  starvation alert rather than by unsafe typing.
- The modal veto recognizes the numbered-selection dialog shape
  (indented `❯ 1. …` rows); other dialogs are covered only by the
  composer checks (a dialog that hides the composer vetoes as
  absent/unknown).
- Measurement reflects the last completed model call.
- Headless (`-p`) sessions get the post-compaction state on their next
  prompt rather than instantly.
- Design details and the full safety analysis: the audit trail in
  `docs/plans/m3-managed-mode.md` and the live test suite in
  `tools/live-managed/`.

MIT.
