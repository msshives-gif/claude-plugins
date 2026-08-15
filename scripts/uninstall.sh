#!/bin/bash
# Remove every subagent-context hook entry from ~/.claude/settings.json
# (or a settings file given as $1). Backs up first. Removes individual
# hook entries (not whole groups), never a user's own hooks that share
# a group.
#
# Matching is deliberately narrow: (a) THIS clone's exact hook paths,
# so an oddly-named clone uninstalls what its own install.sh wrote; and
# (b) standard-directory paths (…/subagent-context/hooks/<script> and
# the pre-rename …/subagent-gauge/hooks/<script>), so installs from a
# default-named clone elsewhere are cleaned too. A bare basename or
# repo-name match would also remove sibling plugins under plugins/ or
# unrelated hooks that happen to be named hooks/guard.py — never do
# that. Known edge: an install made from a RENAMED clone at another
# path must be uninstalled from that clone.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="${1:-$HOME/.claude/settings.json}"

python3 - "$SETTINGS" "$REPO_DIR" <<'PY'
import json, os, shutil, sys, time

settings_path, repo = sys.argv[1], sys.argv[2]
if not os.path.isfile(settings_path):
    print(f"{settings_path} does not exist; nothing to do")
    sys.exit(0)

SCRIPTS = ("observer.py", "drain.py", "guard.py")
EXACT = tuple(os.path.join(repo, "hooks", s).replace("\\", "/")
              for s in SCRIPTS)
HISTORICAL = tuple(f"/{name}/hooks/{s}"
                   for name in ("subagent-context", "subagent-gauge")
                   for s in SCRIPTS)


def is_ours(command):
    # Normalize Windows separators so manual installs with backslash
    # paths still match.
    c = command.replace("\\", "/")
    if "/plugins/" in c:
        return False  # sibling plugins live under plugins/, never ours
    return any(p in c for p in EXACT) or any(h in c for h in HISTORICAL)


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
fd, tmp = tempfile.mkstemp(dir=settings_dir, prefix=".subagent-context-")
with os.fdopen(fd, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(tmp, settings_path)
print(f"removed {removed} hook entrie(s); wrote {settings_path}")
PY
