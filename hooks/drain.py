"""PostToolUse hook (parent session): inject queued subagent context
reports into the orchestrator's context via additionalContext.

Empirically verified 2026-08-01: PostToolUse hookSpecificOutput
.additionalContext reaches the session's model as a system-reminder.
This is the delivery channel — zero extra model turns, works for
background agents and for agents without messaging tools, and never
touches the subagent's final deliverable.

Guard against draining from INSIDE a subagent: if the payload names an
agent transcript or carries an agent_id, this tool call belongs to a
subagent, whose injections would leak the parent's reports into the
wrong context. Exit silently there.
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
    if sg.is_subagent_payload(payload):
        return
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    cfg = sg.load_config()
    texts = sg.drain_queue(cfg, session_id, cfg["drain_batch_max"])
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
