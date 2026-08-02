"""PostToolUse hook (parent session): deliver queued context reports by
injecting them into the orchestrator's context (additionalContext).

Runs on every tool call, so it exits as fast as possible when there is
nothing to deliver — and never runs for tool calls made inside a
subagent, which would leak the parent's reports into the wrong context.
Channel rationale and verification: docs/DESIGN.md.
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
