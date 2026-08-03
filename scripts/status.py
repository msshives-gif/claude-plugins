#!/usr/bin/env python3
"""Show recorded subagent context sizes (the pull-side complement to the
injected reports).

Usage:
  python3 scripts/status.py               # all sessions, newest first
  python3 scripts/status.py --session ID  # one session (prefix ok)
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "hooks"))
import subagent_context as sg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="")
    args = ap.parse_args()

    cfg = sg.load_config()
    agents_root = os.path.join(cfg["state_dir"], "agents")
    if not os.path.isdir(agents_root):
        print(f"no state recorded yet under {agents_root}")
        return

    sessions = sorted(
        (s for s in os.listdir(agents_root) if s.startswith(args.session)),
        key=lambda s: os.path.getmtime(os.path.join(agents_root, s)),
        reverse=True)
    if not sessions:
        print("no matching sessions")
        return

    for session in sessions:
        print(f"session {session}")
        rows = sorted(sg.load_agent_states(cfg, session),
                      key=lambda r: r.get("current", 0), reverse=True)
        for r in rows:
            age_s = ""
            if r.get("observed_at"):
                age_s = f"{(time.time() - r['observed_at']) / 60:.0f}m ago"
            flags = []
            if r.get("current", 0) >= \
                    sg.thresholds(cfg, r.get("model"))["warn_tokens"]:
                flags.append("OVER-THRESHOLD")
            if r.get("compactions"):
                flags.append(f"compacted x{r['compactions']}")
            print(f"  {r.get('name', '?'):30s} ~{r.get('current', 0) / 1000:6.0f}k "
                  f"(peak ~{r.get('peak', 0) / 1000:.0f}k) {r.get('model', ''):22s} "
                  f"{age_s:>8s} {' '.join(flags)}")
        print()


if __name__ == "__main__":
    main()
