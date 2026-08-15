# M3 — compact-manager v0.2.0 `managed` mode (Layer 2)

Status: PLAN (pre-build). Gate history: L2 was gated at the suite plan
audit pending S5/S6; S5 proved the nonce ack channel
(custom_instructions round-trips into PreCompact), S6 proved the
six-rail safety ladder aborts before any keystroke in every
destructive scenario, including attended-session cases
(docs/spikes/compact-manager.md). Scope decision (Matt, 2026-08-15):
managed mode must work in ATTENDED main tmux sessions, not only
launcher-created unattended panes — this reinstates pane adoption,
which the earlier audit cut, now justified by the S6 evidence and the
contracts below.

## What managed mode adds over advisory

A per-session watcher process that actually types
`/compact [cm-nonce-…] <instructions>` into the session's tmux pane at
a safe, model-elected or threshold-elected moment. Layer 1 already
handles everything after the compaction (wake packet, reorientation).

## Binding a watcher to a session

Two entry points, one binding contract:

- `bin/compact-manager start [--session-name N] -- claude <args…>` —
  launcher creates a tmux pane, spawns claude, binds directly.
- `bin/compact-manager adopt <tmux-pane-target>` — attended sessions.
  Explicit pane target required; no auto-guessing. Binding walks:
  pane → pane PID → claude process → `~/.claude/sessions/<pid>.json`
  registry entry (sessionId, transcript path, procStart). The stored
  binding is {pane_id, claude_pid, proc_start, session_id,
  transcript_path, run_token}.

Identity revalidation happens EVERY watcher cycle and again inside
the ladder: pane exists, pane_current_command == "claude" (S6: exact
value), pane PID chain unchanged, registry procStart matches
(anti-PID-recycle). Any mismatch → watcher retires itself (ledgered),
never degrades to guessing.

## Watcher loop (one python process per session)

Every poll (default 15s, backing off to 60s when far from threshold):

1. Revalidate identity (above).
2. Incremental transcript scan (reuse the Layer-1 lib: offsets,
   boundaries, per-model window).
3. Cancel-conditions first: a new compaction boundary or a fresh
   packet file (any trigger — native auto-compact or manual) clears
   any pending attempt and resets the attempt latch.
4. Trigger check: pct >= managed_trigger_pct (default = hard_pct) OR
   a fresh model request file (state_dir/requests/<sid>.json, written
   by the model; the advisory text in managed mode names this path
   and the handoff path). Request = {request_id, written_at}.
5. If triggered and no latch: run the S6 ladder + R6' (re-check
   pane_current_command immediately before Enter — closes the ~1s
   exit-to-shell residual to milliseconds). Defer on any tripped rail
   with exponential backoff (30s → 5min cap), every defer ledgered.
6. On send: record attempt {nonce, generation, sent_at}; await ack.

## Ack, retry, latch

Ack = the Layer-1 precompact hook persists the packet whose
custom_instructions contain our nonce (watcher polls the packet file),
then the transcript shows the boundary. Timeout
(managed_ack_timeout_s, 90) → ONE retry with a new nonce. Second
failure → attempt latch: suppress threshold-triggered attempts until
the boundary count advances or pct falls below the re-arm band. A NEW
model request id overrides the latch exactly once (fresh generation).
A nonce-less manual/auto compaction observed while waiting = user or
native compaction won the race: treat as success, stand down.

## Lifecycle (background-work discipline)

- Watcher state: state_dir/watchers/<sid>.json {pid, proc_start,
  pane, run_token, started_at, heartbeat}; heartbeat = file mtime,
  refreshed each poll.
- Absolute deadline: managed_deadline_hours (24) → self-exit;
  re-adopt to continue.
- Self-retire on: identity break, session registry entry gone/stale,
  pane gone, deadline, `compact-manager stop <sid>`.
- `compact-manager status` lists watchers with sid, pane, pct, last
  action; `stop` kills cleanly. No orphan mode: a watcher that cannot
  write its heartbeat exits.
- Never auto-restart; starting a watcher is always a human action.

## Injection safety (final form)

S6 rails R1-R6 + R6' pre-Enter command re-check. Never clear the
composer; on R5 mismatch our typed text stays visible (recognizable
`/compact [cm-…` prefix) and we ledger + notify via stderr; the human
deletes it. Residual accepted (S6): keystrokes landing inside the
R5→R6 window produce at worst a /compact with a garbage suffix in a
verified claude composer — never shell execution.

## Config (compact-manager.json / env, all with managed_ prefix)

trigger_pct (default: effective hard_pct), stable_ms 300, poll_s 15,
ack_timeout_s 90, max_retries 1, deadline_hours 24, backoff caps.
`mode: managed` enables the advisory text variant that names the
request-file path; hooks are unchanged otherwise (stop_marker gains
its activity write, already stubbed).

## Portability

tmux backend only (Linux/macOS/WSL). Native Windows: managed
unavailable, advisory unaffected. InjectionBackend stays an internal
seam; PTY/iTerm backends deliberately not built.

## Testing

- Unit: binding parse/validate, trigger/latch/generation state
  machine (pure functions), ladder step logic against captured pane
  fixtures (S6 outputs), request-file validation.
- Live (gated, manual): S6 harness re-run through bin/compact-manager
  adopt against a haiku session — happy path, every abort rail, ack
  timeout + retry, native-race cancel, deadline self-expiry, stop.
- The S6 adversarial suite becomes a regression script under
  tools/ (not CI — needs a live model).

## Out of scope

Multi-pane orchestration, auto-adopt, non-tmux backends, Windows
managed, watcher auto-restart, and any use of
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (lower-only: it can only race us).
