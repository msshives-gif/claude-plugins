"""Stop: idle-epoch stamp for the Layer-2 watcher. Managed mode is not
built yet, so this is a documented no-op unless mode == "managed" —
and even then it only writes a timestamp file. Exists now so the
hooks.json surface is stable across Layer 2's arrival."""
import json
import os
import sys
import time

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
except Exception as e:
    print(f"compact-manager stop_marker: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def main():
    payload = json.loads(sys.stdin.read(1_000_000))
    cfg = cm.load_config()
    if cfg["mode"] != "managed":
        return
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    d = os.path.join(cfg["state_dir"], "activity")
    cm._private_makedirs(d)
    tmp = os.path.join(d, f".{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump({"state": "idle", "epoch": time.time_ns()}, fh)
    os.replace(tmp, os.path.join(
        d, f"{cm.path_component(session_id)}.json"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager stop_marker: {e!r}", file=sys.stderr)
    sys.exit(0)
