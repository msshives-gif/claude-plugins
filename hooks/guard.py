"""PreToolUse hook on SendMessage: warn (or ask) before the orchestrator
re-tasks a subagent whose recorded context is over the warn threshold.

This attacks the actual unsafe action — resuming an overloaded agent —
at the moment it happens, using the state observer.py recorded at the
agent's last stop. Empirically verified 2026-08-01: PreToolUse
hookSpecificOutput.additionalContext reaches the model alongside the
tool call.

The ladder: below warn_tokens (and uncompacted), silent; at/above
warn_tokens, an injected warning; at/above block_tokens (default 350k,
0 = off), permissionDecision "ask" — the send goes through only with
explicit approval, so the block is overridable rather than absolute.
"""
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sgauge_common as sg
except Exception as e:
    print(f"subagent-gauge guard: import failed: {e!r}", file=sys.stderr)
    sys.exit(0)


def find_state(states, target):
    # states arrive newest-first, so a reused name resolves to the most
    # recent agent that carried it (matching the harness's latest-wins
    # naming rule).
    for rec in states:
        if target in (rec.get("name"), rec.get("agent_id")):
            return rec
    return None


def main():
    payload = json.loads(sys.stdin.read())
    if payload.get("tool_name") != "SendMessage":
        return
    # Only guard the parent session's sends, not subagent-to-subagent.
    if sg.is_subagent_payload(payload):
        return
    target = (payload.get("tool_input") or {}).get("to") or ""
    if not target or target == "main":
        return
    cfg = sg.load_config()
    session_id = payload.get("session_id") or ""
    rec = find_state(sg.load_agent_states(cfg, session_id), target)
    if not rec:
        return
    current = rec.get("current", 0)
    over = current >= cfg["warn_tokens"] or rec.get("compactions", 0)
    if not over:
        return
    warn = (f"[subagent-gauge] You are messaging agent '{target}' whose "
            f"context was ~{current / 1000:.0f}k tokens at its last stop")
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
    if cfg["block_tokens"] and current >= cfg["block_tokens"]:
        out["hookSpecificOutput"]["permissionDecision"] = "ask"
        out["hookSpecificOutput"]["permissionDecisionReason"] = warn
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"subagent-gauge guard: {e!r}", file=sys.stderr)
    sys.exit(0)
