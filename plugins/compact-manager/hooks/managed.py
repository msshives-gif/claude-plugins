#!/usr/bin/env python3
"""compact-manager Layer 2 managed-mode watcher.

The watcher is deliberately conservative.  A missing file, an unfamiliar
pane, an ambiguous process, or a failed revalidation prevents keystrokes.
Layer-1 state is imported read-only; managed mode owns only managed/.
"""
import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact_manager as cm  # noqa: E402


SCHEMA = 1
HEARTBEAT_S = 10.0
LEASE_FRESH_S = HEARTBEAT_S * 3
REQUEST_MAX_BYTES = 4096
REQUEST_MAX_AGE_S = 600
SCAN_MAX_BYTES = 16_000_000
BACKOFF_INITIAL_S = 30.0
BACKOFF_MAX_S = 300.0
RETRY_LIMIT = 1
ATTEMPT_STATES = {
    "READY", "TRIGGERED", "PREPARED", "TYPED_VERIFIED", "SUBMITTED",
    "ACKED", "BOUNDARY_CONFIRMED", "DEFERRED", "LATCHED",
    "SUBMISSION_UNCERTAIN", "CLEANUP_REQUIRED",
}
ALERT_STATES = {"LATCHED", "SUBMISSION_UNCERTAIN", "CLEANUP_REQUIRED"}
LIFECYCLE_STATES = {"WATCHER_READY", "ALERT_DELIVERY", "WATCHER_RETIRED"}
INSTRUCTION_TEMPLATE = ("/compact [cm-{nonce}] Preserve the task list and "
                        "open decisions to the handoff file")
INSTRUCTION_DENYLIST = ";&|><()\"'$`\n\r"
ADOPT_ACKNOWLEDGEMENT = (
    "In adopt mode, the automation may in a worst-case race deliver the "
    "fixed `/compact …` bytes and one Enter to the underlying shell. "
    "The resulting shell behavior is not semantically bounded; "
    "concurrent user input can alter the executed line.")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

MANAGED_DEFAULTS = {
    "managed_trigger_pct": None,
    "managed_stable_ms": 300,
    "managed_poll_s": 15,
    "managed_ack_timeout_s": 120,
    "managed_completion_timeout_s": 300,
    "managed_deadline_hours": 24,
    "managed_pane_commands": ["claude"],
}


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def load_config(base=None, environ=None):
    """Layer managed_* knobs on cm.load_config(), then clamp them."""
    cfg = dict(base or cm.load_config())
    env = os.environ if environ is None else environ
    raw_file = {}
    path = env.get("COMPACT_MANAGER_CONFIG",
                   os.path.expanduser("~/.claude/compact-manager.json"))
    try:
        if os.path.isfile(path) and os.path.getsize(path) <= 1_000_000:
            with open(path) as fh:
                value = json.load(fh)
            if isinstance(value, dict):
                raw_file = value
    except Exception:
        pass
    raw = {}
    for key, default in MANAGED_DEFAULTS.items():
        value = raw_file.get(key, default)
        env_value = env.get("COMPACT_MANAGER_" + key.upper())
        if env_value is not None:
            value = env_value
        raw[key] = value

    soft = float(cfg.get("soft_pct", 0.70))
    hard = float(cfg.get("hard_pct", 0.80))
    trigger = _finite_number(raw["managed_trigger_pct"])
    if trigger is None or not soft < trigger <= 1.0:
        trigger = hard
    cfg["managed_trigger_pct"] = trigger

    def integer(key, floor, ceiling=None):
        value = _finite_number(raw[key])
        if value is None:
            value = MANAGED_DEFAULTS[key]
        value = max(floor, int(value))
        return min(value, ceiling) if ceiling is not None else value

    cfg["managed_stable_ms"] = integer("managed_stable_ms", 200)
    cfg["managed_poll_s"] = integer("managed_poll_s", 5)
    cfg["managed_ack_timeout_s"] = integer("managed_ack_timeout_s", 30)
    cfg["managed_completion_timeout_s"] = max(
        cfg["managed_ack_timeout_s"], integer("managed_completion_timeout_s", 30))
    cfg["managed_deadline_hours"] = integer("managed_deadline_hours", 1, 72)
    commands = raw["managed_pane_commands"]
    if isinstance(commands, str):
        try:
            commands = json.loads(commands)
        except ValueError:
            commands = [commands]
    if not (isinstance(commands, list) and commands and
            all(isinstance(x, str) and x.strip() for x in commands)):
        commands = list(MANAGED_DEFAULTS["managed_pane_commands"])
    cfg["managed_pane_commands"] = commands
    return cfg


def managed_paths(cfg, session_id, socket="", pane_id=""):
    sid = cm.path_component(session_id)
    base = os.path.join(cfg["state_dir"], "managed")
    pane_key = hashlib.sha256(
        (str(socket) + "\0" + str(pane_id)).encode()).hexdigest()
    return {
        "base": base,
        "txn": os.path.join(base, ".txn.lock"),
        "session_lease": os.path.join(base, "leases", "session-%s.json" % sid),
        "pane_lease": os.path.join(base, "leases", "pane-%s.json" % pane_key),
        "scan": os.path.join(base, "watchers", "%s.scan.json" % sid),
        "journal": os.path.join(base, "watchers", "%s.journal.jsonl" % sid),
        "request": os.path.join(base, "requests", "%s.json" % sid),
    }


