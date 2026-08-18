"""Stop: ended half of the paired turn marker (running half:
reorient.py). The rev-2 single "ended" write was cut as
stale-by-construction; the paired protocol fixes that half: the watcher
trusts ended only when ended.prompt_id matches the running marker's, so
a stale Stop simply fails to pair (design: Sol rounds 1-3, 2026-08-18).
The marker is evidence for the watcher's turn-boundary lane, never an
authorization by itself."""
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
    import managed
except Exception as e:
    print(f"compact-manager stop_marker: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def main():
    payload = cm.read_payload()
    cfg = cm.load_config()
    if cfg["mode"] == "off":
        return
    managed.write_activity(cfg, payload, "ended")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager stop_marker: {e!r}", file=sys.stderr)
    sys.exit(0)
