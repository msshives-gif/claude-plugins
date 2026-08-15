#!/bin/bash
# Remove the cross-session-send-guard hook entry from
# ~/.claude/settings.json (or a settings file given as $1). Matching is
# boundary-safe and narrow (same rule as the sibling plugin): THIS
# clone's exact hook path, plus standard-directory installs
# (…/cross-session-send-guard/hooks/peer_send_guard.py) — never a bare
# basename, so an unrelated or sibling hook that reuses the filename,
# or a ".backup" copy, is never touched.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${1:-$HOME/.claude/settings.json}"

python3 - "$SETTINGS" "$PLUGIN_DIR" <<'PY'
import json, os, shutil, sys, time

settings_path, plugin = sys.argv[1], sys.argv[2]
if not os.path.isfile(settings_path):
    print(f"{settings_path} does not exist; nothing to do")
    sys.exit(0)

EXACT = os.path.join(plugin, "hooks",
                     "peer_send_guard.py").replace("\\", "/")
STANDARD = "/cross-session-send-guard/hooks/peer_send_guard.py"


def _matches(c, path):
    i = c.find(path)
    while i != -1:
        j = i + len(path)
        if j == len(c) or c[j] in " \t'\"":
            return True
        i = c.find(path, i + 1)
    return False


def is_ours(command):
    c = command.replace("\\", "/")
    return _matches(c, EXACT) or _matches(c, STANDARD)


with open(settings_path) as fh:
    settings = json.load(fh)
backup = (f"{settings_path}.bak-cross-session-send-guard-"
          f"{time.strftime('%Y%m%d%H%M%S')}")
shutil.copy2(settings_path, backup)
print(f"backup: {backup}")

removed = 0
for event, groups in list(settings.get("hooks", {}).items()):
    new_groups = []
    for g in groups:
        entries = g.get("hooks", [])
        kept = [e for e in entries if not is_ours(e.get("command", ""))]
        removed += len(entries) - len(kept)
        if kept:
            g = dict(g, hooks=kept)
            new_groups.append(g)
        elif not entries:
            new_groups.append(g)
    if new_groups:
        settings["hooks"][event] = new_groups
    else:
        del settings["hooks"][event]

import tempfile
settings_dir = os.path.dirname(settings_path) or "."
fd, tmp = tempfile.mkstemp(dir=settings_dir,
                           prefix=".cross-session-send-guard-")
with os.fdopen(fd, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings_path)
print(f"removed {removed} hook entrie(s); wrote {settings_path}")
PY
