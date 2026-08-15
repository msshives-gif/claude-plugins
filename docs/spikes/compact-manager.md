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
- Busy: "esc to interrupt" present (spinner line).
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
