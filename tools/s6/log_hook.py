import json
import os
import sys
import time

try:
    payload = json.loads(sys.stdin.read(1_000_000))
except Exception:
    payload = {"parse_error": True}
path = os.environ.get("S6_HOOK_LOG", "/tmp/s6-hook-events.jsonl")
with open(path, "a") as fh:
    fh.write(json.dumps({"ts": time.time(), "payload": payload}) + "\n")
