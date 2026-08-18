# compact-manager spikes S1-S5 (2026-08-15)

Instrumented haiku sessions (headless `-p` loop + interactive tmux),
hooks logging every PreCompact/SessionStart payload. Fixtures pinned at
`fixtures/transcripts/compact_rows.jsonl` (sanitized real rows).
Machine: Claude Code ~2.1.23x, Linux. All numbers are one machine, one
day — treat as current-behavior anchors, not contracts.

## S1 — SessionStart(source=compact) wake injection

**WORKS interactively** (contradicts the stale bug report #15174): after
a manual `/compact`, the model quoted the additionalContext sentinel
verbatim when asked. **Does NOT reach the model across headless
`-p --continue` resumes** (post-compact ask answered NONE). Consequence:
`session_start.py` is a real drain point for interactive sessions, but
the durable packet + PostToolUse/UserPromptSubmit drains remain the
load-bearing path (and the only path for headless).

## S2 — native auto-compact firing point

**Auto-compact fired at preTokens 218,707 on a nominal 200k window**
(haiku) — it climbed through 85%…97% footer readings without firing and
compacted only at effective window exhaustion. It is a last-resort
backstop, NOT an ~83% trigger, on this machine/version. Consequences:
`hard_pct` defaults (0.75-0.80) comfortably precede native; the
advisory band is wide; never rely on native firing "early".

Headless behavior differs: an over-window prompt in `-p --continue`
errors "Prompt is too long" — PreCompact(trigger=auto) fires but no
compaction persists to the transcript, then the turn dies. A headless
orchestrator gets ERRORS, not compaction: the advisory must fire well
before the window fills.

## S3 — compacted-transcript shape

Compaction APPENDS (same file, monotonically growing): a
`type:"system", subtype:"compact_boundary"` row with
`compactMetadata: {trigger: "manual"|"auto", preTokens, postTokens,
cumulativeDroppedTokens, durationMs, preservedSegment{...uuids}}`,
followed by a `type:"user", isCompactSummary: true` row carrying the
summary. `compactMetadata.trigger` + monotonic boundary count =
deterministic, source-attributed compaction detection (better than
counting isCompactSummary alone).

## S4 — composer idle/busy signatures (tmux capture-pane)

- Idle: a line matching `^❯.{0,3}$` — NOTE the "empty" composer is
  `❯` + a NO-BREAK SPACE (U+00A0), not an ASCII space — AND no
  "esc to interrupt" anywhere in the pane.
- Busy: "esc to interrupt" present (spinner line). **Qualified
  2026-08-18 (live captures, fixtures in
  plugins/compact-manager/tests/fixtures/):** the phrase FLICKERS —
  it can be absent mid-generation (spinner line without it) and can
  appear for background tasks after the foreground turn ended. It is
  therefore not a turn-end discriminator in either direction; the
  paired UserPromptSubmit/Stop activity marker (shared `prompt_id`)
  is the semantic turn-end source, and mid-generation safety rests on
  R3 stability plus the marker reading "running".
- Ghost suggestions (2026-08-18): after a turn ends with background
  tasks running, the composer may render a dim suggestion — plain
  capture shows `❯`+NBSP+text, byte-identical to typed input. With
  `capture-pane -e`, suggestion text is ESC[2m-wrapped (per word when
  soft-wrapped, plain spaces between words); typed text carries no
  dim. Modal selection rows are `❯ `+ASCII-space (`❯ 1. Yes`).
- Footer carries `[tokens: N | ctx: NN%]` — a live context readout
  (nulls to 0% right after compaction until the next call).
- Typed text echoes in the composer after `send-keys -l` (submission
  check: grep the first ~40 chars before pressing Enter).

## S5 — PreCompact payload + the nonce channel

Payload fields (both triggers): `custom_instructions, cwd,
hook_event_name, prompt_id, session_id, transcript_path, trigger`.
NOTE: the field is `trigger` ("manual"|"auto"), not `compact_reason`.
`custom_instructions` carries the text after `/compact` VERBATIM
(empty for auto). **`/compact <text>` works in headless `-p` too.**
Consequence for Layer 2: an injected `/compact [cm-nonce-XXXX] …` can
be authenticated by reading the nonce back from
PreCompact(manual).custom_instructions — the ack-channel gap flagged in
the plan audit is closed by design, pending the L2 spikes (S6).

## Session registry / SessionStart sources

SessionStart fires with `source` ∈ startup | resume | compact (and
docs list clear/fork); `-p --continue` fires `resume` every call.

## M2 — live verification of Layer 1 (2026-08-15)

Scripted haiku session (tmux) with the five real hooks via
`--settings`, `COMPACT_MANAGER_MODE=advisory`, claimed 100k window.
Observed:

- Advisory injected once at the hard crossing; NO repeat on the
  following no-op turn (hysteresis held across advisor + reorient).
- `/compact focus on …` → packet seq 1 persisted with the
  custom_instructions verbatim, pre_current/pre_peak, and a FRESH
  handoff excerpt (model had written the file the advisory named).
- Post-compaction: reorientation delivered exactly twice as designed —
  SessionStart(compact) copy + the durable drain — and the CAS
  advanced (`last_drained_packet_seq` 1, boundaries 1); the later
  reorient hook found it drained and stayed silent.
- Measurement lag is one full model call: at PostToolUse time the
  current turn's terminal usage row is not yet flushed, so with
  ~30k-token jumps the soft level was stepped over entirely. Fine at
  realistic tool-call granularity; documented limitation.
- DEFECT FOUND AND FIXED: after the boundary row, `current` still held
  the stale pre-compact reading, so the advisor re-fired "~116% full —
  compaction imminent" at the freshly-compacted session (ledger inject
  n=2). Fix: a boundary row resets `current` to
  `compactMetadata.postTokens` (0 if absent); any later usage row
  overrides. Regression-pinned in ScanTests.

## S6 — destructive-safety ladder for tmux injection (2026-08-15)

Harness: a six-rail injector (R1 pane_current_command whitelist, R2
idle-composer signature, R3 stability window + command re-check, R4
type WITHOUT Enter, R5 exact-composer verify, R6 Enter + submission
verify; never clears the line) run adversarially against a live haiku
session. Results:

- Half-typed user text in the composer (incl. an rm -rf string):
  aborted at R2, ZERO keystrokes typed, user text untouched.
- User typing 150ms into the ladder: aborted at R3, zero pollution.
- Busy/streaming model: aborted at R3 (R2 catches the spinner case).
- Real shell pane (bash): aborted at R1; with a half-typed dangerous
  command in the shell, nothing was typed and nothing executed
  (canary file never created).
- Happy path: full ladder passed and the nonce round-tripped into
  PreCompact(manual).custom_instructions — authenticated ack works
  through the ladder, closing the loop S5 opened.
- Live claude reports pane_current_command == "claude" exactly ->
  R1 can be a strict whitelist. A claude-as-pane-command session
  DIES on exit (no shell beneath), which R1 reports as no-pane.
- Multi-client count via list-clients: not exercisable from inside
  tmux (0 seen); needs a real second terminal to verify — M3 keeps
  it as an advisory signal, not a load-bearing rail.

Residual windows, for the M3 plan: (1) keystrokes landing between the
R5 verify and R6 Enter (~sub-400ms) — pane is verifiably claude's
composer at that point, so the worst case is a /compact with a
garbage suffix, not shell execution; (2) claude exiting to an
underlying shell mid-ladder after R3's re-check (~1s window) — M3
adds a pane_current_command re-check immediately before Enter to
shrink this to milliseconds. Verdict: attended-session injection is
viable with the ladder as primary defense.

## S7 — foreground-group binding verified live (2026-08-15)

On a real attended pane (user shell -> claude): `#{pane_pid}` = the
shell; `/proc/<pane_pid>/stat` tpgid (field 8 in proc(5) numbering =
6th field after the comm's closing paren — parse after the LAST ")")
resolved directly to the claude process, which was its own process
group leader and had a live `~/.claude/sessions/<pid>.json` entry
whose sessionId matched the session actually running in that pane.
The rev-2 binding walk (pane -> tpgid -> registry, procStart
live-checked) is implementable exactly as planned.
