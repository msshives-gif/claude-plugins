# M3 — compact-manager v0.2.0 `managed` mode (Layer 2), revision 4

Status: BUILD-READY. Rev 4 folds the round-3 convergence audit
(Opus: BUILD-WITH-CHANGES, 2 should-fix; Sol: RETHINK, 5 blockers —
all protocol pins, zero redesigns; the two reports overlap on the
parent/watcher lease handoff, READY conflation, and composer
disposition). Dispatcher's call 2026-08-15: with three narrowing
rounds and no architectural movement since round 1, this revision
folds every round-3 finding and proceeds to build; the two-family
CODE audit and the live regression suite remain mandatory gates.
Spike evidence: S1-S7 in docs/spikes/compact-manager.md; the S6
harness + sanitized captures live in tools/s6/.

## The honest safety contract

Typing into a pane has an irreducible check/action gap. The ladder
aborts before any keystroke in every measured destructive scenario
(S6), and the final pre-Enter revalidation shrinks the residual to
milliseconds — but the residual exists. The defensible bound, which
is what `adopt --attended`'s acknowledgement text states verbatim:

> In adopt mode, the automation may in a worst-case race deliver the
> fixed `/compact …` bytes and one Enter to the underlying shell.
> The resulting shell behavior is not semantically bounded;
> concurrent user input can alter the executed line.

No stronger claim (e.g. "unknown-command error") is made anywhere.
The fixed instruction text must contain no command separators,
quoting, substitution, redirection, pipe/background operators, or
newlines (`;`, `&`, `|`, `>`, `<`, `(`, `)`, quotes, `$`,
backticks, newline — the full denylist enforced by a unit test; the
nonce's `[`/`]` are glob characters that stay literal when unmatched
and are included in the test as pinned-literal).
`start` mode is the no-residual alternative: the launcher passes
claude to tmux as a MULTI-ARGUMENT command vector (a single command
string makes tmux insert `sh -c`), so no shell exists beneath and
the pane dies with claude — proven by a live test (pane disappears
on claude exit), not just a unit argv check. Without `--attended`,
adopt refuses.

## Binding contract

Adoption target: `adopt [-S <tmux socket>] -t <pane-id> --attended`.
Pane ids are per-server; every tmux call thereafter uses that socket.

