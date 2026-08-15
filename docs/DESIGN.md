# Design

## The delivery problem

There is no hook channel that pushes information from a stopping
subagent to the parent session's model:

- `SubagentStop`'s `hookSpecificOutput.additionalContext` continues the
  **stopping subagent**, not the parent (verified 2026-07-31 with
  dual-trace probes; docs prose is ambiguous on this).
- `systemMessage` from any hook is user-display only; it never reaches
  any model.
- `TeammateIdle` has no injection channel and no transcript path.

A v1 of this tool (private, 2026-07-31) used the SubagentStop
continuation as a relay: continue the dying agent once with "SendMessage
your context report to the dispatcher, then stop." It worked, but
production use surfaced three costs:

1. **Money/latency:** the relay turn re-reads the agent's full context.
   On a ~400k-token Opus agent that is ~$0.60 per relay (cache-read
   pricing), several times per evening.
2. **Deliverable pollution:** the relay acknowledgment becomes the
   agent's final message, which is what task notifications surface —
   dispatchers see "context report sent" instead of the agent's actual
   report (observed repeatedly in production, 2026-08-01).
3. **No-messaging agents:** agent types without SendMessage can't relay
   at all, and worse, obey the "then stop" fallback in confusing ways.

## The v2 answer: queue + parent-side injection

Two channels that DO reach the parent model, both empirically verified
2026-08-01 with headless test sessions:

- `PostToolUse` `hookSpecificOutput.additionalContext` → injected into
  the session's context as a system-reminder after the tool result.
- `PreToolUse` `hookSpecificOutput.additionalContext` → injected
  alongside the tool call.

So: the observer (SubagentStop, runs for the subagent) writes
measurements to a per-consumer queue file on disk; the drain
(PostToolUse, runs for the parent) injects and empties the queue on the
parent's next tool call. An orchestrator managing agents makes tool
calls constantly, so delivery latency is effectively one tool call.
The guard (PreToolUse on SendMessage) reads the same state at the one
moment that matters — right before the orchestrator gives more work to
an agent — closing the loop even if the earlier report scrolled out of
attention. The queue keying relies on the SubagentStop payload's
`session_id` being the parent session's id; verified against live
payloads and end-to-end tests, 2026-08-01.

Deliberate consequences:

- Zero additional model turns anywhere.
- The subagent's final message is untouched.
- Works for background agents, foreground agents, teammates, and agents
  with no messaging tools.
- Each consumer has its own queue file, so a report can never land in
  the wrong context. The payload's `agent_id` picks the queue: absent →
  the root session's, present → the one addressed to that subagent
  (see "Nested and workflow agents").
- Delivery is best-effort: a drained batch that never gets injected
  (process killed at the wrong moment) is not redelivered. The
  per-agent state files, which the guard reads, are the durable record.
- Overhead is one interpreter spawn (~35ms measured, no model tokens)
  per parent tool call, plus one bounded observer run per subagent stop.

### The queue file is where trust ends

Whatever sits in the queue file gets read into the orchestrator's
context, and any program running as the user could write to that file.
So the drain cleans every line again on the way out: control characters
and newlines are flattened and the line is capped in length, which
means a planted line can't forge extra report lines or slip in escape
sequences. State directories are created `0o700` and files `0o600`,
and the agent-supplied name and model are cleaned where they enter,
too.

What this does not stop: a program running as the user could plant one
plausible-looking report line. That's no worse than what the same
program could already do to the transcript or the settings file, so we
document it rather than defend against it.

## Measurement

Context size = `input_tokens + cache_read_input_tokens +
cache_creation_input_tokens + output_tokens` of the last **terminal**
usage row (`stop_reason` non-null). Streaming writes preliminary usage
rows (`stop_reason: null`, `output_tokens: 1`) before the terminal row
of the same request; taking "last usage row seen" without that filter
can capture an incomplete record. `output_tokens` is included because
the final response is part of the live conversation an agent would
resume with. `prompt` (the sum minus output) is stored too.

**Flush race:** at SubagentStop time the transcript often lacks its
final row (verified 2026-07-31). Waiting only "until any usage row
exists" is wrong for reused agents — a stale row from the previous burst
satisfies it immediately — and "the file went quiet" is wrong when the
flush simply hasn't started. Transcript rows carry timestamps, so the
observer polls (a full parse is ~0.07s for a 5MB transcript) until the
newest terminal row is recent (< 30s old), bounded by `flush_grace_ms`.
If the deadline passes first, the best available reading is still
recorded but flagged, and the report carries "[reading may lag]" instead
of presenting a stale number as current.

**Peak and compaction:** after auto-compaction the current context
shrinks, so a threshold on `current` alone can be defeated. The scan
tracks `peak` (max terminal-row sum) and counts compaction summary rows;
the guard and report escalate on compaction per the `compaction_action`
knob (default `block`: re-tasking a compacted agent needs confirmation).

