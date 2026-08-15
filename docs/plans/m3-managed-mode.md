# M3 — compact-manager v0.2.0 `managed` mode (Layer 2), revision 3

Status: PLAN (pre-build). Rev 3 folds the rev-2 re-audit (Opus:
BUILD-WITH-CHANGES, 4 SHOULD-FIX; Sol: RETHINK, 4 CRITICAL + 6
MAJOR — convergent on lease atomicity, generation stability, journal
recovery, prune scope). Architecture is settled (both reports);
this revision pins the protocols. Spike evidence: S1-S7 in
docs/spikes/compact-manager.md.

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
The fixed instruction text must contain no shell metacharacters
(no `;`, quotes, `$`, backticks — enforced by a unit test).
`start` mode is the no-residual alternative: the launcher execs
claude DIRECTLY as the pane command (argv exec, never `sh -c`), so
no shell exists beneath and the pane dies with claude — enforced and
tested, not assumed. Without `--attended`, adopt refuses.

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

All lease acquisition, reclaim, release, and prune of managed
namespaces are serialized under ONE flock'd transaction file,
`managed/.txn.lock` (flock LOCK_EX, bounded wait). Within the
transaction: acquire `managed/leases/session-<sid>.json` FIRST, then
`managed/leases/pane-<sha(socket,pane)>.json`; if the second is
held, release the first and report the holder. Lease content:
{run_token, pid, proc_start, heartbeat_at}. Refuse if the existing
lease's heartbeat is fresher than 3x heartbeat cadence OR its
pid+start is alive; reclaim requires stale heartbeat AND dead
pid+start; unreadable or ambiguous liveness counts as LIVE. Removal
is conditional: re-read under the txn lock and remove only the exact
{run_token, inode} observed — a loser never deletes a successor.
Readiness handshake: `start`/`adopt` succeed only after the spawned
watcher (setsid, double-fork) confirms both leases + a READY journal
record within a bounded wait; otherwise they report failure and roll
back under the txn lock. Heartbeat: fixed 10s cadence, own timer,
never suspended. Every rail re-reads both leases; token mismatch =
immediate retire (no cleanup action — the successor owns the pane).
`stop <sid>`: under the txn lock, verify pid+start+run_token, signal
via pidfd_open when available (else re-verify pid+start immediately
before kill), bounded wait for lease release; the watcher's SIGTERM
handler defers exit during the typed critical section (R4 through
the SUBMITTED-or-abort journal record), then exits cleanly.

## Measurement: watcher-owned durable cursor

The watcher NEVER touches Layer-1 session state or its lock. It owns
`managed/watchers/<sid>.scan.json`: {device, inode, offset, current,
boundary_count, last_boundary: {offset, sha256_of_row}, model}.
Adopt does a full initial scan (transcripts are small enough — the
sibling measures ~0.07s per 5MB; no byte cap on the initial pass);
each tick processes appended bytes with a per-tick cap, and while
catching up (offset < size) NO trigger/injection actions are taken.
Inode change or shrink = explicit new generation + full rescan.
Boundary rows apply the postTokens reset (the M2 live lesson:
`current` = compactMetadata.postTokens until a newer terminal usage
row), so a freshly-compacted idle session can never re-trigger.

Generation := {device, inode, last_boundary.offset} — stable under
tail growth, unlike a windowed count. Latches, request fingerprints,
and attempt records key on it.

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
- Packet handling: only packets whose seq > packet_seq_at_prepare
  count. Our nonce (any nonce journaled for THIS attempt, including
  the retry's) → ACKED; never reinject after ACKED. A foreign
  packet (other/no nonce, or trigger=auto) → stand down: bounded
  wait for its boundary (completion_timeout_s); boundary → race
  lost, generation advances; no boundary → LATCHED + alert. A
  delayed own packet arriving after LATCHED is journaled and left
  alone (never triggers action).
- SUBMITTED with no packet after ack_timeout_s (120): exactly ONE
  retry, new nonce, same attempt generation — fixed invariant, not
  config. Second miss → LATCHED.
- ACKED with no boundary after completion_timeout_s (300): LATCHED +
  alert; compaction may be slow; never resubmit.
- LATCHED clears only when generation changes or pct falls below the
  re-arm band.
- Crash recovery by journal tail: PREPARED → CLEANUP_REQUIRED
  (composer may hold our text). TYPED_VERIFIED → SUBMISSION_UNCERTAIN
  (Enter may or may not have fired): take NO action until a boundary
  or own-nonce packet proves completion, the generation advances, or
  an operator runs `compact-manager resolve <sid>` (token-checked;
  resolution latches the current generation). SUBMITTED/ACKED →
  reconstruct timers from the journaled anchors and continue
  waiting. LATCHED → stay latched. CLEANUP_REQUIRED clears only via
  `resolve` after the human clears the composer — never
  automatically.

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
typed ledger event (lock-disciplined append, same discipline as the
sibling's ledger), (2) persistent in `compact-manager status` until
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