Walk (all recorded): pane id, `#{pane_tty}`, `#{pane_pid}` + its
/proc starttime; tpgid = field 8 of `/proc/<pane_pid>/stat` in
proc(5) numbering (6th field after the comm's LAST `)`); the
foreground group LEADER pid (claude_pid := tpgid) must be a live
claude whose `~/.claude/sessions/<pid>.json` procStart equals the
LIVE `/proc/<pid>/stat` field 22 (peers.is_alive logic, string
coercion included). A claude that is only a MEMBER of the foreground
group is unsupported (documented). Transcript path is DERIVED:
realpath(`~/.claude/projects/<slug(cwd)>/<sessionId>.jsonl`) with
containment check (registry has no transcript field). Stored
binding: {socket, pane_id, pane_tty, pane_root_pid+start,
claude_pid+start, session_id, transcript_path, run_token, attended}.

Revalidation at every tick and at rails R1/R3/R6': pane exists on
socket, tty unchanged, `pane_current_command` in
`managed_pane_commands` (default `["claude"]`), tpgid == claude_pid,
claude procStart matches live /proc, `pane_in_mode == 0`, BOTH
leases still carry our run_token. Foreground-group loss BEFORE R4 is
`DEFERRED(foreground_lost)` — a normal job-control transient; the
watcher waits, revalidating claude pid+start, and retires only if
the claude process dies or the deadline passes. Any failure from R4
through submission resolution is `CLEANUP_REQUIRED` (below), never a
silent retire.

Platform: Linux + WSL only in v0.2.0 (/proc mechanics). macOS =
M3.1 adapter spike. Advisory mode unaffected.

## Ownership: leases as one transaction

All lease acquisition, reclaim, release, heartbeat, and prune of
managed namespaces are serialized under ONE flock'd transaction
file, `managed/.txn.lock` (flock LOCK_EX, bounded wait). THE LOCK IS
NEVER HELD ACROSS A WAIT — each transaction is a short
read-check-write; `start`/`adopt`/`stop` release it before waiting
on the watcher, and heartbeats take it for milliseconds per beat.

Acquisition is performed by the FINAL double-forked watcher child
(the only party whose pid+start belong in the lease — a lease
carrying the short-lived CLI's pid would look reclaimable while the
watcher is alive, the double-owner hole). Order: acquire
`managed/leases/session-<sid>.json` FIRST, then
`managed/leases/pane-<sha(socket,pane)>.json`; if the second is
held, release the first in the same transaction and report the
holder. Lease content: {run_token, pid, proc_start, heartbeat_at};
run_token = 16 hex chars from os.urandom. Refuse if the existing
lease's heartbeat is fresher than 3x heartbeat cadence OR its
pid+start is alive; reclaim requires stale heartbeat AND dead
pid+start; unreadable or ambiguous liveness counts as LIVE. Removal
and heartbeat replacement are conditional: re-read under the txn
lock, verify our run_token, and replace/remove only the exact
{run_token, inode} observed — a loser never deletes a successor,
and a heartbeat can never resurrect a rolled-back lease. Heartbeat
updates BOTH leases in one transaction (tmp + fsync + rename); on
partial failure, retain ownership, alert, retry next beat.

Startup handshake (cancellable): the CLI creates a pipe, spawns the
watcher (setsid, double-fork), and waits bounded on the pipe. The
child checks the pipe for cancellation BEFORE acquiring leases and
again before entering its loop; on success it writes
{run_token, pid} up the pipe and journals a WATCHER_READY lifecycle
record — which is NOT an attempt state: attempt-tail recovery skips
lifecycle records, so a recovered SUBMISSION_UNCERTAIN /
CLEANUP_REQUIRED / SUBMITTED / LATCHED tail survives adoption
untouched (the child reports the recovered state up the pipe; the
CLI prints it). WATCHER_READY does not wait for the initial
transcript scan — liveness and scan-catch-up are decoupled; no
trigger fires until the scan is caught up. On timeout the CLI
writes the cancellation byte, signals, waits for the child's death,
then conditionally removes any leases carrying the child's token.

Every rail re-reads both leases; token mismatch = retire WITHOUT
touching the pane — but if it happens at or after R4, the retiring
watcher journals the typed-text hazard and raises the alert before
exiting (the successor owns the pane; the hazard record is for the
human). `stop <sid>`: one transaction to verify pid+start+run_token,
then RELEASE the lock, signal via pidfd_open when available (else
re-verify pid+start immediately before kill), and poll bounded for
conditional lease release. The watcher's SIGTERM handler defers
exit during the typed critical section (R4 through the
SUBMITTED-or-abort journal record), then exits cleanly.

## Measurement: watcher-owned durable cursor

The watcher NEVER touches Layer-1 session state or its lock. It owns
`managed/watchers/<sid>.scan.json`: {device, inode, observed_size,
offset, file_epoch, current, boundary_count, last_boundary: {offset,
sha256_of_row}, anchor: {offset, sha256_of_first_row}, model}.
Adopt does a full initial scan, uncapped (the watcher is a daemon,
not a 5s-budget hook); each tick processes appended bytes with a
per-tick cap against a recorded stat snapshot, and a trigger may
fire only after a final re-stat shows no unprocessed bytes and no
trailing fragment. `file_epoch` is a monotonic counter incremented
on device/inode change, size regression (stat size <
observed_size), or anchor mismatch (the stored first-row hash — or
last_boundary hash when present — no longer matches at its offset
on reopen); an epoch increment forces a full rescan and is an
explicit new generation, which catches same-inode truncate-and-
regrow and offline delete/recreate that inode+size alone miss.
Boundary rows apply the postTokens reset (the M2 live lesson), so a
freshly-compacted idle session can never re-trigger.

Generation := {file_epoch, last_boundary.offset,
last_boundary.sha256} (epoch 0 + null boundary before any) — stable
under tail growth, collision-proof under replacement. Latches,
request fingerprints, and attempt records key on it.

## Attempt state machine + journal

Journal: `managed/watchers/<sid>.journal.jsonl`, append + flush +
fsync BEFORE the action each record precedes; torn trailing lines
ignored; every record carries {schema: 1, ts, attempt_id, run_token,
nonce, generation, retry_n, packet_seq_at_prepare, timers: {...}}.
Retention: journal + scan state + requests are pruned ONLY under the
txn lock and only when the session's lease pair is provably
reclaimable (never while referenced by a live run_token).

States: READY → TRIGGERED → PREPARED → TYPED_VERIFIED → SUBMITTED →
ACKED → BOUNDARY_CONFIRMED; DEFERRED(reason, next_attempt_at);
LATCHED(generation); SUBMISSION_UNCERTAIN; CLEANUP_REQUIRED.

- PREPARED is journaled before R4 (typing); TYPED_VERIFIED after R5,
  before Enter; SUBMITTED after the Enter keypress returns.
- Packet classification: `attempt_packet_seq_floor` is captured ONCE
  at attempt creation and is IMMUTABLE across the retry (a retry
  never re-baselines — the round-3 double-compaction hole). An
  own-nonce packet (any nonce journaled for this attempt) → ACKED
  REGARDLESS of seq (nonces are unique; the seq floor exists only
  for foreign classification, and a Layer-1 state reset must not
  turn our own ack invisible). A packet with seq > floor and no
  attempt nonce, or trigger=auto → FOREIGN. An existing packet at
  attempt creation counts as settled only if its boundary is already
  confirmed (its base_compaction_count vs the cursor) — otherwise it
  is treated as an in-flight foreign packet. Note: Layer 1
  overwrites the packet file per PreCompact, so a native compaction
  immediately after ours can hide our own packet — the misread
  degrades to the foreign flow (stand down, boundary confirms,
  generation advances): safe, documented.
- FOREIGN before R4: stand down, bounded wait for its boundary
  (completion_timeout_s); boundary → race lost, generation advances;
  no boundary → SAFETY-LATCHED + alert. FOREIGN observed at/after
  R4 with our text's disposition unproven → CLEANUP_REQUIRED, not
  the generic stand-down (our bytes may sit in the composer).
- SUBMITTED with no packet after ack_timeout_s (120): exactly ONE
  retry, new nonce, same attempt, same floor — fixed invariant.
  Second miss → SAFETY-LATCHED.
- ACKED with no boundary after completion_timeout_s (300): SAFETY-
  LATCHED + alert; never resubmit. Own nonce NEVER leads to
  reinjection, in any state.
- Latch taxonomy (round-3 blocker): a THRESHOLD latch (plain
  hysteresis) re-arms when pct falls below the re-arm band. A
  SAFETY latch (missing ack, missing boundary, foreign-uncertain,
  resolve-imposed) clears ONLY on generation advance or operator
  `resolve` — never on a pct drop, because a delayed accepted
  /compact can still land after the drop.
- Timers: journaled as monotonic deadlines plus the boot_id
  (/proc/sys/kernel/random/boot_id). Same-boot recovery
  reconstructs and continues; cross-boot (boot_id differs) →
  conservative SAFETY-LATCH, never an early retry.
- Crash recovery by journal tail (lifecycle records like
  WATCHER_READY are skipped when finding the attempt tail):
  PREPARED → CLEANUP_REQUIRED (composer may hold our text).
  TYPED_VERIFIED → SUBMISSION_UNCERTAIN (Enter may or may not have
  fired — this also covers an R6'-abort that crashed before its
  CLEANUP_REQUIRED record): take NO action; an own-nonce packet
  moves it to ACKED (completion still requires the boundary); a
  boundary/generation advance completes it; otherwise only operator
  `resolve` clears it, and — same rule as CLEANUP_REQUIRED — resolve
  requires the human to have cleared the composer first. SUBMITTED/
  ACKED → reconstruct timers (per the boot rule) and continue
  waiting. LATCHED → stay latched. CLEANUP_REQUIRED clears only via
  `resolve` after the human clears the composer — never
  automatically.
- `resolve <sid>` authority, under the txn lock: with a live lease,
  only the lease-holding watcher's records are resolvable (token-
  checked). With NO live lease (both leases reclaimable), the
  operator may resolve records of dead run_tokens — that is exactly
  the crashed-watcher cleanup path. Resolution journals a
  resolve-imposed SAFETY latch on the current generation.