def _atomic_json(path, value):
    cm._private_makedirs(os.path.dirname(path))
    tmp = path + ".%s.%s.tmp" % (os.getpid(), time.monotonic_ns())
    with open(tmp, "w") as fh:
        json.dump(value, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


@contextlib.contextmanager
def txn_lock(path, timeout=2.0, monotonic=time.monotonic):
    cm._private_makedirs(os.path.dirname(path))
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() >= deadline:
                    raise cm.LockTimeout(path)
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_json_inode(path):
    """Return (object, inode), distinguishing absence from ambiguity."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            return None, None
        with open(path) as fh:
            value = json.load(fh)
        after = os.lstat(path)
        if before.st_ino != after.st_ino or not isinstance(value, dict):
            return None, None
        return value, before.st_ino
    except FileNotFoundError:
        return {}, 0
    except Exception:
        return None, None


def parse_proc_stat(text):
    """Return proc(5) tpgid(field 8) and starttime(field 22)."""
    try:
        fields = text[text.rindex(")") + 2:].split()
        return {"tpgid": int(fields[5]), "starttime": int(fields[19])}
    except Exception:
        return None


def proc_stat(pid, proc_root="/proc"):
    try:
        with open(os.path.join(proc_root, str(int(pid)), "stat")) as fh:
            return parse_proc_stat(fh.read(8192))
    except Exception:
        return None


def proc_matches(pid, expected_start, proc_root="/proc"):
    live = proc_stat(pid, proc_root)
    try:
        return live is not None and live["starttime"] == int(expected_start)
    except (TypeError, ValueError, OverflowError):
        return False


def lease_is_live(lease, now=None, proc_root="/proc"):
    """Unreadable/malformed liveness is live; reclaim needs two proofs."""
    if not isinstance(lease, dict):
        return True
    now = time.time() if now is None else now
    heartbeat = _finite_number(lease.get("heartbeat_at"))
    if heartbeat is None or now - heartbeat < LEASE_FRESH_S:
        return True
    pid, start = lease.get("pid"), lease.get("proc_start")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return True
    live = proc_stat(pid, proc_root)
    if live is None:
        # ENOENT is represented by a missing /proc directory and proves death;
        # other read failures remain ambiguous/live.
        if not os.path.exists(os.path.join(proc_root, str(pid))):
            return False
        return True
    try:
        return live["starttime"] == int(start)
    except (TypeError, ValueError, OverflowError):
        return True


def _lease_record(run_token, pid, proc_start, now):
    return {"run_token": run_token, "pid": pid, "proc_start": proc_start,
            "heartbeat_at": now}


def _prune_managed_locked(cfg, exclude_session_lease=None, now=None,
                          proc_root="/proc"):
    """TTL-prune only namespaces whose exact lease pair is reclaimable."""
    now = time.time() if now is None else now
    cutoff = now - max(1, cfg.get("state_ttl_days", 7)) * 86_400
    lease_dir = os.path.join(cfg["state_dir"], "managed", "leases")
    try:
        session_names = [name for name in os.listdir(lease_dir)
                         if name.startswith("session-") and name.endswith(".json")]
        pane_names = [name for name in os.listdir(lease_dir)
                      if name.startswith("pane-") and name.endswith(".json")]
    except OSError:
        return
    for name in session_names:
        session_path = os.path.join(lease_dir, name)
        if session_path == exclude_session_lease:
            continue
        lease, session_inode = read_json_inode(session_path)
        if not lease or lease_is_live(lease, now, proc_root):
            continue
        matches = []
        ambiguous = False
        for pane_name in pane_names:
            pane_path = os.path.join(lease_dir, pane_name)
            pane, pane_inode = read_json_inode(pane_path)
            if pane is None:
                ambiguous = True
                break
            if pane and pane.get("run_token") == lease.get("run_token"):
                matches.append((pane_path, pane, pane_inode))
        if (ambiguous or len(matches) != 1 or
                lease_is_live(matches[0][1], now, proc_root)):
            continue
        # Retention age keys off the last heartbeat, not merely an old scan.
        heartbeat = _finite_number(lease.get("heartbeat_at"))
        if heartbeat is None or heartbeat >= cutoff:
            continue
        sid = name[len("session-"):-len(".json")]
        session_paths = managed_paths(cfg, sid)
        for artifact in (session_paths["journal"], session_paths["scan"],
                         session_paths["request"]):
            try:
                info = os.lstat(artifact)
                if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    os.unlink(artifact)
            except OSError:
                pass
        conditional_remove(matches[0][0], lease["run_token"],
                           matches[0][2], locked=True)
        conditional_remove(session_path, lease["run_token"], session_inode,
                           locked=True)


def acquire_leases(paths, run_token, pid, proc_start, now=None,
                   proc_root="/proc", before_second=None, ttl_days=7):
    """Acquire session then pane atomically; roll session back on loss."""
    now = time.time() if now is None else now
    with txn_lock(paths["txn"]):
        # Housekeeping shares this one transaction and never touches the
        # session being acquired: its journal tail may require recovery.
        cfg = {"state_dir": os.path.dirname(paths["base"]),
               "state_ttl_days": ttl_days}
        _prune_managed_locked(cfg, paths["session_lease"], now, proc_root)
        acquired = []
        for index, path in enumerate((paths["session_lease"], paths["pane_lease"])):
            old, inode = read_json_inode(path)
            if old is None:
                return False, {"reason": "ambiguous_lease", "path": path}
            if old and lease_is_live(old, now, proc_root):
                for made_path, made_inode in acquired:
                    conditional_remove(made_path, run_token, made_inode, locked=True)
                return False, {"reason": "lease_held", "path": path,
                               "holder": old}
            _atomic_json(path, _lease_record(run_token, pid, proc_start, now))
            made_inode = os.lstat(path).st_ino
            acquired.append((path, made_inode))
            if index == 0 and before_second:
                before_second()
        return True, {"inodes": {p: ino for p, ino in acquired}}


def conditional_remove(path, run_token, observed_inode, txn_path=None,
                       locked=False):
    def remove():
        value, inode = read_json_inode(path)
        if value is None or not value:
            return False
        if inode != observed_inode or value.get("run_token") != run_token:
            return False
        os.unlink(path)
        return True
    if locked:
        return remove()
    if txn_path is None:
        raise ValueError("txn_path required")
    with txn_lock(txn_path):
        return remove()


def release_leases(paths, run_token):
    removed = []
    with txn_lock(paths["txn"]):
        for path in (paths["session_lease"], paths["pane_lease"]):
            value, inode = read_json_inode(path)
            if value and value.get("run_token") == run_token:
                if conditional_remove(path, run_token, inode, locked=True):
                    removed.append(path)
    return removed


def leases_owned(paths, run_token):
    for path in (paths["session_lease"], paths["pane_lease"]):
        value, _inode = read_json_inode(path)
        if value is None or not value or value.get("run_token") != run_token:
            return False
    return True


def heartbeat_leases(paths, run_token, now=None, fail_after=None):
    """Conditionally replace both lease files in one short transaction."""
    now = time.time() if now is None else now
    with txn_lock(paths["txn"]):
        observed = []
        for path in (paths["session_lease"], paths["pane_lease"]):
            value, inode = read_json_inode(path)
            if value is None or not value or value.get("run_token") != run_token:
                return False
            observed.append((path, value, inode))
        # Re-read immediately before each replacement.  A replacement made by
        # this loop becomes the new exact owner; a rollback can never be
        # resurrected from a stale snapshot.
        for index, (path, value, inode) in enumerate(observed):
            current, current_inode = read_json_inode(path)
            if (current is None or current_inode != inode or
                    current.get("run_token") != run_token):
                return False
            updated = dict(current, heartbeat_at=now)
            _atomic_json(path, updated)
            if fail_after is not None and index == fail_after:
                raise OSError("injected heartbeat failure")
        return True


def default_run_tmux(argv, timeout=5):
    return subprocess.run(["tmux"] + list(argv), capture_output=True,
                          text=True, timeout=timeout, check=False)


def _tmux(socket, args):
    return (["-S", socket] if socket else []) + list(args)


def _result(result):
    if isinstance(result, str):
        return 0, result, ""
    return result.returncode, result.stdout, result.stderr


def pane_facts(socket, pane_id, run_tmux=default_run_tmux):
    fmt = "\t".join(("#{pane_id}", "#{pane_tty}", "#{pane_pid}",
                     "#{pane_current_command}", "#{pane_in_mode}",
                     "#{pane_width}", "#{pane_height}", "#{session_id}"))
    try:
        rc, out, _ = _result(run_tmux(
            _tmux(socket, ["display-message", "-p", "-t", pane_id, fmt])))
        if rc != 0:
            return None
        parts = out.rstrip("\n").split("\t")
        if len(parts) != 8 or parts[0] != pane_id:
            return None
        return {"pane_id": parts[0], "pane_tty": parts[1],
                "pane_pid": int(parts[2]), "pane_current_command": parts[3],
                "pane_in_mode": int(parts[4]), "width": int(parts[5]),
                "height": int(parts[6]), "tmux_session_id": parts[7]}
    except Exception:
        return None


def _registry_entry(pid, sessions_dir):
    path = os.path.join(sessions_dir, "%s.json" % pid)
    try:
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or before.st_size > 65_536):
            return None
        with open(path) as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def derive_transcript(cwd, session_id, projects_dir=None):
    projects = os.path.realpath(projects_dir or
                                os.path.expanduser("~/.claude/projects"))
    slug = str(cwd).replace("/", "-")
    target = os.path.realpath(os.path.join(projects, slug,
                                           "%s.jsonl" % session_id))
    if not target.startswith(projects + os.sep) or not os.path.isfile(target):
        return None
    return target


def build_binding(socket, pane_id, attended, run_tmux=default_run_tmux,
                  proc_root="/proc", sessions_dir=None, projects_dir=None,
                  run_token=None):
    """Perform and record the pane -> tpgid leader -> registry walk."""
    facts = pane_facts(socket, pane_id, run_tmux)
    if facts is None:
        return None, "pane_missing"
    root = proc_stat(facts["pane_pid"], proc_root)
    if root is None:
        return None, "pane_process_missing"
    claude_pid = root["tpgid"]
    if claude_pid <= 0:
        return None, "foreground_missing"
    leader = proc_stat(claude_pid, proc_root)
    if leader is None:
        return None, "claude_process_missing"
    sessions_dir = sessions_dir or os.path.expanduser("~/.claude/sessions")
    entry = _registry_entry(claude_pid, sessions_dir)
    if entry is None:
        return None, "registry_missing"
    try:
        if int(entry.get("procStart")) != leader["starttime"]:
            return None, "registry_start_mismatch"
    except (TypeError, ValueError, OverflowError):
        return None, "registry_start_mismatch"
    session_id, cwd = entry.get("sessionId"), entry.get("cwd")
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        return None, "session_id_invalid"
    transcript = derive_transcript(cwd, session_id, projects_dir)
    if transcript is None:
        return None, "transcript_missing"
    return ({"socket": socket or "", "pane_id": pane_id,
             "pane_tty": facts["pane_tty"],
             "pane_root_pid": facts["pane_pid"],
             "pane_root_start": root["starttime"],
             "claude_pid": claude_pid, "claude_start": leader["starttime"],
             "session_id": session_id, "transcript_path": transcript,
             "tmux_session_id": facts["tmux_session_id"],
             "run_token": run_token or "", "attended": bool(attended)}, None)


def validate_binding(binding, cfg, paths=None, run_tmux=default_run_tmux,
                     proc_root="/proc", check_leases=True):
    facts = pane_facts(binding["socket"], binding["pane_id"], run_tmux)
    if facts is None:
        return False, "pane_missing"
    if facts["pane_tty"] != binding["pane_tty"]:
        return False, "tty_changed"
    root = proc_stat(facts["pane_pid"], proc_root)
    if (facts["pane_pid"] != binding["pane_root_pid"] or root is None or
            root["starttime"] != binding["pane_root_start"]):
        return False, "pane_root_changed"
    if not proc_matches(binding["claude_pid"], binding["claude_start"], proc_root):
        return False, "claude_dead"
    if root["tpgid"] != binding["claude_pid"]:
        return False, "foreground_lost"
    if facts["pane_current_command"] not in cfg["managed_pane_commands"]:
        return False, "command_changed"
    if facts["pane_in_mode"] != 0:
        return False, "pane_in_mode"
    if check_leases and (paths is None or
                         not leases_owned(paths, binding["run_token"])):
        return False, "lease_lost"
    return True, facts


def default_cursor():
    return {"schema": SCHEMA, "device": None, "inode": None,
            "observed_size": 0, "offset": 0, "file_epoch": 0,
            "current": 0, "boundary_count": 0, "last_boundary": None,
            "anchor": None, "model": "", "trailing_fragment": False,
            "caught_up": False, "_scan_error": False}


def load_cursor(path):
    try:
        with open(path) as fh:
            value = json.load(fh)
        if isinstance(value, dict):
            out = default_cursor()
            out.update(value)
            return out
    except Exception:
        pass
    return default_cursor()


def save_cursor(path, cursor):
    # Keep the durable shape exactly as pinned.  caught_up and the trailing
    # fragment bit are per-scan scheduling facts, not durable identity.
    keys = ("device", "inode", "observed_size", "offset", "file_epoch",
            "current", "boundary_count", "last_boundary", "anchor", "model")
    _atomic_json(path, {key: cursor.get(key) for key in keys})


def _row_hash(raw):
    return hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()


def _hash_at(path, row):
    try:
        with open(path, "rb") as fh:
            fh.seek(int(row["offset"]))
            raw = fh.readline()
        expected = row.get("sha256_of_row", row.get("sha256_of_first_row"))
        return bool(raw.endswith(b"\n")) and _row_hash(raw) == expected
    except Exception:
        return None


def generation(cursor):
    boundary = cursor.get("last_boundary")
    return {"file_epoch": cursor.get("file_epoch", 0),
            "last_boundary_offset": boundary.get("offset") if boundary else None,
            "last_boundary_sha256": (boundary.get("sha256_of_row")
                                      if boundary else None)}


def generation_key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _reset_cursor(old, st):
    return dict(default_cursor(), device=st.st_dev, inode=st.st_ino,
                file_epoch=int(old.get("file_epoch", 0)) + 1)


def scan_cursor(cursor, transcript, cap=SCAN_MAX_BYTES):
    """Scan complete rows against one stat snapshot and re-stat at end."""
    cursor = dict(cursor, _scan_error=False)
    try:
        snapshot = os.stat(transcript)
        if not stat.S_ISREG(snapshot.st_mode):
            return dict(cursor, caught_up=False, _scan_error=True)
    except OSError:
        return dict(cursor, caught_up=False, _scan_error=True)
    initialized = cursor.get("device") is not None
    changed = (initialized and
               (cursor.get("device") != snapshot.st_dev or
                cursor.get("inode") != snapshot.st_ino or
                snapshot.st_size < cursor.get("observed_size", 0)))
    if initialized and not changed:
        probe = cursor.get("last_boundary") or cursor.get("anchor")
        if probe:
            anchor_status = _hash_at(transcript, probe)
            if anchor_status is None:
                return dict(cursor, caught_up=False, _scan_error=True)
            if not anchor_status:
                changed = True
    if changed:
        cursor = _reset_cursor(cursor, snapshot)
    elif not initialized:
        cursor = dict(cursor, device=snapshot.st_dev, inode=snapshot.st_ino)
    start = int(cursor.get("offset", 0))
    budget = max(1, int(cap))
    try:
        with open(transcript, "rb") as fh:
            fh.seek(start)
            data = fh.read(min(budget, max(0, snapshot.st_size - start)))
    except OSError:
        return dict(cursor, caught_up=False, _scan_error=True)
    last_nl = data.rfind(b"\n")
    complete = data[:last_nl + 1] if last_nl >= 0 else b""
    pos = start
    for raw in complete.splitlines(keepends=True):
        row_offset = pos
        pos += len(raw)
        if cursor.get("anchor") is None:
            cursor["anchor"] = {"offset": row_offset,
                                "sha256_of_first_row": _row_hash(raw)}
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        md = row.get("compactMetadata")
        if isinstance(md, dict):
            cursor["boundary_count"] = cursor.get("boundary_count", 0) + 1
            cursor["last_boundary"] = {
                "offset": row_offset, "sha256_of_row": _row_hash(raw)}
            post = md.get("postTokens")
            cursor["current"] = (post if isinstance(post, int) and
                                 not isinstance(post, bool) and post >= 0 else 0)
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("model"):
            cursor["model"] = str(message["model"])
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        counts = [usage.get(k) for k in cm._USAGE_KEYS
                  if isinstance(usage.get(k), int) and
                  not isinstance(usage.get(k), bool)]
        if counts:
            total = sum(counts)
            if message.get("stop_reason") is not None or cursor.get("current", 0) == 0:
                cursor["current"] = total
    cursor["offset"] = start + len(complete)
    cursor["observed_size"] = snapshot.st_size
    cursor["trailing_fragment"] = cursor["offset"] < snapshot.st_size
    try:
        final = os.stat(transcript)
        cursor["caught_up"] = (final.st_dev == snapshot.st_dev and
                               final.st_ino == snapshot.st_ino and
                               final.st_size == cursor["offset"] and
                               not cursor["trailing_fragment"])
    except OSError:
        cursor["caught_up"] = False
        cursor["_scan_error"] = True
    return cursor


def initial_scan(cursor, transcript):
    while True:
        before = (cursor.get("file_epoch"), cursor.get("offset"))
        cursor = scan_cursor(cursor, transcript, cap=2 ** 63 - 1)
        if cursor.get("caught_up") or before == (cursor.get("file_epoch"),
                                                 cursor.get("offset")):
            return cursor


def journal_append(path, record):
    cm._private_makedirs(os.path.dirname(path))
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def journal_record(path, state, attempt, now_wall=None, **extra):
    record = {
        "schema": SCHEMA, "ts": time.time() if now_wall is None else now_wall,
        "state": state, "attempt_id": attempt.get("attempt_id"),
        "run_token": attempt.get("run_token"), "nonce": attempt.get("nonce"),
        "generation": attempt.get("generation"),
        "retry_n": attempt.get("retry_n", 0),
        "packet_seq_at_prepare": attempt.get("attempt_packet_seq_floor", 0),
        "attempt_packet_seq_floor": attempt.get("attempt_packet_seq_floor", 0),
        "nonces": list(attempt.get("nonces", [])),
        "timers": dict(attempt.get("timers", {})),
    }
    record.update(extra)
    journal_append(path, record)
    return record


def read_journal(path):
    records = []
    try:
        with open(path, "rb") as fh:
            lines = fh.readlines()
    except OSError:
        return records
    for raw in lines:
        if not raw.endswith(b"\n"):
            continue
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get("schema") == SCHEMA:
                records.append(value)
        except Exception:
            continue
    return records


def attempt_tail(path):
    for record in reversed(read_journal(path)):
        if record.get("state") in ATTEMPT_STATES:
            return record
    return None


def boot_id(proc_root="/proc"):
    try:
        with open(os.path.join(proc_root, "sys/kernel/random/boot_id")) as fh:
            return fh.read(200).strip()
    except OSError:
        return "unknown"


def recover_attempt(path, current_boot_id):
    tail = attempt_tail(path)
    if tail is None:
        return None
    state = tail.get("state")
    recovered = dict(tail)
    recovered["attempt_packet_seq_floor"] = tail.get(
        "attempt_packet_seq_floor", tail.get("packet_seq_at_prepare", 0))
    recovered["nonces"] = list(tail.get("nonces") or
                                ([tail.get("nonce")] if tail.get("nonce") else []))
    if state == "PREPARED":
        recovered["state"] = "CLEANUP_REQUIRED"
        recovered["reason"] = "recovered_prepared"
    elif state == "TYPED_VERIFIED":
        recovered["state"] = "SUBMISSION_UNCERTAIN"
        recovered["reason"] = "recovered_typed_verified"
    elif state in ("TRIGGERED", "SUBMITTED", "ACKED", "DEFERRED"):
        timers = recovered.get("timers") or {}
        if timers.get("boot_id") != current_boot_id:
            recovered["state"] = "LATCHED"
            recovered["latch_kind"] = "SAFETY"
            recovered["reason"] = "cross_boot_timer"
    return recovered


def new_attempt(run_token, generation_value, packet_floor, now_mono,
                current_boot_id, trigger="threshold"):
    nonce = os.urandom(8).hex()
    return {"state": "TRIGGERED", "attempt_id": os.urandom(8).hex(),
            "run_token": run_token, "nonce": nonce, "nonces": [nonce],
            "generation": generation_value, "retry_n": 0,
            "attempt_packet_seq_floor": int(packet_floor),
            "trigger_source": trigger,
            "timers": {"boot_id": current_boot_id,
                       "created_mono": now_mono}}


def packet_seq(packet):
    value = packet.get("seq", 0) if isinstance(packet, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def packet_classification(attempt, packet):
    if not isinstance(packet, dict):
        return "NONE"
    custom = str(packet.get("custom_instructions") or "")
    if any("[cm-%s]" % nonce in custom for nonce in attempt.get("nonces", [])):
        return "OWN"
    if (packet_seq(packet) > attempt["attempt_packet_seq_floor"] or
            packet.get("trigger") == "auto"):
        return "FOREIGN"
    return "NONE"


def boundary_confirmed_for_packet(packet, cursor):
    if not isinstance(packet, dict):
        return False
    base = packet.get("base_compaction_count")
    return (isinstance(base, int) and not isinstance(base, bool) and
            cursor.get("boundary_count", 0) > base)


def transition_attempt(attempt, event, now_mono, cfg, packet=None,
                       generation_value=None):
    """Pure attempt state machine used by the daemon and clock tests."""
    out = dict(attempt)
    out["timers"] = dict(attempt.get("timers", {}))
    out["nonces"] = list(attempt.get("nonces", []))
    state = out["state"]
    # Bytes may remain in the composer.  Even a later boundary is not proof
    # of their disposition; only the explicit operator resolution can clear
    # this state.
    if state == "CLEANUP_REQUIRED":
        return out
    same_generation = (generation_value is None or
                       generation_key(generation_value) ==
                       generation_key(out.get("generation")))
    if not same_generation:
        out["state"] = "BOUNDARY_CONFIRMED"
        return out
    classification = packet_classification(out, packet)
    if classification == "OWN" and state != "CLEANUP_REQUIRED":
        out["state"] = "ACKED"
        out["timers"].setdefault(
            "completion_deadline_mono", now_mono + cfg["managed_completion_timeout_s"])
        return out
    if classification == "FOREIGN":
        if state == "ACKED":
            # Our disposition is already proven.  Layer 1 may have replaced
            # our packet with the native compaction that will advance the
            # boundary; continue the bounded completion wait.
            return out
        if state in ("TRIGGERED", "DEFERRED"):
            out["state"] = "DEFERRED"
            out["reason"] = "foreign_packet"
            out["timers"].setdefault(
                "foreign_deadline_mono",
                now_mono + cfg["managed_completion_timeout_s"])
        elif state not in ("READY", "BOUNDARY_CONFIRMED", "LATCHED"):
            out["state"] = "CLEANUP_REQUIRED"
            out["reason"] = "foreign_after_r4"
        return out
    if event == "prepared":
        out["state"] = "PREPARED"
    elif event == "typed_verified":
        out["state"] = "TYPED_VERIFIED"
    elif event == "submitted":
        out["state"] = "SUBMITTED"
        out["timers"]["ack_deadline_mono"] = (
            now_mono + cfg["managed_ack_timeout_s"])
    elif event == "submission_uncertain":
        out["state"] = "SUBMISSION_UNCERTAIN"
    elif event == "cleanup_required":
        out["state"] = "CLEANUP_REQUIRED"
    elif event == "threshold_latch":
        out["state"], out["latch_kind"] = "LATCHED", "THRESHOLD"
    elif event == "safety_latch":
        out["state"], out["latch_kind"] = "LATCHED", "SAFETY"
    elif event == "pct_rearmed":
        if state == "LATCHED" and out.get("latch_kind") == "THRESHOLD":
            out["state"] = "READY"
    elif event == "timer":
        if state == "SUBMITTED" and now_mono >= out["timers"].get(
                "ack_deadline_mono", float("inf")):
            if out.get("retry_n", 0) < RETRY_LIMIT:
                out["retry_n"] = out.get("retry_n", 0) + 1
                out["nonce"] = os.urandom(8).hex()
                out.setdefault("nonces", []).append(out["nonce"])
                out["state"] = "TRIGGERED"
            else:
                out["state"], out["latch_kind"] = "LATCHED", "SAFETY"
                out["reason"] = "missing_ack"
        elif state == "ACKED" and now_mono >= out["timers"].get(
                "completion_deadline_mono", float("inf")):
            out["state"], out["latch_kind"] = "LATCHED", "SAFETY"
            out["reason"] = "missing_boundary"
        elif state == "DEFERRED" and out.get("reason") == "foreign_packet" and \
                now_mono >= out["timers"].get("foreign_deadline_mono", float("inf")):
            out["state"], out["latch_kind"] = "LATCHED", "SAFETY"
            out["reason"] = "foreign_uncertain"
    return out


def instruction_text(nonce):
    if not re.match(r"^[0-9a-f]{16}$", nonce):
        raise ValueError("invalid nonce")
    text = INSTRUCTION_TEMPLATE.format(nonce=nonce)
    if any(ch in text for ch in INSTRUCTION_DENYLIST):
        raise ValueError("instruction contains shell metacharacter")
    return text


def composer_idle(capture):
    if "esc to interrupt" in capture.lower():
        return False
    lines = [line.rstrip("\r") for line in capture.splitlines()
             if line.startswith("❯")]
    return bool(lines) and lines[-1] == "❯\u00a0"


def composer_exact(capture, text):
    lines = [line.rstrip("\r") for line in capture.splitlines()
             if line.startswith("❯")]
    return bool(lines) and lines[-1] == "❯\u00a0" + text


def capture_pane(binding, run_tmux=default_run_tmux):
    rc, out, _ = _result(run_tmux(_tmux(
        binding["socket"], ["capture-pane", "-p", "-J", "-t",
                            binding["pane_id"]])))
    return out if rc == 0 else None


def ladder_preflight(binding, cfg, paths, run_tmux=default_run_tmux,
                     proc_root="/proc", wait=lambda seconds: time.sleep(seconds)):
    ok, facts = validate_binding(binding, cfg, paths, run_tmux, proc_root)
    if not ok:
        return False, "R1_%s" % facts
    first = capture_pane(binding, run_tmux)
    leases_ok, leases_reason = validate_binding(
        binding, cfg, paths, run_tmux, proc_root)
    if not leases_ok:
        return False, "R2_%s" % leases_reason
    if first is None or not composer_idle(first):
        return False, "R2_not_idle"
    dimensions = (facts["width"], facts["height"])
    digest = hashlib.sha256(first.encode()).digest()
    wait(cfg["managed_stable_ms"] / 1000.0)
    second = capture_pane(binding, run_tmux)
    ok, facts2 = validate_binding(binding, cfg, paths, run_tmux, proc_root)
    if not ok:
        return False, "R3_%s" % facts2
    if (second is None or hashlib.sha256(second.encode()).digest() != digest or
            (facts2["width"], facts2["height"]) != dimensions):
        return False, "R3_changed"
    return True, "ready"


def run_ladder(binding, cfg, paths, attempt, cursor, journal_path,
               packet_loader, run_tmux=default_run_tmux, proc_root="/proc",
               wait=lambda seconds: time.sleep(seconds), now_mono=time.monotonic):
    """Execute R1-R6'.  Never sends a clearing key."""
    ok, reason = ladder_preflight(binding, cfg, paths, run_tmux, proc_root, wait)
    if not ok:
        return dict(attempt, state="DEFERRED", reason=reason)
    ok, reason = validate_binding(binding, cfg, paths, run_tmux, proc_root)
    if not ok:
        return dict(attempt, state="DEFERRED", reason="R4_%s" % reason)
    attempt = transition_attempt(attempt, "prepared", now_mono(), cfg)
    journal_record(journal_path, "PREPARED", attempt)
    text = instruction_text(attempt["nonce"])
    rc, _, _ = _result(run_tmux(_tmux(
        binding["socket"], ["send-keys", "-t", binding["pane_id"], "-l", text])))
    if rc != 0:
        attempt.update(state="CLEANUP_REQUIRED", reason="R4_send_failed")
        journal_record(journal_path, "CLEANUP_REQUIRED", attempt,
                       reason=attempt["reason"])
        return attempt
    wait(0.4)
    capture = capture_pane(binding, run_tmux)
    ok, reason = validate_binding(binding, cfg, paths, run_tmux, proc_root)
    if not ok or capture is None or not composer_exact(capture, text):
        detail = reason if not ok else "composer_mismatch"
        attempt.update(state="CLEANUP_REQUIRED", reason="R5_%s" % detail)
        journal_record(journal_path, "CLEANUP_REQUIRED", attempt,
                       reason=attempt["reason"])
        return attempt
    attempt = transition_attempt(attempt, "typed_verified", now_mono(), cfg)
    journal_record(journal_path, "TYPED_VERIFIED", attempt)
    ok, why = validate_binding(binding, cfg, paths, run_tmux, proc_root)
    capture = capture_pane(binding, run_tmux)
    packet = packet_loader()
    if (not ok or capture is None or not composer_exact(capture, text) or
            packet_seq(packet) > attempt["attempt_packet_seq_floor"] or
            generation_key(generation(cursor)) != generation_key(attempt["generation"])):
        if not ok:
            detail = str(why)
        elif capture is None or not composer_exact(capture, text):
            detail = "composer_mismatch"
        elif packet_seq(packet) > attempt["attempt_packet_seq_floor"]:
            detail = "new_packet"
        else:
            detail = "boundary_advanced"
        attempt.update(state="CLEANUP_REQUIRED", reason="R6_prime_%s" % detail)
        journal_record(journal_path, "CLEANUP_REQUIRED", attempt,
                       reason=attempt["reason"])
        return attempt
    rc, _, _ = _result(run_tmux(_tmux(
        binding["socket"], ["send-keys", "-t", binding["pane_id"], "Enter"])))
    if rc != 0:
        attempt = transition_attempt(attempt, "submission_uncertain",
                                     now_mono(), cfg)
        attempt["reason"] = "enter_failed"
        journal_record(journal_path, "SUBMISSION_UNCERTAIN", attempt,
                       reason=attempt["reason"])
        return attempt
    attempt = transition_attempt(attempt, "submitted", now_mono(), cfg)
    journal_record(journal_path, "SUBMITTED", attempt)
    wait(1.0)
    after = capture_pane(binding, run_tmux)
    if after is None:
        attempt.update(state="SUBMISSION_UNCERTAIN", reason="submit_unverifiable")
        journal_record(journal_path, "SUBMISSION_UNCERTAIN", attempt,
                       reason=attempt["reason"])
    elif composer_exact(after, text):
        attempt.update(state="SUBMISSION_UNCERTAIN", reason="composer_not_cleared")
        journal_record(journal_path, "SUBMISSION_UNCERTAIN", attempt,
                       reason=attempt["reason"])
    return attempt


def validate_request(path, now=None):
    now = time.time() if now is None else now
    try:
        info = os.lstat(path)
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                info.st_size > REQUEST_MAX_BYTES or now - info.st_mtime > REQUEST_MAX_AGE_S):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(fd, REQUEST_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > REQUEST_MAX_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or not set(value).issubset({"request_id", "reason"}):
            return None
        if set(value) not in ({"request_id"}, {"request_id", "reason"}):
            return None
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
            return None
        if "reason" in value and not isinstance(value["reason"], str):
            return None
        return {"request_id": request_id, "receipt_mtime": info.st_mtime}
    except Exception:
        return None


def request_fingerprint(generation_value, request_id):
    return {"generation": generation_value, "request_id": request_id}


def pending_request_id(request_history, consumed_generations, generation_k):
    """Staleness is judged at FIRST observation only: a request observed
    fresh stays actionable for the generation it was observed in, even if
    its file later ages past the mtime bound or disappears."""
    if generation_k in consumed_generations:
        return None
    pending = sorted(rid for rid, gen_key in request_history.items()
                     if gen_key == generation_k)
    return pending[0] if pending else None


def packet_path(cfg, session_id):
    return cm.state_paths(cfg, session_id)["packet"]


def load_layer1_packet(cfg, session_id):
    return cm.load_packet({"packet": packet_path(cfg, session_id)})


def notify(binding, cfg, paths, attempt, run_tmux=default_run_tmux):
    message = ("compact-manager %s for %s: %s; run compact-manager status"
               % (attempt["state"], binding["session_id"],
                  cm.sanitize(attempt.get("reason", "operator action required"), 120)))
    cm.ledger_append(cfg, {"event": "managed_alert", "state": attempt["state"],
                           "session_id": binding["session_id"],
                           "run_token": binding["run_token"],
                           "reason": attempt.get("reason", "")})
    delivered = []
    try:
        rc, out, _ = _result(run_tmux(_tmux(
            binding["socket"], ["list-clients", "-t",
                                binding.get("tmux_session_id", ""),
                                "-F", "#{client_name}"])))
        if rc == 0:
            for client in [line for line in out.splitlines() if line]:
                drc, _, _ = _result(run_tmux(_tmux(
                    binding["socket"], ["display-message", "-c", client, message])))
                delivered.append({"client": client, "ok": drc == 0})
    except Exception:
        pass
    journal_record(paths["journal"], "ALERT_DELIVERY", attempt,
                   delivered=delivered)


class Watcher:
    def __init__(self, binding, cfg, paths, run_tmux=default_run_tmux,
                 proc_root="/proc", monotonic=time.monotonic,
                 wall=time.time, wait=None):
        self.binding, self.cfg, self.paths = binding, cfg, paths
        self.run_tmux, self.proc_root = run_tmux, proc_root
        self.monotonic, self.wall = monotonic, wall
        self.wait = wait or (lambda seconds: time.sleep(seconds))
        self.stop_requested = False
        self.typed_critical = False
        self.next_heartbeat = monotonic() + HEARTBEAT_S
        self.started = monotonic()
        self.deadline = self.started + cfg["managed_deadline_hours"] * 3600
        self.cursor = load_cursor(paths["scan"])
        self.attempt = recover_attempt(paths["journal"], boot_id(proc_root))
        self.backoff = BACKOFF_INITIAL_S
        self.consumed_request_generations = set()
        self.request_history = {}
        for record in read_journal(paths["journal"]):
            fingerprint = record.get("request_fingerprint")
            observation = record.get("request_observation")
            if isinstance(observation, dict):
                gen = observation.get("generation")
                request_id = observation.get("request_id")
                if isinstance(gen, dict) and isinstance(request_id, str):
                    self.request_history[request_id] = generation_key(gen)
            if not isinstance(fingerprint, dict):
                continue
            gen = fingerprint.get("generation")
            request_id = fingerprint.get("request_id")
            if isinstance(gen, dict) and isinstance(request_id, str):
                key = generation_key(gen)
                self.consumed_request_generations.add(key)
                self.request_history[request_id] = key

    def alert_if_needed(self, previous=None):
        if self.attempt and self.attempt.get("state") in ALERT_STATES and \
                self.attempt.get("state") != previous:
            notify(self.binding, self.cfg, self.paths, self.attempt, self.run_tmux)

    def tick(self):
        now = self.monotonic()
        if self.attempt and self.attempt.get("state") in ALERT_STATES:
            latest = attempt_tail(self.paths["journal"])
            if (latest and latest.get("state") == "LATCHED" and
                    latest.get("reason") == "operator_resolved" and
                    latest.get("attempt_id") == self.attempt.get("attempt_id") and
                    latest.get("run_token") == self.attempt.get("run_token")):
                latest["attempt_packet_seq_floor"] = latest.get(
                    "attempt_packet_seq_floor",
                    latest.get("packet_seq_at_prepare", 0))
                self.attempt = latest
        if now >= self.deadline:
            if self.attempt and self.attempt.get("state") in (
                    "PREPARED", "TYPED_VERIFIED", "SUBMITTED", "ACKED"):
                self.attempt.update(state="CLEANUP_REQUIRED",
                                    reason="watcher_deadline")
                journal_record(self.paths["journal"], "CLEANUP_REQUIRED",
                               self.attempt, reason="watcher_deadline")
                self.alert_if_needed()
            return False, "deadline"
        ok, reason = validate_binding(self.binding, self.cfg, self.paths,
                                      self.run_tmux, self.proc_root)
        if not ok:
            if reason == "foreground_lost" and not (self.attempt and
                    self.attempt.get("state") in
                    ("PREPARED", "TYPED_VERIFIED", "SUBMITTED", "ACKED")):
                return True, "foreground_lost"
            if self.attempt and self.attempt.get("state") in (
                    "PREPARED", "TYPED_VERIFIED", "SUBMITTED", "ACKED"):
                self.attempt.update(state="CLEANUP_REQUIRED", reason=reason)
                journal_record(self.paths["journal"], "CLEANUP_REQUIRED",
                               self.attempt, reason=reason)
                self.alert_if_needed()
            return False, reason
        if now >= self.next_heartbeat:
            try:
                if not heartbeat_leases(self.paths, self.binding["run_token"],
                                        self.wall()):
                    return False, "lease_lost"
            except Exception as exc:
                cm.ledger_append(self.cfg, {"event": "managed_heartbeat_error",
                                           "error": cm.sanitize(repr(exc), 200)})
            self.next_heartbeat = now + HEARTBEAT_S
        old_generation = generation(self.cursor)
        request = validate_request(self.paths["request"], self.wall())
        if request is not None and request["request_id"] not in self.request_history:
            old_key = generation_key(old_generation)
            self.request_history[request["request_id"]] = old_key
            observation_attempt = self.attempt or {
                "run_token": self.binding["run_token"],
                "generation": old_generation,
                "attempt_packet_seq_floor": packet_seq(
                    load_layer1_packet(self.cfg, self.binding["session_id"]))}
            journal_record(
                self.paths["journal"], "REQUEST_OBSERVED",
                observation_attempt,
                request_observation=request_fingerprint(
                    old_generation, request["request_id"]),
                receipt_mtime=request["receipt_mtime"])
        self.cursor = scan_cursor(self.cursor, self.binding["transcript_path"])
        if self.cursor.get("_scan_error"):
            return False, "transcript_unavailable"
        save_cursor(self.paths["scan"], self.cursor)
        current_generation = generation(self.cursor)
        packet = load_layer1_packet(self.cfg, self.binding["session_id"])
        if self.attempt:
            previous = self.attempt.get("state")
            self.attempt = transition_attempt(
                self.attempt, "timer", now, self.cfg, packet,
                current_generation)
            if self.attempt.get("state") != previous:
                journal_record(self.paths["journal"], self.attempt["state"],
                               self.attempt, reason=self.attempt.get("reason"))
                self.alert_if_needed(previous)
            if self.attempt.get("state") in (
                    "CLEANUP_REQUIRED", "SUBMISSION_UNCERTAIN"):
                return True, self.attempt["state"]
            if self.attempt.get("state") == "BOUNDARY_CONFIRMED":
                self.attempt = None
                self.backoff = BACKOFF_INITIAL_S
            elif self.attempt.get("state") == "LATCHED":
                if self.attempt.get("latch_kind") == "THRESHOLD":
                    eff = cm.window_for(self.cfg, self.cursor.get("model"))
                    pct = self.cursor.get("current", 0) / eff["context_window"]
                    if pct < self.cfg["managed_trigger_pct"] - self.cfg["rearm_band_pct"]:
                        self.attempt = transition_attempt(
                            self.attempt, "pct_rearmed", now, self.cfg,
                            generation_value=current_generation)
                        journal_record(self.paths["journal"], "READY", self.attempt)
                        self.attempt = None
                return True, "latched"
            elif self.attempt.get("state") in ("SUBMITTED", "ACKED", "DEFERRED"):
                if self.attempt["state"] != "DEFERRED" or \
                        self.attempt.get("reason") == "foreign_packet" or \
                        now < self.attempt.get("timers", {}).get("next_attempt_at", float("inf")):
                    return True, self.attempt["state"]
                self.attempt["state"] = "TRIGGERED"
            if self.attempt and self.attempt.get("state") == "TRIGGERED":
                previous = "TRIGGERED"
                self.typed_critical = True
                try:
                    self.attempt = run_ladder(
                        self.binding, self.cfg, self.paths, self.attempt,
                        self.cursor, self.paths["journal"],
                        lambda: load_layer1_packet(
                            self.cfg, self.binding["session_id"]),
                        self.run_tmux, self.proc_root, self.wait,
                        self.monotonic)
                finally:
                    self.typed_critical = False
                if self.attempt["state"] == "DEFERRED":
                    self.attempt["timers"]["next_attempt_at"] = now + self.backoff
                    self.backoff = min(BACKOFF_MAX_S, self.backoff * 2)
                    journal_record(self.paths["journal"], "DEFERRED",
                                   self.attempt,
                                   reason=self.attempt.get("reason"))
                self.alert_if_needed(previous)
                if "lease_lost" in str(self.attempt.get("reason", "")):
                    return False, "lease_lost_after_r4"
                return True, self.attempt["state"]
        if not self.cursor.get("caught_up"):
            return True, "catching_up"
        eff = cm.window_for(self.cfg, self.cursor.get("model"))
        pct = self.cursor.get("current", 0) / eff["context_window"]
        req_generation = generation_key(current_generation)
        # One override per generation regardless of request-id churn.  A file
        # observed in an older generation was satisfied by that generation's
        # advance and remains ignored until the model writes a new id.
        request_id = pending_request_id(self.request_history,
                                        self.consumed_request_generations,
                                        req_generation)
        if pct < self.cfg["managed_trigger_pct"] and request_id is None:
            return True, "below_threshold"
        floor = packet_seq(packet)
        # An in-flight packet at creation is foreign unless its boundary is
        # already confirmed by the watcher cursor.
        if packet and not boundary_confirmed_for_packet(packet, self.cursor):
            attempt = new_attempt(self.binding["run_token"], current_generation,
                                  floor, now, boot_id(self.proc_root), "foreign")
            attempt.update(state="DEFERRED", reason="foreign_packet")
            attempt["timers"]["foreign_deadline_mono"] = (
                now + self.cfg["managed_completion_timeout_s"])
            self.attempt = attempt
            journal_record(self.paths["journal"], "DEFERRED", attempt,
                           reason="foreign_packet")
            return True, "foreign_packet"
        trigger = "request" if request_id is not None else "threshold"
        self.attempt = new_attempt(self.binding["run_token"], current_generation,
                                   floor, now, boot_id(self.proc_root), trigger)
        if request_id is not None:
            self.consumed_request_generations.add(req_generation)
            journal_record(self.paths["journal"], "TRIGGERED", self.attempt,
                           request_fingerprint=request_fingerprint(
                               current_generation, request_id))
        else:
            journal_record(self.paths["journal"], "TRIGGERED", self.attempt)
        previous = self.attempt["state"]
        self.typed_critical = True
        try:
            self.attempt = run_ladder(
                self.binding, self.cfg, self.paths, self.attempt, self.cursor,
                self.paths["journal"],
                lambda: load_layer1_packet(self.cfg, self.binding["session_id"]),
                self.run_tmux, self.proc_root, self.wait, self.monotonic)
        finally:
            self.typed_critical = False
        if self.attempt["state"] == "DEFERRED":
            self.attempt["timers"]["next_attempt_at"] = now + self.backoff
            self.backoff = min(BACKOFF_MAX_S, self.backoff * 2)
            journal_record(self.paths["journal"], "DEFERRED", self.attempt,
                           reason=self.attempt.get("reason"))
        self.alert_if_needed(previous)
        if "lease_lost" in str(self.attempt.get("reason", "")):
            return False, "lease_lost_after_r4"
        return True, self.attempt["state"]

    def run(self):
        # READY deliberately precedes the uncapped initial catch-up.
        # "Uncapped" means no total budget: process as many bounded chunks as
        # necessary, keeping the fixed heartbeat alive between chunks.
        while not self.cursor.get("caught_up"):
            if self.stop_requested or self.monotonic() >= self.deadline:
                return
            valid, _ = validate_binding(
                self.binding, self.cfg, self.paths, self.run_tmux,
                self.proc_root)
            if not valid:
                return
            before = (self.cursor.get("file_epoch"), self.cursor.get("offset"))
            self.cursor = scan_cursor(
                self.cursor, self.binding["transcript_path"], SCAN_MAX_BYTES)
            if self.cursor.get("_scan_error"):
                return
            save_cursor(self.paths["scan"], self.cursor)
            now = self.monotonic()
            if now >= self.next_heartbeat:
                try:
                    if not heartbeat_leases(
                            self.paths, self.binding["run_token"], self.wall()):
                        return
                except Exception as exc:
                    cm.ledger_append(
                        self.cfg, {"event": "managed_heartbeat_error",
                                   "error": cm.sanitize(repr(exc), 200)})
                self.next_heartbeat = now + HEARTBEAT_S
            after = (self.cursor.get("file_epoch"), self.cursor.get("offset"))
            if after == before:
                break
        while True:
            keep, reason = self.tick()
            if not keep or (self.stop_requested and not self.typed_critical):
                journal_record(self.paths["journal"], "WATCHER_RETIRED",
                               self.attempt or {"run_token": self.binding["run_token"],
                                                "generation": generation(self.cursor)},
                               reason=reason)
                break
            now = self.monotonic()
            eff = cm.window_for(self.cfg, self.cursor.get("model"))
            pct = self.cursor.get("current", 0) / eff["context_window"]
            interval = self.cfg["managed_poll_s"]
            if pct < self.cfg["managed_trigger_pct"] - 0.20:
                interval = 60
            self.wait(max(0.0, min(interval, self.next_heartbeat - now)))


def _write_handshake(fd, value):
    try:
        os.write(fd, (json.dumps(value) + "\n").encode())
    except OSError:
        pass


def watcher_entry(args):
    cfg = load_config()
    binding = json.loads(args.binding_json)
    run_token = os.urandom(8).hex()
    binding["run_token"] = run_token
    paths = managed_paths(cfg, binding["session_id"], binding["socket"],
                          binding["pane_id"])
    cancel_fd, ready_fd = args.cancel_fd, args.ready_fd
    try:
        readable, _, _ = select.select([cancel_fd], [], [], 0)
        if readable:
            return 1
        live = proc_stat(os.getpid())
        if live is None:
            _write_handshake(ready_fd, {"ok": False, "error": "proc_unreadable"})
            return 1
        ok, detail = acquire_leases(paths, run_token, os.getpid(),
                                    live["starttime"],
                                    ttl_days=cfg.get("state_ttl_days", 7))
        if not ok:
            _write_handshake(ready_fd, {"ok": False, "error": detail})
            return 1
        readable, _, _ = select.select([cancel_fd], [], [], 0)
        if readable:
            release_leases(paths, run_token)
            return 1
        recovered = recover_attempt(paths["journal"], boot_id())
        ready_attempt = {"run_token": run_token,
                         "generation": generation(default_cursor())}
        journal_record(paths["journal"], "WATCHER_READY", ready_attempt,
                       pid=os.getpid(), recovered_state=(recovered or {}).get("state"))
        _write_handshake(ready_fd, {"ok": True, "run_token": run_token,
                                    "pid": os.getpid(),
                                    "recovered_state": (recovered or {}).get("state")})
        watcher = Watcher(binding, cfg, paths)

        def stop(_signum, _frame):
            watcher.stop_requested = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        watcher.alert_if_needed()
        watcher.run()
        release_leases(paths, run_token)
        return 0
    except Exception as exc:
        _write_handshake(ready_fd, {"ok": False,
                                    "error": cm.sanitize(repr(exc), 300)})
        try:
            release_leases(paths, run_token)
        except Exception:
            pass
        return 1
    finally:
        for fd in (cancel_fd, ready_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def spawn_watcher(binding, timeout=10.0):
    """setsid + double fork + exec, with a two-way cancellable handshake."""
    cancel_r, cancel_w = os.pipe()
    ready_r, ready_w = os.pipe()
    for fd in (cancel_r, ready_w):
        os.set_inheritable(fd, True)
    launcher = os.fork()
    if launcher == 0:
        try:
            os.close(cancel_w)
            os.close(ready_r)
            os.setsid()
            child = os.fork()
            if child > 0:
                os._exit(0)
            argv = [sys.executable, os.path.abspath(__file__), "watch",
                    "--binding-json", json.dumps(binding),
                    "--cancel-fd", str(cancel_r), "--ready-fd", str(ready_w)]
            os.execv(sys.executable, argv)
        except Exception:
            os._exit(127)
    os.close(cancel_r)
    os.close(ready_w)
    try:
        os.waitpid(launcher, 0)
        readable, _, _ = select.select([ready_r], [], [], timeout)
        if not readable:
            try:
                os.write(cancel_w, b"C")
            except OSError:
                pass
            try:
                os.killpg(launcher, signal.SIGTERM)
            except OSError:
                pass
            death_deadline = time.monotonic() + 2.0
            while time.monotonic() < death_deadline:
                try:
                    os.killpg(launcher, 0)
                except ProcessLookupError:
                    break
                except OSError:
                    break
                time.sleep(0.05)
            # The parent knows the binding and can conditionally clean only a
            # lease whose recorded watcher is now dead.  Token and inode are
            # re-read under the transaction by release_leases.
            cfg = load_config()
            paths = managed_paths(cfg, binding["session_id"],
                                  binding["socket"], binding["pane_id"])
            cleanup_deadline = time.monotonic() + 1.0
            while time.monotonic() < cleanup_deadline:
                lease, _ = read_json_inode(paths["session_lease"])
                if not lease:
                    break
                if not proc_matches(lease.get("pid"), lease.get("proc_start")):
                    release_leases(paths, lease.get("run_token"))
                    break
                time.sleep(0.05)
            return {"ok": False, "error": "watcher handshake timeout"}
        raw = os.read(ready_r, 65_536)
        return json.loads(raw.splitlines()[0])
    except Exception as exc:
        return {"ok": False, "error": cm.sanitize(repr(exc), 300)}
    finally:
        os.close(cancel_w)
        os.close(ready_r)


def _all_session_leases(cfg):
    directory = os.path.join(cfg["state_dir"], "managed", "leases")
    try:
        names = sorted(n for n in os.listdir(directory)
                       if n.startswith("session-") and n.endswith(".json"))
    except OSError:
        return []
    rows = []
    for name in names:
        value, _ = read_json_inode(os.path.join(directory, name))
        if value:
            rows.append((name[8:-5], value))
    return rows


def status_rows(cfg):
    leases = dict(_all_session_leases(cfg))
    watcher_dir = os.path.join(cfg["state_dir"], "managed", "watchers")
    try:
        journal_sids = {name[:-len(".journal.jsonl")]
                        for name in os.listdir(watcher_dir)
                        if name.endswith(".journal.jsonl")}
    except OSError:
        journal_sids = set()
    rows = []
    for sid in sorted(set(leases) | journal_sids):
        lease = leases.get(sid, {})
        paths = managed_paths(cfg, sid)
        # The pane hash is unavailable from a session-only listing; status is
        # lease/journal oriented and does not guess a pane binding.
        tail = attempt_tail(paths["journal"])
        rows.append({"session_id": sid, "run_token": lease.get("run_token"),
                     "pid": lease.get("pid"),
                     "live": bool(lease) and lease_is_live(lease),
                     "state": (tail or {}).get("state", "WATCHER_READY"),
                     "reason": (tail or {}).get("reason")})
    return rows


def stop_session(cfg, sid, timeout=10.0, proc_root="/proc"):
    paths = managed_paths(cfg, sid)
    with txn_lock(paths["txn"]):
        lease, inode = read_json_inode(paths["session_lease"])
        if not lease:
            return False, "no live lease"
        token, pid, start = lease.get("run_token"), lease.get("pid"), lease.get("proc_start")
        if not proc_matches(pid, start, proc_root):
            return False, "lease owner is not live"
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            pfd = os.pidfd_open(pid)
            try:
                # The pidfd pins one process identity, so re-verifying the
                # starttime after the open closes the pid-recycle window
                # race-free; signalling an unverified pidfd would not.
                if not proc_matches(pid, start, proc_root):
                    return False, "pid changed before signal"
                signal.pidfd_send_signal(pfd, signal.SIGTERM)
            finally:
                os.close(pfd)
        else:
            if not proc_matches(pid, start, proc_root):
                return False, "pid changed before signal"
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, str(exc)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value, _ = read_json_inode(paths["session_lease"])
        if not value or value.get("run_token") != token:
            return True, "stopped"
        time.sleep(0.1)
    return False, "stop timed out; lease retained"


def resolve_session(cfg, sid):
    paths = managed_paths(cfg, sid)
    with txn_lock(paths["txn"]):
        tail = attempt_tail(paths["journal"])
        if tail is None or tail.get("state") not in ALERT_STATES:
            return False, "nothing resolvable"
        token = tail.get("run_token")
        lease, _ = read_json_inode(paths["session_lease"])
        if lease is None:
            return False, "session lease is unreadable"
        lease_dir = os.path.dirname(paths["session_lease"])
        matching_panes = []
        try:
            pane_names = [name for name in os.listdir(lease_dir)
                          if name.startswith("pane-") and name.endswith(".json")]
        except OSError:
            pane_names = []
        for name in pane_names:
            pane, _ = read_json_inode(os.path.join(lease_dir, name))
            if pane is None:
                return False, "pane lease is unreadable"
            if pane and pane.get("run_token") == token:
                matching_panes.append(pane)
        if lease and lease_is_live(lease):
            if lease.get("run_token") != token:
                return False, "alert belongs to another run token"
            if len(matching_panes) != 1 or not lease_is_live(matching_panes[0]):
                return False, "live watcher does not hold both leases"
        else:
            # No live session lease: dead-token cleanup authority exists only
            # when every surviving lease for that token is also reclaimable.
            if any(lease_is_live(pane) for pane in matching_panes):
                return False, "pane lease is still live"
            if lease and lease.get("run_token") == token and lease_is_live(lease):
                return False, "session lease is still live"
        cursor = load_cursor(paths["scan"])
        resolved = dict(tail, state="LATCHED", latch_kind="SAFETY",
                        reason="operator_resolved",
                        generation=generation(cursor))
        journal_record(paths["journal"], "LATCHED", resolved,
                       latch_kind="SAFETY", reason="operator_resolved")
        return True, "resolved to safety latch"


def _cli_binding(socket, pane, attended, run_tmux=default_run_tmux,
                 wait_s=0):
    cfg = load_config()
    deadline = time.monotonic() + wait_s
    while True:
        binding, error = build_binding(socket, pane, attended, run_tmux)
        if binding is not None or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if binding is None:
        return None, "binding refused: %s" % error
    return binding, None


def cli_main(argv=None, run_tmux=default_run_tmux):
    parser = argparse.ArgumentParser(prog="compact-manager")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--session-name")
    start.add_argument("command_argv", nargs=argparse.REMAINDER)
    adopt = sub.add_parser("adopt")
    adopt.add_argument("-S", dest="socket", default="")
    adopt.add_argument("-t", dest="pane", required=True)
    adopt.add_argument("--attended", action="store_true")
    sub.add_parser("status")
    stop = sub.add_parser("stop")
    stop.add_argument("sid")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("sid")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.command == "start":
        command = list(args.command_argv)
        if command and command[0] == "--":
            command.pop(0)
        if not command or os.path.basename(command[0]) != "claude":
            parser.error("start requires -- claude <args…>")
        tmux_argv = ["new-session", "-d", "-P", "-F", "#{pane_id}"]
        if args.session_name:
            tmux_argv += ["-s", args.session_name]
        tmux_argv += command  # multiple argv: tmux execs directly, no shell
        rc, out, err = _result(run_tmux(tmux_argv))
        if rc != 0:
            print("compact-manager: tmux start failed: %s" % cm.sanitize(err),
                  file=sys.stderr)
            return 1
        pane = out.strip().splitlines()[-1]
        binding, error = _cli_binding("", pane, False, run_tmux, wait_s=10)
        if error:
            print("compact-manager: %s" % error, file=sys.stderr)
            return 1
        result = spawn_watcher(binding)
    elif args.command == "adopt":
        if not args.attended:
            print("compact-manager: adopt requires --attended\n" +
                  ADOPT_ACKNOWLEDGEMENT, file=sys.stderr)
            return 2
        print(ADOPT_ACKNOWLEDGEMENT, file=sys.stderr)
        binding, error = _cli_binding(args.socket, args.pane, True, run_tmux)
        if error:
            print("compact-manager: %s" % error, file=sys.stderr)
            return 1
        result = spawn_watcher(binding)
    elif args.command == "status":
        rows = status_rows(cfg)
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    elif args.command == "stop":
        tail = attempt_tail(managed_paths(cfg, args.sid)["journal"])
        if tail and tail.get("state") in ALERT_STATES:
            print("ALERT %s: %s" % (tail["state"],
                                    tail.get("reason", "operator action required")))
        ok, message = stop_session(cfg, args.sid)
        print(message)
        return 0 if ok else 1
    else:
        ok, message = resolve_session(cfg, args.sid)
        print(message)
        return 0 if ok else 1
    if result.get("ok"):
        print("watcher pid=%s run_token=%s%s" % (
            result["pid"], result["run_token"],
            (" recovered=%s" % result["recovered_state"]
             if result.get("recovered_state") else "")))
        return 0
    print("compact-manager: watcher failed: %s" % result.get("error"),
          file=sys.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch")
    watch.add_argument("--binding-json", required=True)
    watch.add_argument("--cancel-fd", type=int, required=True)
    watch.add_argument("--ready-fd", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "watch":
        return watcher_entry(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
