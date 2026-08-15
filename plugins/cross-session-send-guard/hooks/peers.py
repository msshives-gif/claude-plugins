"""Resolve a SendMessage `to` address to a PEER SESSION's transcript.

Grounded in the 2026-08-15 resolution spike (docs/spikes/send-guard.md):
the harness keeps a live registry at ~/.claude/sessions/<pid>.json
mapping name -> sessionId + cwd + socket + procStart. Observed peer
address forms: the registry name verbatim ("projects-f7"), name plus a
disambiguation ref ("name [fc9877]"), and a direct socket path
("uds:/run/user/1000/cc-socks/<pid>.sock").

Everything here fails open: any surprise returns None (the guard stays
silent). Never guess — a wrong-session measurement is worse than no
warning. The pid/cwd/newest-transcript heuristic from issue #1 is
deliberately NOT implemented as a gating path.
"""
import json
import os
import re

_REF_SUFFIX = re.compile(r"\s+\[[0-9a-f]{4,12}\]$")


def parse_address(to):
    """-> ("pid", <int>) for uds: form, ("name", <base>) otherwise."""
    if to.startswith("uds:"):
        base = os.path.basename(to[4:])
        if base.endswith(".sock"):
            try:
                return ("pid", int(base[:-5]))
            except ValueError:
                return None
        return None
    return ("name", _REF_SUFFIX.sub("", to))


def _proc_start(pid, proc_root):
    """Kernel starttime (field 22 of /proc/<pid>/stat), None if gone.
    The stat comm field can contain spaces/parens; parse after the
    last ')'."""
    try:
        with open(os.path.join(proc_root, str(pid), "stat")) as fh:
            stat = fh.read()
        fields = stat[stat.rindex(")") + 2:].split()
        return int(fields[19])  # starttime is field 22, 20th after comm
    except Exception:
        return None


def is_alive(entry, proc_root="/proc"):
    """Live means the pid exists AND its kernel starttime matches the
    registry's procStart — a recycled pid must not count (spike: age
    alone proves nothing; sessions here run for weeks)."""
    pid = entry.get("pid")
    if not isinstance(pid, int):
        return False
    start = _proc_start(pid, proc_root)
    if start is None:
        return False
    reg_start = entry.get("procStart")
    if reg_start is None:
        return True  # older registry versions: existence is best we have
    return start == reg_start


def _slug(cwd):
    return cwd.replace("/", "-")


def resolve_peer(to, payload, cfg, proc_root="/proc"):
    """-> {"session_id", "transcript", "name", "status"} or None.

    None when: empty/"main" target, no live registry match, resolved
    session is the sender's own, or the transcript file is absent.
    Multiple live matches resolve to the newest updatedAt (the
    harness's own latest-wins naming rule); the [ref] hex is not
    derivable from on-disk state, so it only strips.
    """
    try:
        if not to or to == "main":
            return None
        parsed = parse_address(to)
        if parsed is None:
            return None
        kind, key = parsed
        candidates = []
        sessions_dir = cfg["sessions_dir"]
        for fn in os.listdir(sessions_dir):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(sessions_dir, fn)) as fh:
                    entry = json.load(fh)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            if kind == "pid" and entry.get("pid") != key:
                continue
            if kind == "name" and entry.get("name") != key:
                continue
            if not is_alive(entry, proc_root):
                continue
            candidates.append(entry)
        if not candidates:
            return None
        best = max(candidates, key=lambda e: e.get("updatedAt") or 0)
        session_id = best.get("sessionId")
        cwd = best.get("cwd")
        if not session_id or not cwd:
            return None
        if session_id == payload.get("session_id"):
            return None  # never measure the sender's own session
        transcript = os.path.join(cfg["projects_dir"], _slug(cwd),
                                  f"{session_id}.jsonl")
        if not os.path.isfile(transcript):
            return None
        return {"session_id": session_id, "transcript": transcript,
                "name": best.get("name") or to,
                "status": best.get("status") or ""}
    except Exception:
        return None
