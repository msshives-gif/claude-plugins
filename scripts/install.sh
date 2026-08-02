#!/bin/bash
# Manual (non-plugin) install: merge subagent-gauge hooks into
# ~/.claude/settings.json (or a settings file given as $1), backing the
# file up first. Idempotent: running twice adds nothing twice.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${1:-$HOME/.claude/settings.json}"

python3 - "$SETTINGS" "$REPO_DIR" <<'PY'
import json, os, shutil, sys, time

settings_path, repo = sys.argv[1], sys.argv[2]
entries = {
    "SubagentStop": (None, "observer.py", 10),
    "PostToolUse": ("*", "drain.py", 5),
    "PreToolUse": ("SendMessage", "guard.py", 5),
}

settings = {}
if os.path.isfile(settings_path):
    with open(settings_path) as fh:
        settings = json.load(fh)
    backup = f"{settings_path}.bak-subagent-gauge-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(settings_path, backup)
    print(f"backup: {backup}")

hooks = settings.setdefault("hooks", {})
for event, (matcher, script, timeout) in entries.items():
    cmd = f'python3 "{repo}/hooks/{script}"'
    groups = hooks.setdefault(event, [])
    # json.dumps escapes the quotes in cmd, so match on the bare path.
    if any(f"{repo}/hooks/{script}" in json.dumps(g) for g in groups):
        print(f"{event}: already installed, skipping")
        continue
    group = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)
    print(f"{event}: installed")

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
print(f"wrote {settings_path}")
print("Restart running Claude Code sessions to pick up hook changes.")
PY
