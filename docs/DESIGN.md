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
measurements to a per-parent-session queue file on disk; the drain
(PostToolUse, runs for the parent) injects and empties the queue on the
parent's next tool call. An orchestrator managing agents makes tool
calls constantly, so delivery latency is effectively one tool call.
The guard (PreToolUse on SendMessage) reads the same state at the one
moment that matters — when the orchestrator is about to re-task an
agent — closing the loop even if the earlier report scrolled out of
attention.

Deliberate consequences:

- Zero additional model turns anywhere.
- The subagent's final message is untouched.
- Works for background agents, foreground agents, teammates, and agents
  with no messaging tools.
- A subagent's own PostToolUse events must NOT drain the parent's queue;
  the drain exits if the payload carries `agent_id` /
  `agent_transcript_path` or a subagent transcript path.

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
satisfies it immediately. The observer instead waits until the file size
is stable across two 250ms polls (bounded by `flush_grace_ms`), then
parses.

**Peak and compaction:** after auto-compaction the current context
shrinks, so a threshold on `current` alone can be defeated. The scan
tracks `peak` (max terminal-row sum) and counts compaction summary rows;
the guard and report treat any compaction as an overload signal.

**Identity:** from the transcript's `meta.json` sidecar (`name`,
`agentType`, `model`, `spawnDepth`), falling back to the payload
`agent_id`. Filename parsing is not used. Exact attribution only: if the
payload doesn't name a real `agent_transcript_path`, the observer logs
`no-exact-transcript` and records nothing — a guessed transcript
attributes another agent's tokens to this one (observed in v1
production; strictly worse than no number).

## Rejected alternatives

- **Relay through the stopping agent** (v1) — see above.
- **Parent-side PostToolUse on the Agent tool result** — works only for
  foreground spawns; background spawns return `async_launched` before
  any usage exists (upstream #5812 territory).
- **Statusline / dashboards** — inform the human, not the orchestrator;
  the decision-maker here is the model.
- **OTEL metrics** — no per-subagent context metric exists, and the
  orchestrator can't read OTEL mid-session anyway.

## Compatibility posture

The transcript JSONL, `meta.json`, and the `subagents/` layout are
undocumented internals. Everything parses defensively (per-line, typed
checks) and every entry point fails open (`exit 0` always; errors to
stderr, observations to the ledger). A format change should degrade to
"no reports", never to a broken session. Fixtures in `tests/` pin the
formats this was built against (Claude Code, 2026-08-01).

## Prior art

Nothing found (2026-08-01 survey) that closes the loop from "subagent X
stopped at N tokens" to "the orchestrator knows": session-optimizer's
context-guard budgets the *main* session; claudewatch gives an agent
self-awareness of its *own* context; claude-cli-monitor computes
per-subagent tokens but renders them to a human statusline; the
observability dashboards (disler et al.) are human-facing. Upstream
feature requests for native support were closed not-planned (#5812,
#22625).