## Triggers

1. Threshold: cursor pct >= trigger_pct (default effective hard_pct;
   validated to (soft_pct, 1]) AND not catching up.
2. Model request `managed/requests/<sid>.json`, advertised schema
   exactly `{"request_id": "<8-64 chars [A-Za-z0-9-]>", "reason":
   "<optional, ignored for control>"}`. Receipt time = the file's
   lstat mtime at FIRST observation (the model never supplies wall
   time); stale if mtime > 10 min old at first observation.
   Validation: lstat regular non-symlink, <= 4KB, exact schema, else
   ignored. At most ONE request-triggered override per generation
   regardless of id churn; consumed fingerprint {generation,
   request_id} journaled. A request is satisfied (and its file
   ignored thereafter) once the generation advances after first
   observation.

Injected text, fixed, metacharacter-free:
`/compact [cm-<nonce>] Preserve the task list and open decisions to
the handoff file` — nonce = 16 hex chars from os.urandom
(correlation, not authentication; Layer 1's reorientation_text
strips a leading `[cm-…]` token from display, raw kept in packet).

## Ladder (unchanged rails, pinned predicates)

R1 identity conjunction → R2 composer idle (LAST `❯` line with the
U+00A0 empty signature; no "esc to interrupt"; `pane_in_mode == 0`)
→ R3 stability (two captures stable_ms apart hash-equal, pane
dimensions unchanged, identity re-check) → R4 type (no Enter) → R5
exact composer verify → R6' full revalidation (identity conjunction
+ composer still exactly our text + both leases + no packet with
seq > packet_seq_at_prepare + no boundary advance) → Enter → submit
verify. Modal/menu chrome: R2's empty-composer + pane_in_mode
requirements fail closed on all observed dialogs; the S6 harness and
sanitized captures land in `tools/s6/` and the enumerated
abort-pattern list is finalized from that corpus at build time, with
the standing rule "anything unrecognized ⇒ not idle ⇒ defer".
Defers: backoff 30s → 5min cap via next_attempt_at timestamps (no
sleeps).

