"""SubagentStop hook: measure the stopping agent's context, record it,
and queue a report for the orchestrator.

Exact attribution only: we measure ONLY the transcript the payload names
(agent_transcript_path). No mtime-newest fallback — a guessed transcript
attributes another agent's tokens to this one, which is worse than no
number (verified in production, 2026-07-31..08-01).

This hook never uses additionalContext: on SubagentStop that channel
continues the STOPPING agent (not the parent), which costs a full extra
model turn on a possibly-huge context and overwrites the agent's final
deliverable with the relay acknowledgment. Delivery to the orchestrator
happens via drain.py instead.
"""
import json
import os
import sys

# Even the import must fail open: a non-zero exit surfaces a hook error
# line in the user's session (e.g. fcntl is Unix-only).
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sgauge_common as sg
except Exception as e:
    print(f"subagent-gauge observer: import failed: {e!r}", file=sys.stderr)
    sys.exit(0)


def main():
    payload = json.loads(sys.stdin.read())
    if payload.get("hook_event_name") != "SubagentStop":
        return
    cfg = sg.load_config()
    sg.prune_stale(cfg)
    session_id = payload.get("session_id") or ""
    agent_id = payload.get("agent_id") or ""

    atp = payload.get("agent_transcript_path") or ""
    if "subagents" not in atp or not os.path.isfile(atp):
        sg.ledger_append(cfg, {"event": "SubagentStop", "session": session_id,
                               "agent_id": agent_id, "result": "no-exact-transcript"})
        return

    res = sg.measure(atp, cfg["flush_grace_ms"])
    if res is None:
        sg.ledger_append(cfg, {"event": "SubagentStop", "session": session_id,
                               "agent_id": agent_id, "result": "unmeasurable"})
        return

    meta = sg.read_meta(atp)
    # Sidecar first (has teammate names); documented payload agent_type
    # next, so a sidecar format change degrades to a readable name.
    name = (meta.get("name") or meta.get("agentType")
            or payload.get("agent_type") or agent_id or "subagent")
    model = meta.get("model") or ""

    import time
    record = {
        "agent_id": agent_id,
        "name": name,
        "model": model,
        "spawn_depth": meta.get("spawnDepth", 0),
        "current": res["current"],
        "prompt": res["prompt"],
        "peak": res["peak"],
        "compactions": res["compactions"],
        "terminal_row": res["terminal"],
        "stale": bool(res.get("stale")),
        "observed_at": time.time(),
        "transcript": atp,
    }
    sg.write_agent_state(cfg, session_id, record)
    sg.ledger_append(cfg, {"event": "SubagentStop", "session": session_id,
                           **{k: record[k] for k in
                              ("agent_id", "name", "model", "current",
                               "peak", "compactions")}})

    out = {"suppressOutput": True}
    # Compaction is itself an overload signal — it must not be filtered
    # out just because the post-compaction context is small.
    if res["current"] >= cfg["report_min_tokens"] or res["compactions"]:
        report = sg.fmt_report(name, model, res, cfg["warn_tokens"])
        if agent_id and agent_id not in report:
            report += f" (id {agent_id})"
        sg.enqueue(cfg, session_id, report)
        if cfg["system_message"]:
            out["systemMessage"] = report
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail open, loudly on stderr
        print(f"subagent-gauge observer: {e!r}", file=sys.stderr)
    sys.exit(0)
