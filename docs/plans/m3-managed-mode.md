# M3 — compact-manager v0.2.0 `managed` mode (Layer 2), revision 2

Status: PLAN (pre-build). Revision 2 folds the two-family plan audit
of revision 1 (Opus: BUILD-WITH-CHANGES, B1-B3+S1-S4; Sol: RETHINK,
2 CRITICAL + 9 MAJOR). Every numbered finding is resolved in the text
below or explicitly accepted as a documented residual. Gate history:
S5 proved the nonce correlation channel, S6 proved the six-rail
ladder aborts before any keystroke in every measured destructive
scenario (docs/spikes/compact-manager.md).

## The honest safety contract (Sol CRITICAL 2)

Typing into a pane has an irreducible check/action gap: between the
final pre-Enter revalidation and the Enter keypress (milliseconds),
the pane's state can change. The ladder makes every measured
destructive path abort before any keystroke, and R6' shrinks the
exit-to-shell window to that final gap — but "a newline can never
reach a shell" is NOT achievable with keystroke injection into a
shell-backed pane, and this plan does not claim it.

Consequences:

- `start` mode (launcher-created pane, claude IS the pane process,
  no shell beneath, nobody attended): the pane dies with claude, so
  the exit-to-shell case cannot exist. This is the no-residual mode.
- `adopt` mode (attended sessions — the user requirement): requires
  an explicit acknowledgement, `adopt --attended`, whose help text
  states the residual: "if claude exits to the underlying shell in
  the milliseconds between the final check and the keypress, one
  Enter (and at worst the /compact text) can reach your shell."
  Without the flag, adopt refuses shell-backed panes.
- Worst-case bound, for the risk text: the injected TEXT is always
  `/compact [cm-…] …` (never user-chosen), so the executed line in
  that worst case is an unknown-command error unless the USER's own
  half-typed text is on the shell line — and a non-empty shell line
  already trips R2/R5 while claude is alive; only text pasted INSIDE
  the final gap can be executed. Accepted, documented, ledgered.

## Binding contract (Opus B3/S1/S2; Sol CRITICAL 1, MAJOR 8)

Adoption target is `(tmux socket path, pane id)` — pane ids are
unique per server only; every subsequent tmux call uses `-S <socket>`.

Binding walk, all recorded at adopt time:

1. Pane facts: pane id, pane TTY (`#{pane_tty}`), pane root PID
   (`#{pane_pid}`) + its /proc starttime.
