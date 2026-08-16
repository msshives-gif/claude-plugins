#!/bin/bash
# Run every plugin's test suite. Fails if any suite fails.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

for plugin in "$REPO"/plugins/*/; do
    [ -d "$plugin/tests" ] || continue
    echo "== $(basename "$plugin") =="
    (cd "$plugin" && python3 -m unittest discover tests)
done
