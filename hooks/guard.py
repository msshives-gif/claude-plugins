"""PreToolUse hook on SendMessage: warn (or ask) before the orchestrator
re-tasks a subagent whose recorded context is over the warn threshold.

This attacks the actual unsafe action — resuming an overloaded agent —
at the moment it happens, using the state observer.py recorded at the
agent's last stop. Empirically verified 2026-08-01: PreToolUse
hookSpecificOutput.additionalContext reaches the model alongside the
tool call.

Default is a soft warning (permission untouched). With hard_block=true
the tool call needs explicit user approval (permissionDecision "ask").
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sgauge_common as sg


def find_state(states, target):
    for rec in states:
        if target in (rec.get("name"), rec.get("agent_id")):
            return rec
    return None


def main():
    payload = json.loads(sys.stdin.read())
    if payload.get("tool_name") != "SendMessage":
        return
    # Only guard the parent session's sends, not subagent-to-subagent.
    if payload.get("agent_id") or payload.get("agent_transcript_path"):
        return
    target = (payload.get("tool_input") or {}).get("to") or ""
    if not target or target == "main":
        return
    cfg = sg.load_config()
    session_id = payload.get("session_id") or ""
    rec = find_state(sg.load_agent_states(cfg, session_id), target)
    if not rec:
        return
    over = rec.get("current", 0) >= cfg["warn_tokens"] or rec.get("compactions", 0)
    if not over:
        return
    warn = (f"[subagent-gauge] You are messaging agent '{target}' whose "
            f"context was ~{rec.get('current', 0) / 1000:.0f}k tokens at its "
            f"last stop")
    if rec.get("compactions"):
        warn += f" (compacted x{rec['compactions']})"
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
    if cfg["hard_block"]:
        out["hookSpecificOutput"]["permissionDecision"] = "ask"
        out["hookSpecificOutput"]["permissionDecisionReason"] = warn
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"subagent-gauge guard: {e!r}", file=sys.stderr)
    sys.exit(0)
