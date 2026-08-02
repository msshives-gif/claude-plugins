#!/bin/bash
# Remove every subagent-gauge hook entry from ~/.claude/settings.json
# (or a settings file given as $1). Backs up first.
set -euo pipefail

SETTINGS="${1:-$HOME/.claude/settings.json}"

python3 - "$SETTINGS" <<'PY'
import json, os, shutil, sys, time

settings_path = sys.argv[1]
if not os.path.isfile(settings_path):
    print(f"{settings_path} does not exist; nothing to do")
    sys.exit(0)

with open(settings_path) as fh:
    settings = json.load(fh)
backup = f"{settings_path}.bak-subagent-gauge-{time.strftime('%Y%m%d%H%M%S')}"
shutil.copy2(settings_path, backup)
print(f"backup: {backup}")

removed = 0
for event, groups in list(settings.get("hooks", {}).items()):
    kept = [g for g in groups if "subagent-gauge" not in json.dumps(g)]
    removed += len(groups) - len(kept)
    if kept:
        settings["hooks"][event] = kept
    else:
        del settings["hooks"][event]

with open(settings_path, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
print(f"removed {removed} hook group(s); wrote {settings_path}")
PY
