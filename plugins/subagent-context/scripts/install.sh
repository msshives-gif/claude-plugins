#!/bin/bash
# Manual (non-plugin) install: merge subagent-context hooks into
# ~/.claude/settings.json (or a settings file given as $1), backing the
# file up BEFORE parsing it. Idempotent: running twice adds nothing
# twice. Writes are atomic (temp file + rename).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${1:-$HOME/.claude/settings.json}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (the hooks are Python)" >&2
    exit 1
fi

python3 - "$SETTINGS" "$REPO_DIR" <<'PY'
import json, os, shlex, shutil, sys, tempfile, time

settings_path, repo = sys.argv[1], sys.argv[2]
entries = {
    "SubagentStop": (None, "observer.py", 15),
    "PostToolUse": ("*", "drain.py", 5),
    "PreToolUse": ("SendMessage", "guard.py", 5),
}

settings = {}
if os.path.isfile(settings_path):
    backup = f"{settings_path}.bak-subagent-context-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(settings_path, backup)
    print(f"backup: {backup}")
    try:
        with open(settings_path) as fh:
            settings = json.load(fh)
    except json.JSONDecodeError as e:
        sys.exit(f"{settings_path} is not valid JSON ({e}); fix it first "
                 f"(backup already taken)")

hooks = settings.setdefault("hooks", {})
for event, (matcher, script, timeout) in entries.items():
    script_path = os.path.join(repo, "hooks", script)
    q = shlex.quote(script_path)
    # "|| python" covers Windows, where Python usually isn't named
    # python3. "|| true" keeps the command exit 0 even when the script
    # is missing or both interpreters fail — a reporting hook must
    # never surface a blocking exit code (2 would deny the tool call).
    cmd = f"python3 {q} || python {q} || true"
    groups = hooks.setdefault(event, [])
    # json.dumps escapes quotes/non-ASCII, so compare unescaped dumps.
    if any(script_path in json.dumps(g, ensure_ascii=False) for g in groups):
        # Already installed: refresh the timeout in place so upgrades
        # that change it (0.1.0 shipped SubagentStop at 10) take effect.
        updated = False
        for g in groups:
            for e in g.get("hooks", []):
                if script_path in e.get("command", "") \
                        and e.get("timeout") != timeout:
                    e["timeout"] = timeout
                    updated = True
        print(f"{event}: timeout updated to {timeout}" if updated
              else f"{event}: already installed, skipping")
        continue
    group = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)
    print(f"{event}: installed")

settings_dir = os.path.dirname(settings_path) or "."
os.makedirs(settings_dir, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix=".subagent-context-")
with os.fdopen(fd, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings_path)
print(f"wrote {settings_path}")
print("Restart running Claude Code sessions to pick up hook changes.")
PY
