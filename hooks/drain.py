"""PostToolUse hook: deliver queued context reports by injecting them
into the consumer's context (additionalContext).

Runs on every tool call, so it exits as fast as possible when there is
nothing to deliver. The payload's agent_id routes delivery: absent means
the root session (drain the session queue), present means that subagent
is itself an orchestrator (drain only the queue addressed to it, into
its own context — verified to reach the subagent's model, see
docs/DESIGN.md). Either way, reports can never leak into the wrong
context: each consumer has its own queue file.
"""
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sgauge_common as sg
except Exception as e:
    print(f"subagent-gauge drain: import failed: {e!r}", file=sys.stderr)
    sys.exit(0)


def main():
    payload = json.loads(sys.stdin.read())
    # Subagent payloads carry the ROOT session_id (verified, fixtures).
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    agent_id = payload.get("agent_id") or ""
    if not agent_id and sg.is_subagent_payload(payload):
        return  # subagent context without an id: no safe routing
    cfg = sg.load_config()
    texts = sg.drain_queue(cfg, session_id, cfg["drain_batch_max"],
                           consumer_agent=agent_id or None)
    if not texts:
        return
    print(json.dumps({
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(texts),
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"subagent-gauge drain: {e!r}", file=sys.stderr)
    sys.exit(0)
