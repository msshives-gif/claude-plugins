#!/bin/bash
# Remove compact-manager hook entries from ~/.claude/settings.json (or a
# settings file given as $1). Matching is boundary-safe and narrow (same
# rule as the sibling plugins): THIS clone's exact hook paths, plus
# standard-directory installs (…/compact-manager/hooks/<script>.py) —
# never a bare basename, so an unrelated or sibling hook that reuses a
# filename, or a ".backup" copy, is never touched.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${1:-$HOME/.claude/settings.json}"

python3 - "$SETTINGS" "$PLUGIN_DIR" <<'PY'
import json, os, shutil, sys, tempfile, time

settings_path, plugin = sys.argv[1], sys.argv[2]
if not os.path.isfile(settings_path):
    print(f"{settings_path} does not exist; nothing to do")
    sys.exit(0)

SCRIPTS = ("advisor.py", "reorient.py", "stop_marker.py",
           "precompact.py", "session_start.py")
EXACT = [os.path.join(plugin, "hooks", s).replace("\\", "/")
         for s in SCRIPTS]
STANDARD = [f"/compact-manager/hooks/{s}" for s in SCRIPTS]


def _matches(c, path):
    i = c.find(path)
    while i != -1:
        j = i + len(path)
        # EXACT paths start at a token boundary; STANDARD suffixes may
        # sit mid-path, so only the right boundary applies to them —
        # callers pass absolute EXACT paths and "/…" STANDARD markers,
        # and both are safe with a right-boundary check plus the
        # leading "/" they carry. Keep right-boundary semantics here.
        if j == len(c) or c[j] in " \t'\"":
            return True
        i = c.find(path, i + 1)
    return False


def is_ours(command):
    c = command.replace("\\", "/")
    return any(_matches(c, p) for p in EXACT + STANDARD)


with open(settings_path) as fh:
    settings = json.load(fh)
backup = (f"{settings_path}.bak-compact-manager-"
          f"{time.strftime('%Y%m%d%H%M%S')}-{os.getpid()}")
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

settings_dir = os.path.dirname(settings_path) or "."
fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix=".compact-manager-")
with os.fdopen(fd, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings_path)
print(f"removed {removed} hook entrie(s); wrote {settings_path}")
PY
