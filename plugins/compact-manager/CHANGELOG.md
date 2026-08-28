# Changelog — compact-manager

## Unreleased

- Watcher fix: a session-requested compact whose attempt wedged on a
  composer mismatch and then self-recovered via a clean ACTIVE clear
  (`RECOVERED`/`composer_cleared`, never own-proved) re-arms the
  request instead of dropping it. Consuming the request generation on
  `TRIGGERED` was permanent, so one transient R5 failure silenced the
  session's compact ask until the threshold trigger (observed live
  2026-08-27, fbeb0bf1: request at ~50% ctx dropped after recovery).
  The `RECOVERED` row carries `request_rearmed: true`, journal replay
  reaches the same set after a restart, and retype churn stays
  bounded by `ACTIVE_RECOVERY_LIMIT`. The passive lane
  (`composer_clean`) still never re-arms: it proves the typed bytes
  vanished, not by whose hand.
- Diagnosability: `CLEANUP_REQUIRED` rows for `R5_composer_mismatch`
  and `R6_prime_composer_mismatch` now journal what the failing
  verify actually observed (`expected_text`, `observed_text`,
  `observed_composer`, `observed_modal`, or
  `observed_capture_failed`) — the live 2026-08-27 mismatch was
  undiagnosable post-hoc without it. Texts are capped raw, not
  whitespace-collapsed, so NBSP-vs-space differences survive.

- Resume nudge: every **manager-fired** compaction now nudges, not
  just session-requested ones — /compact never re-invokes the model,
  so a compaction the watcher typed strands an autonomous session no
  matter who asked for it (Matt, 2026-08-26). Foreign/native
  boundaries and unproven boundaries still never nudge, and the
  default stays off.
- Resume nudge fix: the causal binding now compares the RUNNING-half
  marker identity (prompt_id + written_at) instead of the full
  revision tuple. Claude Code re-fires the Stop hook for the /compact
  turn itself, rewriting the ended marker's timestamp with the same
  prompt_id seconds after the watcher's Enter; exact-tuple equality
  read that as "a turn slipped in" and stood the nudge down on every
  real compact (observed live: fbeb0bf1 19:32, `RESUME_NUDGE
  skipped_session_resumed`, session idle 3.5h). Every genuine new
  PROMPT rewrites the running half via UserPromptSubmit, so the
  narrowed binding still stands down for real resumptions; the one
  promptless continuation — a blocking Stop hook forcing the turn
  onward — is gated separately: an ended marker carrying
  `stop_hook_active` refuses arming and vetoes Enter (the same gate
  active clearing uses), with the first blocked Stop invocation a
  documented limitation the flag cannot see. A compact submitted
  while markers were unreadable (`activity_rev_at_submit` None)
  declines the nudge (`skipped_no_submit_binding`) — an ended pair
  first observed after the boundary cannot be proven to predate it.

- Watcher fix: an `ACKED` attempt whose compaction never lands now
  latches `missing_boundary` when the completion timeout expires even
  while a packet lingers on disk. Previously the packet-classification
  early returns (OWN, and FOREIGN when a native compaction replaced
  our packet — Sol round-1) re-entered `ACKED` on every tick before
  the timer branch could run, so the attempt sat `ACKED` forever with
  no alert (the known issue noted in the resume-nudge release). A
  lingering own packet can no longer resurrect a `LATCHED` attempt to
  `ACKED` either — a recovered cross-boot latch would compare a stale
  monotonic deadline and silently un-latch (Sol round-1). The latch
  keeps the nonce identity, so a boundary that lands late still
  confirms with the fired-compact proof intact — and a late own
  packet observed while latched still stamps and journals the
  fired-compact proof (metadata-only journal row; no state change,
  no alert), so `cm_compact_count` keeps the fired compact across
  restarts (Sol round-2). The proof-only append serializes under the
  txn lock with a tail re-check: a concurrent operator resolution
  stays authoritative and the proof is merged into a fresh
  operator_resolved row rather than either side losing to lock
  order; transient failures keep the proof pending for retry; and
  startup recovery journals a proof gained by its packet inspection
  even when the recovered state is unchanged (Sol rounds 3-4).
