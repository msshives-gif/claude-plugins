"""UserPromptSubmit: second measurement + drain point. Tool-free
conversation can cross thresholds with zero tool calls (audit finding),
and the first post-compact interaction can be a user prompt — so this
hook runs the same locked decision as the advisor."""
import json
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
    import advisor
except Exception as e:
    print(f"compact-manager reorient: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def main():
    payload = json.loads(sys.stdin.read(1_000_000))
    cfg = cm.load_config()
    if cfg["mode"] == "off":
        return
    text = advisor.run(payload, cfg)
    if text:
        print(json.dumps({
            "suppressOutput": not cfg["system_message"],
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": text,
            },
        }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager reorient: {e!r}", file=sys.stderr)
    sys.exit(0)
