#!/bin/bash
# Manual (non-plugin) install: merge the cross-session-send-guard hook
# into ~/.claude/settings.json (or a settings file given as $1).
# Idempotent; atomic write; backs up first.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${1:-$HOME/.claude/settings.json}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (the hook is Python)" >&2
    exit 1
fi

python3 - "$SETTINGS" "$PLUGIN_DIR" <<'PY'
import json, os, shlex, shutil, sys, tempfile, time

settings_path, plugin = sys.argv[1], sys.argv[2]
script_path = os.path.join(plugin, "hooks", "peer_send_guard.py")
q = shlex.quote(script_path)
cmd = f"python3 {q} || python {q} || true"

settings = {}
if os.path.isfile(settings_path):
    backup = (f"{settings_path}.bak-cross-session-send-guard-"
              f"{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(settings_path, backup)
    print(f"backup: {backup}")
    try:
        with open(settings_path) as fh:
            settings = json.load(fh)
    except json.JSONDecodeError as e:
        sys.exit(f"{settings_path} is not valid JSON ({e}); fix it first "
                 f"(backup already taken)")

hooks = settings.setdefault("hooks", {})
groups = hooks.setdefault("PreToolUse", [])
if any(script_path in json.dumps(g, ensure_ascii=False) for g in groups):
    print("already installed, skipping")
else:
    groups.append({"matcher": "SendMessage",
                   "hooks": [{"type": "command", "command": cmd,
                              "timeout": 5}]})
    print("installed")

settings_dir = os.path.dirname(settings_path) or "."
os.makedirs(settings_dir, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=settings_dir,
                           prefix=".cross-session-send-guard-")
with os.fdopen(fd, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings_path)
print(f"wrote {settings_path}")
print("Restart running Claude Code sessions to pick up hook changes.")
PY