- Watcher: leaving an alert state journals its transition row under
  the txn lock, serialized with `resolve` — an unlocked append could
  interleave with a concurrent operator resolve and leave a stale
  wedge tail describing a latch the runtime no longer holds (status
  reporting a wedge while the live watcher runs free; Sol round-6).
  This covers the THRESHOLD-latch re-arm appends too (Sol round-7).
  Fail-open: a wedged lock falls back to the unlocked append rather
  than losing the row.
- `resolve`: a PREPARED/TYPED_VERIFIED raw tail whose run token holds
  a LIVE session lease is refused ("stop the watcher first") — those
  alerts are crash-recovery projections of a possibly-live ladder,
  which would type and submit straight through the resolution (Sol
  round-7); a dead lease keeps the crash tail resolvable. Both the
  projected and raw views now come from ONE journal snapshot, so a
  ladder advancing between two reads cannot slip past the guard (Sol
  round-8).
- Tests: the resume-nudge config tests now pin `COMPACT_MANAGER_CONFIG`
  to a nonexistent path — they read the machine's real
  `~/.claude/compact-manager.json` before, so enabling the nudge
  locally failed `test_default_off`. The live suite (`tools/
  live-managed/run.sh`) had the same hole and now exports a
  nonexistent `COMPACT_MANAGER_CONFIG` (observed live: s18_default_off
  failed spuriously on a machine with the nudge enabled).