2. Foreground claude: read `tpgid` from `/proc/<pane_root_pid>/stat`
   (field 8) — the pane TTY's foreground process group. The group
   leader must be a live claude process. A descendant-tree walk is
   NOT used (it can bind a background/resumed claude while a
   different claude owns the pane — Sol's CRITICAL 1 scenario).
3. Registry: `~/.claude/sessions/<claude_pid>.json` must exist and
   its `procStart` must equal the LIVE `/proc/<claude_pid>/stat`
   starttime (field 22, parsed after the last `)`), exactly the
   `peers.is_alive` logic including the string-coercion caveat.
   The registry supplies sessionId and cwd; the transcript path is
   DERIVED as realpath(`~/.claude/projects/<slug(cwd)>/<sessionId>
   .jsonl`) with containment check under the projects dir (the
   registry carries NO transcript field — peers.py:153 pattern).
4. Stored binding: {socket, pane_id, pane_tty, pane_root_pid+start,
   claude_pid+start, fg_pgid, session_id, transcript_path,
   run_token, attended: bool}.

Revalidation (every poll tick AND inside the ladder at R1, R3, and
R6'): pane exists on that socket; pane_tty unchanged;
pane_current_command in `managed_pane_commands` (default
`["claude"]`, configurable — wrapper launches otherwise fail closed,
ledgered, Opus M4); tpgid group leader still == claude_pid; claude
procStart still matches live /proc; `pane_in_mode == 0`. ANY
mismatch or ambiguity → retire (ledgered), never guess.

Platform: v0.2.0 is Linux + WSL only — the starttime and tpgid
checks are /proc-based. macOS needs an adapter spike (M3.1) before
managed is offered there (Sol MAJOR 8); advisory mode is unaffected.

## Single-watcher lease (Opus B2; Sol MAJOR 5)

`leases/<sid>.json` AND `leases/pane-<socket-hash>-<pane>.json`,
both acquired atomically (O_CREAT|O_EXCL) at start/adopt, each
containing {run_token, pid, proc_start, heartbeat_at}. A live lease
(heartbeat fresher than 3x heartbeat cadence OR pid+start alive)
refuses a second watcher for the session OR the pane. Reclaim
requires BOTH heartbeat expiry AND pid+start dead. run_token is the
generation token: every tick and every ladder rail re-reads the
lease and retires if it no longer carries our token. Heartbeat is a
fixed 10s cadence on its own schedule — never suspended by backoff,
stability sleeps, or ack waits (Sol: no blocking sleeps in the state
machine; waits are `next_*_at` timestamps checked each tick).
`stop <sid>` verifies pid+start+run_token before signalling,
SIGTERMs, waits boundedly for lease release, then reports. SIGTERM
is deferred during the typed critical section (below).

## Measurement (Opus B1; Sol MAJOR 9)

The watcher NEVER reads or writes the Layer-1 session state file and
never takes the session lock. Each poll does a fresh bounded
read-only scan of the transcript tail (the sibling guard's
`measure(grace_ms=0)` pattern; byte cap; last terminal usage row +
boundary count). No shared cursor exists, so the advisor's offsets
can't be corrupted and the hook's 5s deadline is never consumed.
Packet files are read-only to the watcher.

## Attempt state machine (Sol MAJOR 3, 4, 6; Opus S3)

States, journaled to `watchers/<sid>.journal.jsonl` BEFORE the
action they precede: READY → TRIGGERED → PREPARED (pre-R4 journal) →
TYPED_VERIFIED (post-R5, pre-Enter journal) → SUBMITTED → ACKED →
BOUNDARY_CONFIRMED; plus DEFERRED(rail, next_attempt_at) and
LATCHED(generation).

- Generation := {transcript inode, boundary_count}.
- ACK: a packet whose custom_instructions carry OUR nonce → ACKED.
  A same-nonce packet must NEVER lead to reinjection — it proves
  PreCompact accepted the command (Sol MAJOR 3; the rev-1 "any fresh
  packet cancels the attempt" rule is replaced by: a FOREIGN packet
  (no/foreign nonce, or trigger=auto) → stand down; its boundary
  confirms the race loss; our own packet → ACKED).
- SUBMITTED without any packet after `ack_timeout_s` (120): exactly
  ONE retry with a new nonce — the retry count is a fixed v0.2.0
  invariant, not configuration (Sol MAJOR 10). Second failure →
  LATCHED for this generation.
- ACKED without a boundary after `completion_timeout_s` (300):
  LATCHED + ledger alert; never resubmit (compaction may just be
  slow — Opus M5).
- LATCHED clears when the generation changes (boundary advances or
  transcript replaced) or pct falls below the re-arm band.
- Model request override: at most ONE request-triggered override per
  generation regardless of request-id churn; the consumed
  fingerprint {generation, request_id} is journaled durably, so a
  watcher restart cannot replay it (Sol MAJOR 4).
- Crash/stop recovery: on adopt/start, the journal is read; a
  PREPARED or TYPED_VERIFIED tail entry means the composer may hold
  our text — `status` and the adopt output report "manual composer
  cleanup required"; the watcher stays DEFERRED until the composer
  is clean. Never auto-clear (Sol MAJOR 6). The R5-mismatch leftover
  behaves identically and is surfaced the same way, plus a
  `tmux display-message` on the target session (visible to the
  attended user; stderr is NOT a notification channel — Opus S4).

## Trigger sources

1. Threshold: fresh-scan pct >= `trigger_pct` (default: effective
   hard_pct; validated to (soft_pct, 1]).
2. Model request file `requests/<sid>.json`, written by the model
   (the managed-mode advisory text names the path and asks for
   `{"request_id": "<8-64 [A-Za-z0-9-]>", "reason": "..."}`).
   Validation: lstat regular non-symlink file, ≤4KB, exact schema,
   finite written_at within 10 minutes, else ignored (defensive
   parse; the file only ever triggers a ladder-gated /compact).
   Satisfaction: ANY boundary after written_at satisfies the request
   (prevents a redundant second compaction after a threshold-
   triggered one — Opus S3).

Threshold-triggered injections carry fixed instructions (Opus M3):
`/compact [cm-<nonce>] Preserve the task list, open decisions, and
file paths; the handoff file has the detail.` Request-triggered ones
append nothing model-controlled (the reason stays in the journal).

## Injection ladder (final form; Sol MAJOR 7)

R1 identity conjunction (above) → R2 composer idle → R3 stability
window + identity re-check → R4 type without Enter → R5 exact
composer verify → R6' full pre-Enter revalidation (identity
conjunction AND composer still exactly our text AND `pane_in_mode
== 0` AND no new packet/boundary since PREPARED) → Enter → submit
verify. Composer predicates are pinned, not vibes: the LAST `❯` line
in the visible pane with the U+00A0 empty signature, no
"esc to interrupt" anywhere, `pane_in_mode == 0`, pane dimensions
unchanged across R3; known modal markers (permission dialogs,
command menu) are explicit abort patterns. The S6 harness plus
sanitized captures land in `tools/s6/` as the regression suite
(two-watcher collision and wrapper-launch cases added), so the
predicates are testable against recorded reality.

Defer on any rail: exponential backoff 30s → 5min cap via
`next_attempt_at` (no sleeps). Every defer, retire, trigger, send,
ack, latch, and identity break is a typed ledger event (enumerated
in the build; append is lock-disciplined like the sibling's ledger —
Sol MAJOR 11).

## Nonce (Sol MINOR 12; Opus NIT)

`[cm-<16 hex chars from os.urandom>]`, anchored at the start of the
custom_instructions. It is a CORRELATION token (a local process can
read the packet — same threat model as the rest of the suite), not
authentication. Layer-1 change: `reorientation_text` strips a
leading `[cm-…]` token from the displayed instructions (raw value
stays in the packet for the watcher).

## Config (all clamped; Sol MAJOR 10)

trigger_pct (soft_pct, 1]; stable_ms >= 200; poll_s >= 5 (default
15, backs off to 60 far from threshold); ack_timeout_s >= 30
(default 120); completion_timeout_s >= ack (default 300); backoff
cap fixed 5min; deadline_hours clamped [1, 72] (default 24);
pane_commands list of exact strings (default ["claude"]). Retry
count is not configurable (fixed 1).

## Cut from v0.2.0 (Sol MINOR 13; rev-1 leftovers)

- stop_marker's activity write: removed (stale-by-construction and
  consumerless; the hook reverts to a pure no-op stub).
- Auto-adopt, multi-pane orchestration, non-tmux backends, macOS
  managed, watcher auto-restart, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE.

## Housekeeping (Sol MINOR 14; Opus M2)

Prune gains the watchers/, requests/, leases/ namespaces (age-based,
plugin-named files only, and NEVER a lease whose pid+start is alive).
CLI: `bin/compact-manager` inside the plugin dir, invoked by
absolute path (`/plugin` shows it); install/uninstall untouched
(managed adds no hooks), with tests covering the new namespaces.

## Testing

- Unit: binding validation, lease acquire/reclaim, state machine
  transitions (pure functions over injected clocks), request
  validation, ladder predicates against recorded pane captures,
  journal recovery, config clamps.
- Live gated regression (tools/s6/): every abort rail, happy path,
  ack timeout + single retry, native-race stand-down, two-watcher
  collision, wrapper launch, deadline expiry, stop during ladder,
  crash-with-typed-text recovery reporting.
