"""SubagentStop hook: measure the stopping agent's context, record it,
and queue a report for the orchestrator (drain.py delivers it).

We only measure the transcript the payload names — guessing charges one
agent's tokens to another. Why this hook never continues the stopping
agent to deliver the report: docs/DESIGN.md, "The delivery problem".
"""
import json
import os
import sys
import time

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
    parent = sg.resolve_parent(meta)
    wf_run = sg.workflow_run(atp)
    if wf_run:
        # Workflow sidecars carry no name; label by run + short id.
        name = f"{wf_run}/{meta.get('name') or agent_id[:10]}"

    record = {
        "agent_id": agent_id,
        "name": name,
        "model": model,
        "spawn_depth": meta.get("spawnDepth", 0),
        "parent_agent_id": parent,
        "workflow_run": wf_run,
        "current": res["current"],
        "prompt": res["prompt"],
        "peak": res["peak"],
        "compactions": res["compactions"],
        "terminal_row": res["terminal"],
        "stale": bool(res.get("stale")),
        "observed_at": time.time(),
        "transcript": atp,
    }
    # Each step is guarded on its own: state, then delivery, then the
    # audit trail — no step's failure may cost a later one.
    try:
        sg.write_agent_state(cfg, session_id, record)
    except Exception as e:
        print(f"subagent-gauge observer: state write failed: {e!r}",
              file=sys.stderr)

    out = {"suppressOutput": True}
    delivered_to = "none"
    # Compaction is itself an overload signal — it must not be filtered
    # out just because the post-compaction context is small.
    if res["current"] >= cfg["report_min_tokens"] or res["compactions"]:
        report = sg.fmt_report(name, model, res, cfg["warn_tokens"])
        if agent_id and agent_id not in report:
            report += f" (id {sg.sanitize(agent_id, 40)})"
        # Reports about a nested agent go to its spawner; unknown or
        # root-owned parents go to the root session. resolve_parent
        # already validated the id, so no fallback path is needed here.
        consumer = parent if parent not in ("", "?") else None
        try:
            sg.enqueue(cfg, session_id, report, consumer_agent=consumer)
            delivered_to = consumer or "session"
        except Exception as e:
            print(f"subagent-gauge observer: enqueue failed: {e!r}",
                  file=sys.stderr)
        if cfg["system_message"]:
            out["systemMessage"] = report
    try:
        sg.ledger_append(cfg, {"event": "SubagentStop",
                               "session": session_id,
                               "delivered_to": delivered_to,
                               **{k: record[k] for k in
                                  ("agent_id", "name", "model", "current",
                                   "peak", "compactions",
                                   "parent_agent_id", "workflow_run")}})
    except Exception as e:
        print(f"subagent-gauge observer: ledger failed: {e!r}",
              file=sys.stderr)
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail open, loudly on stderr
        print(f"subagent-gauge observer: {e!r}", file=sys.stderr)
    sys.exit(0)
