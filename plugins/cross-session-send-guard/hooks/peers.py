"""Resolve a SendMessage `to` address to a PEER SESSION's transcript.

Grounded in the 2026-08-15 resolution spike (docs/spikes/send-guard.md):
the harness keeps a live registry at ~/.claude/sessions/<pid>.json
mapping name -> sessionId + cwd + socket + procStart. Observed peer
address forms: the registry name verbatim ("projects-f7"), name plus a
disambiguation ref ("name [fc9877]"), and a direct socket path
("uds:/run/user/1000/cc-socks/<pid>.sock").

Everything here fails open: any surprise returns None (the guard stays
silent). Never guess — a wrong-session measurement is worse than no
warning, and the ONLY path that can gate a send must never rest on a
guess. The pid/cwd/newest-transcript heuristic from issue #1 is
deliberately NOT implemented as a gating path.
"""
import json
import os
import re

_REF_SUFFIX = re.compile(r"\s+\[[0-9a-f]{4,12}\]$")
# Registry files are tiny (~300 bytes); anything bigger is not ours.
_REGISTRY_MAX_BYTES = 65_536
# Never examine more registry files than this per resolution.
_REGISTRY_MAX_FILES = 200
_SESSION_ID_OK = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def parse_address(to):
    """-> ("pid", <int>, False) for uds: form,
    ("name", <base>, <had_ref>) otherwise. had_ref records that the
    sender disambiguated with a "[ref]" suffix — the ref itself is not
    mappable to a registry entry from on-disk state."""
    if to.startswith("uds:"):
        base = os.path.basename(to[4:])
        if base.endswith(".sock"):
            try:
                return ("pid", int(base[:-5]), False)
            except ValueError:
                return None
        return None
    stripped = _REF_SUFFIX.sub("", to)
    return ("name", stripped, stripped != to)


def _proc_start(pid, proc_root):
    """Kernel starttime (field 22 of /proc/<pid>/stat), None if gone.
    The stat comm field can contain spaces/parens; parse after the
    last ')'."""
    try:
        with open(os.path.join(proc_root, str(pid), "stat")) as fh:
            stat = fh.read(8192)
        fields = stat[stat.rindex(")") + 2:].split()
        return int(fields[19])  # starttime is field 22, 20th after comm
    except Exception:
        return None


def is_alive(entry, proc_root="/proc"):
    """Live means the pid exists AND its kernel starttime matches the
    registry's procStart. A missing or malformed procStart is NOT
    proof of liveness — a recycled pid must never count (sessions here
    run for weeks; every genuine registry entry observed carries a
    string procStart)."""
    pid = entry.get("pid")
    if not isinstance(pid, int):
        return False
    start = _proc_start(pid, proc_root)
    if start is None:
        return False
    try:
        # The live registry stores procStart as a STRING (observed
        # 2026-08-15); tolerate either form.
        return start == int(entry.get("procStart"))
    except (TypeError, ValueError):
        return False


def _slug(cwd):
    return cwd.replace("/", "-")


def _read_entry(path):
    """One registry entry, or None. Regular, small files only — a
    FIFO/symlink-to-something-huge must not stall the 5s hook budget."""
    try:
        st = os.stat(path)
        if not os.path.isfile(path) or st.st_size > _REGISTRY_MAX_BYTES:
            return None
        with open(path) as fh:
            entry = json.load(fh)
        return entry if isinstance(entry, dict) else None
    except Exception:
        return None


def resolve_peer(to, payload, cfg, proc_root="/proc"):
    """-> {"session_id", "transcript", "name", "status"} or None.

    None when: empty/"main" target; no live registry match; the live
    matches span MORE THAN ONE distinct sessionId (bare or ref-bearing
    — the ref can't be mapped from disk, and the harness rejects
    ambiguous bare names itself, so guessing could measure or GATE the
    wrong session); a uds: address that isn't the matched entry's own
    declared socket; the resolved session is the sender's own; a
    malformed sessionId; or a missing transcript. Multiple pids for
    ONE session (a --resume pair) resolve to that session's newest
    entry.
    """
    try:
        if not to or to == "main":
            return None
        parsed = parse_address(to)
        if parsed is None:
            return None
        kind, key, _had_ref = parsed
        sessions_dir = cfg["sessions_dir"]
        candidates = []
        if kind == "pid":
            entry = _read_entry(os.path.join(sessions_dir, f"{key}.json"))
            if entry is not None:
                # The send goes to that literal socket: only trust the
                # entry if it declares exactly this socket as its own.
                declared = entry.get("messagingSocketPath")
                if declared and os.path.normpath(declared) == \
                        os.path.normpath(to[4:]) and \
                        is_alive(entry, proc_root):
                    candidates.append(entry)
        else:
            names = sorted(os.listdir(sessions_dir))[:_REGISTRY_MAX_FILES]
            for fn in names:
                if not fn.endswith(".json"):
                    continue
                entry = _read_entry(os.path.join(sessions_dir, fn))
                if entry is None or entry.get("name") != key:
                    continue
                if not is_alive(entry, proc_root):
                    continue
                candidates.append(entry)
        if not candidates:
            return None
        session_ids = {e.get("sessionId") for e in candidates}
        if len(session_ids) > 1:
            return None  # distinct sessions share the name: never guess
        best = max(candidates, key=lambda e: e.get("updatedAt") or 0)
        session_id = best.get("sessionId")
        cwd = best.get("cwd")
        if not session_id or not cwd:
            return None
        if not _SESSION_ID_OK.match(str(session_id)):
            return None  # path-escape / malformed id: never build a path
        if session_id == payload.get("session_id"):
            return None  # never measure the sender's own session
        projects = os.path.realpath(cfg["projects_dir"])
        transcript = os.path.realpath(
            os.path.join(projects, _slug(cwd), f"{session_id}.jsonl"))
        if not transcript.startswith(projects + os.sep):
            return None
        if not os.path.isfile(transcript):
            return None
        return {"session_id": session_id, "transcript": transcript,
                "name": best.get("name") or to,
                "status": best.get("status") or ""}
    except Exception:
        return None
