#!/bin/bash
# Manual (non-plugin) install: merge the five compact-manager hooks
# into ~/.claude/settings.json (or a settings file given as $1).
# Idempotent; atomic write; backs up first.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${1:-$HOME/.claude/settings.json}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (the hooks are Python)" >&2
    exit 1
fi

python3 - "$SETTINGS" "$PLUGIN_DIR" <<'PY'
import json, os, shlex, shutil, sys, tempfile, time

settings_path, plugin = sys.argv[1], sys.argv[2]

WIRING = [
    ("PostToolUse", "*", "advisor.py"),
    ("UserPromptSubmit", None, "reorient.py"),
    ("Stop", None, "stop_marker.py"),
    ("PreCompact", "manual", "precompact.py"),
    ("PreCompact", "auto", "precompact.py"),
    ("SessionStart", "compact", "session_start.py"),
]


def cmd_for(script):
    q = shlex.quote(os.path.join(plugin, "hooks", script))
    return f"python3 {q} || python {q} || true"


def _matches(c, path):
    # Boundary-safe: the path must be followed by a delimiter or EOL,
    # so "advisor.py.backup" or metadata mentioning the path in a
    # longer token never counts as installed (audit finding).
    i = c.find(path)
    while i != -1:
        j = i + len(path)
        if j == len(c) or c[j] in " \t'\"":
            return True
        i = c.find(path, i + 1)
    return False


settings = {}
if os.path.isfile(settings_path):
    backup = (f"{settings_path}.bak-compact-manager-"
              f"{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}")
    shutil.copy2(settings_path, backup)
    print(f"backup: {backup}")
    try:
        with open(settings_path) as fh:
            settings = json.load(fh)
    except json.JSONDecodeError as e:
        sys.exit(f"{settings_path} is not valid JSON ({e}); fix it first "
                 f"(backup already taken)")

hooks = settings.setdefault("hooks", {})
added = 0
for event, matcher, script in WIRING:
    groups = hooks.setdefault(event, [])
    script_path = os.path.join(plugin, "hooks", script)
    norm = script_path.replace("\\", "/")
    present = any(
        g.get("matcher") == matcher
        and any(_matches(str(e.get("command", "")).replace("\\", "/"),
                         norm)
                for e in g.get("hooks", []))
        for g in groups)
    if present:
        continue
    group = {"hooks": [{"type": "command", "command": cmd_for(script),
                        "timeout": 5}]}
    if matcher is not None:
        group["matcher"] = matcher
    groups.append(group)
    added += 1
print(f"added {added} hook group(s)" if added else "already installed")

settings_dir = os.path.dirname(settings_path) or "."
os.makedirs(settings_dir, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix=".compact-manager-")
with os.fdopen(fd, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings_path)
print(f"wrote {settings_path}")
print("Hooks are inert until you set a mode, e.g. "
      "COMPACT_MANAGER_MODE=advisory.")
print("Restart running Claude Code sessions to pick up hook changes.")
PY
