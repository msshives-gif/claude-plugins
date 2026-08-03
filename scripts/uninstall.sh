#!/bin/bash
# Remove every subagent-context hook entry from ~/.claude/settings.json
# (or a settings file given as $1). Backs up first. Removes individual
# hook entries (not whole groups), matching this clone's hook scripts by
# basename — so it works whatever the clone directory is named, and
# never removes a user's own hooks that share a group.
set -euo pipefail

SETTINGS="${1:-$HOME/.claude/settings.json}"

python3 - "$SETTINGS" <<'PY'
import json, os, shutil, sys, time

settings_path = sys.argv[1]
if not os.path.isfile(settings_path):
    print(f"{settings_path} does not exist; nothing to do")
    sys.exit(0)

# "subagent-gauge" stays in the marker list so this also cleans up
# installs made before the rename to subagent-context.
MARKERS = ("/hooks/observer.py", "/hooks/drain.py", "/hooks/guard.py",
           "subagent-context", "subagent-gauge")

with open(settings_path) as fh:
    settings = json.load(fh)
backup = f"{settings_path}.bak-subagent-context-{time.strftime('%Y%m%d%H%M%S')}"
shutil.copy2(settings_path, backup)
print(f"backup: {backup}")

removed = 0
for event, groups in list(settings.get("hooks", {}).items()):
    new_groups = []
    for g in groups:
        entries = g.get("hooks", [])
        kept = [e for e in entries
                if not any(m in e.get("command", "") for m in MARKERS)]
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
fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix=".subagent-context-")
with os.fdopen(fd, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings_path)
print(f"removed {removed} hook entrie(s); wrote {settings_path}")
PY
