"""PreToolUse hook on SendMessage: catch the moment before the
orchestrator gives more work to an already-full agent.

The target's transcript is re-scanned live at decision time (one
bounded parse), so the judgment reflects the agent's current size, not
its last stop. Below warn_tokens: silent. At warn_tokens: a warning is
injected next to the tool call. At block_tokens (0 = off): the send
also needs explicit confirmation — overridable, not absolute. A
compacted target escalates per the compaction_action knob (off | warn |
block, default block). Thresholds honor per-model "models" overrides
for the target agent's recorded model. Rationale: docs/DESIGN.md.
"""
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import subagent_context as sg
except Exception as e:
    print(f"subagent-context guard: import failed: {e!r}", file=sys.stderr)
    sys.exit(0)


def find_state(states, target, sender):
    # states arrive newest-first, so a reused name resolves to the most
    # recent agent that carried it (matching the harness's latest-wins
    # naming rule). Only agents the SENDER spawned are candidates —
    # sender "" (root) matches root-owned records; an unknown parent
    # ("?") matches nobody, so it can never be misattributed. Records
    # from before the field existed count as root-owned only when their
    # recorded depth says shallow.
    for rec in states:
        if target not in (rec.get("name"), rec.get("agent_id")):
            continue
        sd = rec.get("spawn_depth", 0)
        default = "" if isinstance(sd, int) and not isinstance(sd, bool) \
            and sd < 2 else "?"
        if rec.get("parent_agent_id", default) == sender:
            return rec
    return None


# Fresh reads larger than this fall back to the stored numbers: _scan
# has no internal deadline, and a pathological transcript must not eat
# the 5s PreToolUse hook budget. ~50MB parses in well under a second.
FRESH_READ_MAX_BYTES = 50_000_000


def live_reading(rec):
    """Strengthen the stored reading with a live re-scan of the target's
    transcript, so the guard judges the agent on what it is NOW rather
    than what it was at its last stop.

    Merge rule: the transcript is append-only, so a fresh full scan
    whose peak contains the stored reading (fresh peak >= stored
    current) has seen every row the observer saw — it is simply
    authoritative, including a LOWER post-compaction current. (The
    stored record can pair a pre-compaction current with the new
    compaction count when the summary row flushed before the
    post-compaction terminal row; a containment-proven fresh scan is
    the only stale-proof answer.) If containment fails, the file was
    truncated or replaced — never weaken on partial data: take the max.
    compactions is a monotonic max either way.

    Falls back to the stored numbers on any error (no transcript field,
    unreadable/oversized file) — the guard stays fail-open and inside
    its 5s budget: grace_ms=0 means exactly one bounded parse.

    Returns (current, compactions, live) where live says whether the
    presented number reflects the fresh scan (for honest warn wording).
    """
    current = rec.get("current", 0)
    compactions = rec.get("compactions", 0)
    tp = rec.get("transcript")
    fresh = None
    if tp:
        try:
            if os.path.getsize(tp) <= FRESH_READ_MAX_BYTES:
                fresh = sg.measure(tp, grace_ms=0)
        except Exception:
            fresh = None
    if not fresh:
        return current, compactions, False
    compactions = max(compactions, fresh["compactions"])
    if not fresh.get("terminal"):
        # Preliminary rows only: not a completed measurement. Merge the
        # facts (never weaken) but never claim liveness from it.
        return max(current, fresh["current"]), compactions, False
    if fresh["peak"] >= current:
        return fresh["current"], compactions, True
    # Containment failed: the file was truncated/replaced. peak >=
    # fresh current always, so the stored number wins here — present it
    # without a liveness claim.
    return max(current, fresh["current"]), compactions, False


def main():
    payload = json.loads(sys.stdin.read())
    if payload.get("tool_name") != "SendMessage":
        return
    target = (payload.get("tool_input") or {}).get("to") or ""
    # "main" is the orchestrator's own address, not a subagent.
    if not target or target == "main":
        return
    # Subagent payloads carry the root session_id; the sender's own
    # agent_id ("" for the root session) scopes the lookup.
    sender = payload.get("agent_id") or ""
    if not sender and sg.is_subagent_payload(payload):
        return  # subagent context without an id: no safe attribution
    cfg = sg.load_config()
    session_id = payload.get("session_id") or ""
    rec = find_state(sg.load_agent_states(cfg, session_id), target, sender)
    if not rec:
        return
    th = sg.thresholds(cfg, rec.get("model"))
    current, compactions, live = live_reading(rec)
    compaction_signal = compactions and th["compaction_action"] != "off"
    over = current >= th["warn_tokens"] or compaction_signal
    if not over:
        return
    size_txt = (f"context is ~{current / 1000:.0f}k tokens" if live else
                f"context was ~{current / 1000:.0f}k tokens at its last stop")
    warn = (f"[subagent-context] You are messaging agent '{target}' whose "
            + size_txt)
    if compactions:
        warn += f" (compacted x{compactions})"
    warn += (". Long-context agents degrade and anchor on their priors. "
             "For a cheap, targeted follow-up this is fine; for a new task "
             "or a fresh review round, spawn a new agent instead.")
    out = {
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": warn,
        },
    }
    # "ask" only for the root session: a subagent may have no one to
    # answer the confirmation, and an unanswerable ask is a block —
    # stronger than this tool's fail-open promise allows.
    size_block = th["block_tokens"] and current >= th["block_tokens"]
    compaction_block = th["compaction_action"] == "block" and compactions
    if not sender and (size_block or compaction_block):
        out["hookSpecificOutput"]["permissionDecision"] = "ask"
        out["hookSpecificOutput"]["permissionDecisionReason"] = warn
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"subagent-context guard: {e!r}", file=sys.stderr)
    sys.exit(0)
