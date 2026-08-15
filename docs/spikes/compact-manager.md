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