**Guard fresh-read (0.4.0):** stored records are measured at the
agent's last stop, so a re-tasked agent working for half an hour was
judged on a long-stale number. The guard now re-scans the target's
transcript at decision time (`measure(transcript, grace_ms=0)` — one
bounded parse, no flush wait, capped at `FRESH_READ_MAX_BYTES` so a
pathological file can't eat the 5s hook budget). Merge rules: the
compaction count is a monotonic max; `current` takes the fresh value
when the fresh scan saw a NEW compaction (the context genuinely reset —
keeping the stale pre-compaction size would defeat
`compaction_action: "off"`), otherwise the max. Any failure falls back
to the stored numbers; the warn text says which reading it presents
("is ~Nk" live vs "was ~Nk at its last stop").

Two sub-choices deliberately not built: **no write-back** of the fresh
reading to the state record (it would race the observer's lockless
atomic write and corrupt the record's measured-at-last-stop semantics
that `status.py` presents; the re-scan is cheap enough to re-derive per
guard) and **no peak merge** (the guard never consults peak).

**Identity:** from the transcript's `meta.json` sidecar (`name`,
`agentType`, `model`, `spawnDepth`), falling back to the payload
`agent_id`. Filename parsing is not used. We only measure the
transcript the payload names; if it doesn't name a real one, the
observer records nothing and logs `no-exact-transcript`. Guessing means
charging one agent's tokens to another, which is worse than having no
number at all — v1 guessed by file mtime, and we watched it
misattribute.

**Why the ledger exists:** it is the only place the failure modes of
parsing an undocumented format show up — `no-exact-transcript`,
`unmeasurable`, and stale readings are recorded there even when no
report is produced. Without it, a Claude Code format change would look
identical to "no subagents ran".

## Nested and workflow agents

Subagents can spawn subagents (three layers below the main conversation
by default; `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`), and the Workflow
tool fans out its own agents. Both fire SubagentStop with an exact
`agent_transcript_path` and the ROOT session's `session_id`; tool
events inside a subagent fire with that subagent's `agent_id` and the
root `session_id` too. All verified 2026-08-02 with captured payloads —
pinned in `tests/fixtures/`.

Routing is by consumer, resolved from the stopping agent's sidecar:

- `parentAgentId` present → the report is for that spawner. It goes to
  `queue-agents/<session>/<parent>.jsonl`, and the drain delivers it on
  the spawner's next tool call, into the spawner's own context
  (injection into a subagent's context verified by echo test).
- No `parentAgentId`, verified depth 0 or 1 → treated as root-owned
  (root session queue, as before). This branch is a FALLBACK for
  sidecars written before `parentAgentId` existed (first seen
  2026-07-16): current-format sidecars name their spawner whenever one
  exists, at any depth — teammates are depth 0, so a teammate's child
  is depth 1. When the fallback fires, root ownership is inferred, not
  proven; a pre-2026-07-16 teammate-child sidecar would be attributed
  to root.
- Anything else (no usable `parentAgentId`, no verified shallow depth)
  → ownership is UNKNOWN, written as `"?"`. Delivery degrades to the
  root queue, but the record can never be owner-matched — a root-owned
  lookup must not silently match somebody else's child.

Workflow agents get their run id (from the transcript path) as the
report label, since their sidecars carry no name. The Workflow
tool exists only in the root session (it is stripped from every
subagent), so workflow reports always route to root.

The guard follows the same ownership: a send's candidate states are
those whose `parent_agent_id` equals the sender's `agent_id` ("" for
the root session), so with current-format sidecars a reused name can't
warn about another branch's agent (pre-2026-07-16 sidecars: see the
fallback caveat above). Inside a subagent the guard only warns — `permissionDecision:
"ask"` stays root-only, because a subagent may have nobody to answer
the confirmation, and an unanswerable ask is a block.

Deliberately not built, until real use demands it: report rollups for
large workflow fan-outs (the drain batch cap already bounds one
injection; typical workflows are ≤15 agents), dedup of per-burst
teammate stops, and any persistent parent graph — ownership lives on
each state record and nowhere else. Known consequence of ownership
scoping: a teammate messaging a SIBLING teammate is not guarded
(the sender doesn't own the target; root does).

## Rejected alternatives

- **Relay through the stopping agent** (v1) — see above.
- **Parent-side PostToolUse on the Agent tool result** — works only for
  foreground spawns; background spawns return `async_launched` before
  any usage exists (which is what #5812 asked for).
- **Statusline / dashboards** — inform the human, not the orchestrator;
  the decision-maker here is the model.
- **OTEL metrics** — no per-subagent context metric exists, and the
  orchestrator can't read OTEL mid-session anyway.

## Running on other platforms

File locking uses a sidecar lock file (created `O_CREAT|O_EXCL`, with
an ownership token) rather than `flock`/`fcntl`: one mechanism that
behaves the same on Linux, macOS, and Windows, with bounded waits and
stale-lock breaking. The catch is that it only works while every
program agrees to use it — the kernel isn't enforcing anything — and if
a hook is killed while holding the lock, other hooks wait out their
bounded timeout and skip work until the 10-second stale threshold
breaks the dead lock. The token stops a lock-breaker from later
deleting a lock that a newer holder owns. The queue's drain rewrites in
place (truncate) rather than swapping a temp file in, because renaming
over an open file fails on Windows.

Hook commands try `python3` then `python`, covering Windows installs
where Python has no `python3` name.

## When Claude Code's internals change

The transcript JSONL, `meta.json`, and the `subagents/` layout are
undocumented internals. Everything parses defensively (per-line, typed
checks) and every entry point fails open (`exit 0` always; errors to
stderr, observations to the ledger). A format change should degrade to
"no reports", never to a broken session. Fixtures in `tests/` pin the
formats this was built against (Claude Code, 2026-08-02).

## Prior art

Nothing found (2026-08-01 survey) that closes the loop from "subagent X
stopped at N tokens" to "the orchestrator knows": session-optimizer's
context-guard budgets the *main* session; claudewatch gives an agent
self-awareness of its *own* context; claude-cli-monitor computes
per-subagent tokens but renders them to a human statusline; the
observability dashboards (disler et al.) are human-facing. Upstream
feature requests for native support were closed not-planned (#5812,
#22625).
