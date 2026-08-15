"""SessionStart (matcher "compact"): opportunistic reorientation right
after a compaction. Spike S1: this injection DOES reach the model in
interactive sessions on current Claude Code (not across headless
resumes). It never advances the drain CAS — the durable delivery stays
with advisor/reorient, so if this fires the model may see the
reorientation twice (harmless duplicate by design)."""
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
except Exception as e:
    print(f"compact-manager session_start: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def main():
    payload = json.loads(sys.stdin.read(1_000_000))
    cfg = cm.load_config()
    if cfg["mode"] == "off":
        return
    if (payload.get("source") or payload.get("session_started_from")) \
            != "compact":
        return
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    paths = cm.state_paths(cfg, session_id)
    packet = cm.load_packet(paths)
    if not packet:
        return
    cm.ledger_append(cfg, {"event": "inject", "hook": "session_start",
                           "seq": packet.get("seq")})
    print(json.dumps({
        "suppressOutput": not cfg["system_message"],
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": cm.reorientation_text(packet),
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager session_start: {e!r}", file=sys.stderr)
    sys.exit(0)