- Watcher: resume nudge (`managed_resume_nudge`, default off). After a
  **session-requested** compaction's boundary is confirmed, the
  watcher types one continuation prompt and presses Enter — `/compact`
  never re-invokes the model, so an autonomous session that requested
  its compaction at a turn boundary otherwise strands itself until a
  human prompts it (observed live 2026-08-26: 7h45m idle after a clean
  requested compact). Arming is deliberately narrow (Sol round-1):
  request-triggered attempts only, with manager-origin proof of the
  boundary (prior `ACKED` or an own-packet proof — a human or native
  `/compact` winning the race never arms), on a fresh transition only
  (a restart's resurrected boundary tail cannot re-arm), and only with
  positive ended-turn marker evidence — the armed revision causally
  binds every later keystroke to the exact turn that requested the
  compaction, re-proven immediately before Enter. The pending nudge
  cancels itself if a second boundary lands, another compaction goes
  in flight, or the session resumes some other way; it stands down
  permanently on any visible composer input (it never clears a draft);
  and its 180s window bounds keystrokes, not just entry — the deadline
  is rechecked before the first typed byte and before Enter. Every
  terminal disposition is journaled as a `RESUME_NUDGE` row — a state
  invisible to recovery, wedge verdicts, and status by design. The
  watcher publishes its effective setting into its leases, and the
  soft-threshold advisory tells the model to end its turn and let the
  watcher restart it only when a live attached watcher positively
  reports the nudge enabled.
- Watcher: active composer clearing (`managed_clear_composer`, default
  on). When a compaction is due and unsubmitted text sits in the
  composer — a human draft or the watcher's own wedged `/compact` line
  — the watcher now clears it with bounded `C-e`,`C-u`,`Backspace`
  key rounds (`C-e` first so a mid-line cursor cannot strand text;
  the `Backspace` eats the newline of a hard-multiline draft, a no-op
  otherwise; progress is judged on the whole composer region) and
  proceeds, instead of deferring (drafts) or wedging until an operator
  arrives (`R5_composer_mismatch` sat 15h live on 2026-08-25/26).
  Gates: positive ended-turn marker evidence (never marker-less,
  running, or `stop_hook_active` turns), schedulable leader, no modal,
  parseable composer, and the whole composer region — continuation
  lines of a hard-multiline draft included — byte-stable across a
  stability window (500ms floor, matching the boundary lane), all
  re-proven before every keystroke, so someone caught mid-typing
  defers the clear; text typed during the brief clearing loop itself
  can still be lost, with the journaled record as the recovery path.
  Every clear journals `COMPOSER_CLEAR` before the first key with an
  excerpt of the destroyed line (plus the full region for multiline
  drafts); failures journal `COMPOSER_CLEAR_FAILED` and degrade to a
  defer. Bounds: 3 keyed clear invocations per attempt (successes
  included, charged durably before the first key) and 3 active
  self-recoveries per confirmed boundary — both restored from the
  full journal on watcher restart, so restart loops cannot refresh
  them. Active recovery extends to `R4_send_failed` and
  `R6_prime_composer_mismatch`, stands down permanently when a live
  packet classifies OWN (stamping the fired-compact proof durably),
  waits on FOREIGN packets, yields to an operator resolve before
  every keystroke, and proves the hazard gone by direct observation
  (packet re-read after the clear) before re-arming. Enter stays
  gated on the full R5/R6' byte-exact chain. `false` restores the
  strictly-hands-off contract, including the keyless probe-based R5
  recovery. Live-suite scenarios s08/s15 re-pinned from "never
  clears" to "clears and submits", s17 added (hard-multiline draft);
  adopt acknowledgement text extended.

- Watcher: R5 typed-text verification now re-captures the pane up to
  three times (read-only) before declaring `composer_mismatch` — a
  transient repaint racing the single capture terminalized a healthy
  attempt (observed live 2026-08-25: 14h of silently suspended
  auto-compaction). R6' still re-proves everything before Enter.
- Watcher: self-recovery from `CLEANUP_REQUIRED/R5_composer_mismatch`
  — the one cleanup reason whose hazard (our typed bytes) is fully
  observable. Read-only probes every 60s require: composer empty, no
  modal, no attempt nonce anywhere in the pane, transcript caught up
  and provably nonce-free from the attempt's boundary offset, no
  in-flight packet, a schedulable (R/S) leader — a SIGSTOPed claude
  can hold the bytes unconsumed in the tty input queue (Sol
  blocker) — a provably ENDED turn (the boundary lane's marker-pair
  evidence: mid-generation an empty composer proves nothing about
  queued stdin) whose ended marker does not carry stop_hook_active
  (a blocking Stop hook writes "ended" then forces generation to
  continue) and whose RUNNING half is a NEW identity relative to the
  snapshot taken at the wedge's first sighting — identity is causal
  where wall-clock ordering is not (a clock step would launder a
  stale pair), and only a UserPromptSubmit rewrites the running half
  (the ended half alone is rewritten by every Stop and proves
  nothing), so a new running identity means a prompt was submitted
  post-wedge, whose FIFO tty ordering proves our earlier bytes were
  consumed first — and transcript growth since the wedge (Sol
  blockers, rounds 2-6; a dead-idle session never self-recovers;
  only confirmed ENOENT counts as an absent marker, every other
  inspection error is unprovable and blocks). The snapshot itself is
  quarantined 30s past the first sighting so a pre-wedge prompt's
  delayed hook write lands inside it, never after it. The recovery
  commit is keyed on the attempt's identity, not the current binding
  token, so a successor watcher that adopted the pane after the
  wedging watcher died can complete the inherited recovery (final
  audit). A journal record without a manager-shaped
  attempt_id never recovers (identity guards the growth baseline).
  Two consecutive clean probes commit a durable `RECOVERED` journal
  record — under the txn lock with a tail re-read, so an operator
  resolve landing mid-probe always stands (Sol blocker), and the
  RECOVERED append alone is the commit point (a failed follow-up
  status write cannot strand a wedged watcher under a recovered
  journal — Opus finding). Restarts start fresh instead of
  resurrecting the wedge. All other cleanup reasons keep
  operator-resolve-only semantics; SUBMISSION_UNCERTAIN still clears
  only on positive boundary proof or an operator resolve, never on
  probes.
- Wedged watchers are now surfaced INSIDE the session, not just as a
  one-shot tmux flash: the advisor injects one warning per distinct
  wedge (any usage level, with the exact `resolve` command and the
  re-arm step) plus an all-clear when the journal POSITIVELY clears;
  SessionStart reports a wedged or resolve-latched watcher instead of
  the plain "attached" line. The journal verdict is three-valued
  (wedged / clear / unknown) with a hard 64KB tail-read byte cap —
  "no claim" stays silent and never cancels a delivered warning (Sol:
  a lifecycle-flooded tail window must not fabricate an all-clear,
  and an oversized torn record must not defeat the read bound).

- Unwatched-session visibility (the gap that let a live session sit
  two days over its trigger unmanaged): the overview flags a managed,
  provably live session at/over trigger with no attached watcher
  (`unwatched: true` in `overview --json`, a `(!)` warning line in
  the text readout), and the advisor injects a mid-flight attach
  warning into the session itself the first tool call after it
  crosses trigger unwatched — once per crossing, re-armed when a
  compaction drops it back below (`unwatched_warned` state key).
  The SessionStart lease check moved to `managed.lease_attached` so
  both hooks share one positive-proof predicate.

- Watcher: removed the time-based retirement deadline
  (`managed_deadline_hours` is gone from the config). A watcher now
  lives exactly as long as its session: exit, `/clear`/in-app
  `/resume` rotation, pane repurposing, and operator stop retire it;
  nothing else does. Previously it silently retired 24h after
  adoption, leaving long-lived sessions unmanaged with no notice
  (observed live: a session two days past its watcher's deadline
  sitting 8 points above its trigger). To keep "as long as its
  session" honest without a clock, a zombie/dead leader proc state
  (Z/X/x — exited claude, unreaped pid) is now positively recognized
  as claude_dead; a stopped one (T) is a paused session and keeps
  its watcher.
- Watcher: catch-up loop exits (stop, non-transient binding loss,
  heartbeat-ownership loss, scan error) now journal WATCHER_RETIRED
  like the tick loop's exits (Sol audit). All retirement exits route
  through one helper that journals a CLEANUP_REQUIRED typed-state
  hazard before the retirement record, closing a pre-existing gap
  where an operator stop or lease loss while an attempt sat
  SUBMITTED/ACKED between ticks would mask the unproven typed bytes
  behind a clean retirement in status (Sol audit HIGH) — and the
  retirement append is fenced on lease ownership under the txn lock
  so a stale loser can't supersede a successor's lifecycle in status
  (Sol audit MEDIUM).
- Overview: cleanly retired watchers now age out of the watchers list
  24h after their last journal write (same window the sessions list
  uses) instead of lingering until the 7-day TTL reap. Rows carrying
  hazard flags (DEAD-LEASE, ATTENTION, MALFORMED-LEASE) never age
  out of the display. Fixes stale multi-day RETIRED rows in the CLI
  overview and the ccam dashboard panel that relays it. A retired row
  whose leftover lease file is unreadable (status_rows drops
  unparseable leases, so it carries no flag) is surfaced as DEAD-LEASE
  and kept — the file still blocks adoption (Sol audit).
- Overview: per-session count of compactions the manager itself fired
  (`cm` column in the text table, `cm_compacts` in `overview --json`).
  Derived read-only from the watcher journal — attempts with own-nonce
  proof: ACKED, or the new `own_packet_proof` journal field stamped on
  a fast completion's BOUNDARY_CONFIRMED (packet and boundary inside
  one poll — Sol round-1 blocker) and on a retry's CLEANUP_REQUIRED
  whose late own packet proved the first submission fired (Sol
  round-2). Deduped by attempt_id so retries/recovery replays count
  once; deferred-foreign attempts resolved by native compactions do
  not count, and hostile non-string attempt ids neither crash nor
  count. `null`/blank = no retained watcher journal, distinct from a
  watched session at 0. The managed TTL reaper now defers a dead
  watcher's journal cleanup while the session's state file is inside
  the overview's 24h window, so a displayed count can't flip to null
  (Sol round-1) — and a session lease orphaned by pane reuse during
  that deferral stays reclaimable (zero-pane-match reap, Sol
  round-2). Round-3 hardening: a tokenless (malformed) lease is
  skipped entirely instead of deleting artifacts and KeyErroring out
  of acquisition; own proof survives a competing R6′ abort and is
  re-classified once at watcher recovery; journal writes persist
  `own_packet_proof` only for the exact True (no bool() laundering);
  a present-but-unreadable journal reports null, not 0 (null/blank =
  "count unavailable", covering absent AND unreadable journals); and
  the pruner sweeps lease-less journal/scan/request files past the
  TTL once their session ages off the overview (they previously
  accumulated forever), re-proving each file's identity (inode +
  mtime) at the last instant so a concurrent atomic replace can't
  lose a fresh file (Sol round-4).