## Notification (best-effort, layered)

CLEANUP_REQUIRED / SUBMISSION_UNCERTAIN / LATCHED alerts are: (1) a
typed ledger event (O_APPEND single-write records, atomic at these
sizes — the same lockless discipline the Layer-1 ledger actually
uses), (2) persistent in `compact-manager status` until
resolved, (3) best-effort `tmux display-message -c <client>` to
EVERY client attached to the target session on the bound socket
(delivery journaled; explicitly best-effort — a detached session has
no clients), and (4) echoed by the next `adopt`/`start`/`stop` on
that session. stderr is not a channel.

## Config (managed_* namespace, all clamped)

managed_trigger_pct (soft_pct, 1]; managed_stable_ms >= 200 (300);
managed_poll_s >= 5 (15, backs off to 60 far from threshold);
managed_ack_timeout_s >= 30 (120); managed_completion_timeout_s >=
ack (300); managed_deadline_hours [1, 72] (24);
managed_pane_commands (["claude"]). Retry count fixed at 1. Backoff
cap fixed at 5 min. Heartbeat cadence fixed at 10s.

## Cut from v0.2.0

stop_marker activity write (hook reverts to no-op stub); auto-adopt;
multi-pane; non-tmux backends; macOS managed;
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE; watcher auto-restart.

## CLI

`bin/compact-manager start [--session-name N] -- claude <args…>` |
`adopt [-S socket] -t <pane> --attended` | `status` | `stop <sid>` |
`resolve <sid>`. Lives in the plugin dir, invoked by absolute path.
Managed adds no hooks; install/uninstall untouched, with tests
covering the new managed/ namespaces.

## Testing

Unit: binding validation; txn-lock lease acquire/rollback/reclaim
(incl. the loser-never-deletes-successor rule); state machine over
injected clocks (every transition incl. SUBMISSION_UNCERTAIN and the
packet_seq baseline); journal recovery from every tail state + torn
tails; cursor postTokens reset + generation stability under growth,
shrink, inode change; request validation/fingerprints; config
clamps; instruction-text metacharacter test; argv-exec test for
start.
Live gated regression (tools/s6/): every abort rail, happy path,
ack-timeout single retry, native-race stand-down, two-watcher
collision (both orders), wrapper launch fail-closed, stop during
ladder, kill -9 between TYPED_VERIFIED and SUBMITTED →
SUBMISSION_UNCERTAIN → resolve flow, deadline expiry, Ctrl-Z
foreground-loss defer/return.
