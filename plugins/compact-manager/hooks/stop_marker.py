"""Stop: reserved no-op. The rev-2 activity write was cut by the M3
plan audits (stale-by-construction — nothing marks "busy" again — and
the watcher's ladder must not trust it; the composer and
foreground-process checks are the load-bearing idle signals). The
hook stays wired so the hooks.json surface is stable."""
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compact_manager as cm
except Exception as e:
    print(f"compact-manager stop_marker: import failed: {e!r}",
          file=sys.stderr)
    sys.exit(0)


def main():
    cm.read_payload()  # drain stdin; deliberately nothing else


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"compact-manager stop_marker: {e!r}", file=sys.stderr)
    sys.exit(0)