- Turn-boundary lane: the watcher can now type a requested (or
  hard-threshold) `/compact` at a foreground turn boundary even while
  background tasks keep the pane busy — the failure that let a
  marathon session starve a written handoff for 28 minutes until
  native auto-compact fired stale (live case fbeb0bf1, 2026-08-17).
  Proof comes from a paired activity marker (UserPromptSubmit writes
  `running-<sid>.json`, Stop writes `ended-<sid>.json`, paired by the
  shared `prompt_id`; two lock-free atomic files, compared at watcher
  read time, so a stale Stop can never authorize a newer turn) plus a
  structured SGR-aware pane parse: bottom-anchored composer, dim
  ghost-suggestion text counts as empty, typed text and modals veto,
  unknown escapes veto. The strict whole-pane-idle lane keeps its
  typing contract for ordinary below-hard threshold attempts, with
  two deliberate changes: an affirmative "running" marker or a modal
  layout now vetoes it too (mid-generation panes can look
  byte-identical to idle), and its input-opportunity retries use the
  fast poll cadence instead of exponential backoff. Supporting changes: reason-aware defer scheduling (input
  opportunities retry at `managed_poll_s`; only structural failures
  back off exponentially — the old blanket 30→300s backoff was half
  the starvation), `trigger_source` persisted through the journal,
  dynamic promotion to the lane when usage crosses hard mid-retry, a
  pending attempt pins the outer loop to the fast poll cadence, a new
  ended marker makes a deferred attempt due immediately, and a
  non-latching STARVATION_ALERT (journal row + ATTENTION flag in
  status/overview + best-effort tmux display-message) after two
  minutes of blocked eligibility. Live-pinned UI facts (fixtures in
  tests/fixtures/): suggestions render ESC[2m-dim per word and are
  byte-identical to typed text once stripped; `esc to interrupt`
  flickers off mid-generation (the R2 veto alone was never a reliable
  turn discriminator); modal selection rows use "❯ "+ASCII-space.
  Post-commit audit round (fresh Sol + 2 Opus lanes) hardened the
  first cut: extended-color SGR params (38;5;n / 38;2;r;g;b) no
  longer misread as dim; the two-file marker read is linearized by a
  coherence re-read and is three-valued (an unpaired running marker
  is affirmative turn-in-flight evidence and vetoes both lanes);
  marker identity requires a matching transcript device/inode and
  ended >= running ordering; the pre-typing revalidation captures
  before its final marker read; SUBMITTED/ACKED attempts keep the
  fast poll cadence; the modal pattern requires indentation so a
  history echo of a prompt starting "1. " can't starve the watcher;
  starvation timing runs from first eligibility. Documented
  limitations: co-installed BLOCKING Stop hooks can fake a turn end
  (bounded: the typed /compact queues to the real boundary); modal
  coverage is the numbered-selection shape.
  Live-suite catches, fixed: an explicit model request now overrides
  a THRESHOLD latch (still_above_rearm_band) instead of starving until
  the re-arm band fills; the harness allows the Bash tool so the s14
  topology can actually run. (s16's queued-/compact pin
  stayed inconclusive in that run — the turn ended before injection —
  and remains an informational scenario.)
  Design: GPT-5.6 Sol rounds 1-4 (2026-08-18), two-file marker
  protocol green-lit round 3, final sign-off round 4; Opus + Sol
  audit rounds same date.

- Per-session threshold overrides, changeable mid-flight: a new
  `override` CLI subcommand writes a validated
  `<state_dir>/overrides/<sid>.json` (`trigger`/`soft`/`hard`/
  `window`, any subset, `--clear` resets, sid prefixes accepted). The
  advisor merges it into its effective thresholds (stamps stay BASE —
  pre-override — so nothing override-flavored lingers after
  `--clear`), a RUNNING watcher re-reads one consistent snapshot per
  decision so a change lands within one poll, and both readouts
  overlay the file over stamps and derivation — a change (or a clear)
  displays truthfully immediately.
  Values are range-validated (fractions in (0, 1], window 10k–1e9,
  soft kept ≤ hard) but not clamped beyond that: it is the human's
  lever, and the managed-mode
  start-of-session status line advertises the exact command (real
  session id baked in) for use when the user asks. New `set` slash
  command translates a spoken request into the CLI call. Override
  files age out with the session's other state (TTL reaper).

- Watchers retire on session-id rotation instead of babysitting the
  dead id: `/clear` and in-app `/resume` keep the claude process but
  rotate its session id, which previously left the watcher holding
  its lease against a frozen transcript until the 24h deadline while
  the live session went unwatched. `validate_binding` now retires
  with `reason=session_rotated` on positive proof only — a readable
  registry entry for the bound pid, same start time, carrying a
  different valid session id; missing/malformed registry entries
  never retire a healthy watcher. Retirement is journaled from every
  exit path (including the initial catch-up loop). SessionStart now
  also fires on `clear` (new hook matcher), so the fresh session id
  immediately hears that no watcher holds it, with the attach
  command. Pinned live by suite scenario s13 (/clear rotation →
  final journal record `WATCHER_RETIRED`/`session_rotated` + lease
  released); in-app `/resume` rotation shares the code path but has
  no live scenario yet.
- Operator stops journal `reason=stop_requested` (previously the
  retirement record inherited the incidental last-tick status, e.g.
  `below_threshold`), from both the tick loop and the catch-up loop.
- Session rows display the thresholds each session ACTUALLY runs
  with: the advisor stamps its effective (env-honoring,
  per-model-merged) window/soft/hard/trigger into the state file
  (`eff_*` fields, validated on load); the overview prefers stamps
  over re-deriving from its own config, which cannot see another
  session's env overrides. Text table gains a `trig` column; JSON
  rows gain `soft_pct`/`hard_pct`/`trigger_pct`. Unstamped or
  garbage stamps fall back to the readout's own derivation.

- Overview session rows carry a liveness verdict: `alive` column in
  the text readout (`live` / `GONE` / `?`), `session_live`
  (true/false/null) in `overview --json`. Proof-based (sessions
  registry entry + `/proc` pid + non-zombie state + start-time match,
  mirroring lease-reclaim's "declaring death needs proof"), and dead
  is only asserted from a COMPLETE registry scan — any unjudged entry
  (unreadable file, unreadable `/proc` stat, scan bound) degrades the
  absent sessions to unknown, never to dead; machines with no `/proc`
  or registry read as unknown throughout. Distinguishes "idle but
  running" from "exited, state lingering out the 24h window".

## 0.3.0 — 2026-08-16

Discoverability: managed mode's one invisible state — configured on,
hooks firing, but no watcher adopted for the session — is now surfaced
instead of discovered at the 80% line.

- `setup` slash command: guided first-run mode choice + window
  overrides + command introduction (install itself stays inert by
  design; merges into an existing config, never clobbers).
- Slash commands `attach` / `detach` / `status` (plugin namespace
  `/compact-manager:<name>`; `install.sh` installs substituted,
  marker-tagged copies beside the settings file as
  `/compact-manager-<name>` — plugin-context `${CLAUDE_PLUGIN_ROOT}`
  is never substituted for script installs, so symlinks would have
  resolved to `/bin/compact-manager`. Existing unmarked files are
  never overwritten; `uninstall.sh` removes only marker-carrying
  files or legacy symlinks resolving into this clone, canonicalizing
  via python3 rather than `readlink -f`).
- Second-round verification hardening: attachment proof rejects
  future heartbeats beyond 1s of slop (same-machine writes — further
  future is malformed, not skew); the MALFORMED-LEASE flag covers
  pid-malformed shapes on surfaced live rows (what it cannot see is
  documented); overview rows carry an `updated=…s-ago` age column
  with future mtimes surfaced as untrustworthy, detach's
  current-session heuristic primes its own state row first,
  age-verifies, prefers an environment-provided session id, and asks
  rather than guessing on any ambiguity; the CLI-discovery fallback
  finds the CLI file itself (versioned plugin-cache layouts have
  directories named compact-manager with no bin/). Token values
  outside [0, 1e11] render as zeros per-row.
- Default `context_window` is now 1,000,000 (the Claude 5 standard;
  was 200k). Deliberate failure direction: unknown new model names
  must never compact early at a stale small default — a legacy 200k
  model just misses advisories and falls back to native compaction.
  Override legacy models via `models`.
- `bin/compact-manager overview`: deterministic runtime readout
  (watcher rows with ATTENTION/DEAD-LEASE/MALFORMED-LEASE flags,
  per-session usage percentages, current-session marker). The status
  slash command relays it instead of re-deriving logic in prose, and
  all three command bodies locate the CLI defensively — placeholder
  substitution in command markdown is version/install-form-dependent.
- SessionStart wiring gains `startup` and `resume` matchers: in
  managed mode the hook injects one watcher-status line (attached with
  pid, or the attach command to run). Attachment needs positive proof
  — fresh lease heartbeat or verified-alive pid — never
  lease_is_live()'s ambiguity-counts-as-live reclaim default.
  Advisory/off stay silent; non-dict payloads and non-string session
  ids too. (This paragraph folds in a same-day cross-family review:
  2 user-file deletion paths, the plugin-variable break, the
  false-attached path, and fail-open gaps — all fixed pre-release.)

## 0.2.0 — 2026-08-15

Released after two two-family code-audit rounds (all findings folded)
and a 20/20 live regression run against the real CLI over tmux
(tools/live-managed/run.sh — gated, run manually).

Layer 2: `managed` mode. A per-session watcher daemon (started
explicitly with `bin/compact-manager start|adopt`; installing the
plugin alone still changes nothing) tails the session transcript with
its own durable cursor and types `/compact` into the session's tmux
pane at verified-idle moments — behind a six-rail ladder (identity
conjunction on the pane/tty/foreground-process walk, empty-composer
signature, stability window, type-without-Enter, exact composer
verify, full revalidation before Enter) that aborts on any doubt and
never clears the composer. Injection is confirmed end-to-end: a nonce
round-trips through the PreCompact wake packet and completion
requires the transcript's compaction boundary; a missing ack gets
exactly one retry, then a safety latch that never re-injects and
clears only via operator `resolve` or a real compaction. Ownership is
a session+pane lease pair under one flock'd transaction file with
10s heartbeats (two watchers can never share a pane; reclaim needs a
stale heartbeat AND a dead pid+starttime); every action is journaled
fsync-first, and crash recovery maps each journal tail to its
conservative state (typed-but-unconfirmed → SUBMISSION_UNCERTAIN /
CLEANUP_REQUIRED, resolved only by a human). Triggers: threshold
(`managed_trigger_pct`, default `hard_pct`) or a model-written
request file advertised by the advisory. `start` runs claude with no
shell under the pane; `adopt` targets an existing (even attended)
pane and requires `--attended` acknowledging the worst-case-race
residual. Linux/WSL only; advisory mode unaffected. New `managed_*`
config knobs, all clamped. Hooks, install scripts, and the plugin's
hook surface are unchanged.

## 0.1.1 — 2026-08-15

Post-release audit round (live M2 verification + two fresh-context
auditors, two model families). Fixes: a compaction boundary now resets
the measured `current` to `compactMetadata.postTokens` (the advisor
used to warn a freshly-compacted session that its context was still
full — caught live and by both auditors); downward hysteresis steps
hard→soft→none instead of emitting a spurious soft advisory on a
purely downward move; per-model overrides get the same sanity clamps
as globals; zero/garbage config values restore defaults instead of
misbehaving quietly; transcript scanning is byte-budgeted with a
giant-line escape; the handoff excerpt cap is a true byte cap and the
delivery cap tracks the knob; the state file is field-validated on
load (self-recovery from corruption); lock stale-break is
rename-atomic and release is owner-aware; `state_ttl_days` is now
real (daily best-effort reaper). New: manual `scripts/install.sh` /
`uninstall.sh` (boundary-safe, sibling-sparing, tested).

Re-verify round (two fresh auditors, two families) hardened the fixes
themselves: `discard_to_newline` persists across hook processes and
clears on file replacement; numeric state fields require exact
non-negative ints (a float offset used to wedge scanning for good);
the reaper only touches sentinel-marked dirs, skips symlinked
subdirs, and deletes only plugin-named regular files (lstat ages);
installer presence-detection is boundary-safe (a `.backup` lookalike
no longer blocks the real install); backups get collision-proof
names; the stdin cap is a true byte cap; the lock stale-break
re-checks what it displaced (restoring a displaced live lock via
O_EXCL, never clobbering a later acquirer; residual overlap after a
>10s-stale break is documented, worst case one lost advisory
update); huge-int timestamps no longer wedge state via
math.isfinite's OverflowError; the pruner only deletes stems
path_component could generate and skips junctions/symlinked files;
installer matching is boundary-safe on both sides. A third
two-family pass on the fix diff found no production defect. 66
tests.

## 0.1.0 — 2026-08-15

Layer 1 (advisory). Incremental own-transcript measurement with
inode/shrink reset guard; soft/hard advisories with hysteresis and
per-model windows; wake packet at PreCompact (mechanical facts +
model-written handoff excerpt, freshness-checked); reorientation
delivered at-most-once via PostToolUse/UserPromptSubmit with an
opportunistic SessionStart(compact) copy. Default mode `off` —
installing is inert. Grounded in live spikes S1-S5
(docs/spikes/compact-manager.md): SessionStart compact injection works
interactively; native auto-compact measured firing at ~1.09× the
nominal window; /compact custom instructions round-trip into the
PreCompact payload. Layer 2 (managed tmux injection) not built;
`managed` mode reserved.
