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
    """Return proc(5) state(3), tpgid(field 8) and starttime(field 22)."""
    try:
        fields = text[text.rindex(")") + 2:].split()
        return {"state": fields[0], "tpgid": int(fields[5]),
                "starttime": int(fields[19])}
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
    """Derive the transcript PATH (containment-checked). The file may not
    exist yet — a virgin session writes it on its first turn, and start
    binds before any turn; the watcher waits (transcript_pending, bounded
    by its deadline) and can never inject until a real scan catches up."""
    projects = os.path.realpath(projects_dir or
                                os.path.expanduser("~/.claude/projects"))
    # Claude Code's project slug maps every non-alphanumeric character to
    # "-" (verified live: /tmp/cm_slug.test -> -tmp-cm-slug-test), not just
    # the path separators.
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    target = os.path.realpath(os.path.join(projects, slug,
                                           "%s.jsonl" % session_id))
    if not target.startswith(projects + os.sep):
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
            "anchor": None, "model": "", "discard_to_newline": False,
            "trailing_fragment": False, "caught_up": False,
            "_scan_error": False, "_scan_missing": False}


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
            "current", "boundary_count", "last_boundary", "anchor", "model",
            "discard_to_newline")
    _atomic_json(path, {key: cursor.get(key) for key in keys})


def _row_hash(raw):
    return hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()


def _hash_at(path, row):
    try:
        with open(path, "rb") as fh:
            fh.seek(int(row["offset"]))
            if "sha256_of_prefix" in row:
                # Prefix anchor from the oversized-row escape: no complete
                # row exists to hash, so identity is a bounded prefix.
                raw = fh.read(int(row.get("prefix_len", 4096)))
                return (hashlib.sha256(raw).hexdigest() ==
                        row["sha256_of_prefix"])
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
    cursor = dict(cursor, _scan_error=False, _scan_missing=False)
    try:
        snapshot = os.stat(transcript)
        if not stat.S_ISREG(snapshot.st_mode):
            return dict(cursor, caught_up=False, _scan_error=True)
    except FileNotFoundError:
        # Not-yet-created (virgin session) is a WAIT, not a failure.
        return dict(cursor, caught_up=False, _scan_error=True,
                    _scan_missing=True)
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
    if cursor.get("discard_to_newline"):
        # Mid-skip of a row larger than the budget: drop bytes to the
        # next newline, then resume normal parsing (Layer 1's escape).
        if cursor.get("anchor") is None and data:
            cursor["anchor"] = {"offset": start, "prefix_len": min(4096, len(data)),
                                "sha256_of_prefix":
                                    hashlib.sha256(data[:4096]).hexdigest()}
        nl = data.find(b"\n")
        if nl < 0:
            cursor["offset"] = start + len(data)
            cursor["observed_size"] = snapshot.st_size
            cursor["trailing_fragment"] = True
            cursor["caught_up"] = False
            return cursor
        cursor["discard_to_newline"] = False
        start += nl + 1
        data = data[nl + 1:]
    last_nl = data.rfind(b"\n")
    if last_nl < 0 and len(data) >= budget:
        # A single row larger than the whole budget can never complete;
        # without this escape the offset would never advance and the
        # cursor would stay uncaught-up forever (managed mode disabled).
        # A skipped span still needs an identity anchor, or a same-size
        # truncate-and-regrow behind the offset would go undetected.
        if cursor.get("anchor") is None:
            cursor["anchor"] = {"offset": start, "prefix_len": min(4096, len(data)),
                                "sha256_of_prefix":
                                    hashlib.sha256(data[:4096]).hexdigest()}
        cursor["offset"] = start + len(data)
        cursor["observed_size"] = snapshot.st_size
        cursor["discard_to_newline"] = True
        cursor["trailing_fragment"] = True
        cursor["caught_up"] = False
        return cursor
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
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        # A previously torn tail (no trailing newline) would swallow THIS
        # record into one unparseable line; delimit it first. The torn
        # fragment stays ignored as invalid JSON.
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            os.write(fd, b"\n")
        # A short write would leave a torn record that recovery ignores,
        # making the durable tail LESS conservative than the action about
        # to be taken; write must complete fully or raise.
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short journal write")
            view = view[written:]
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
        "latch_kind": attempt.get("latch_kind"),
        "latch_tokens": attempt.get("latch_tokens"),
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
        stored = timers.get("boot_id")
        # "unknown" is the boot_id() failure sentinel: an unproven boot
        # identity must never count as proven same-boot ("unknown" ==
        # "unknown" would continue stale monotonic timers post-reboot).
        unproven = (not stored or stored == "unknown" or
                    not current_boot_id or current_boot_id == "unknown")
        if unproven or stored != current_boot_id:
            recovered["state"] = "LATCHED"
            recovered["latch_kind"] = "SAFETY"
            recovered["reason"] = ("unproven_boot_timer" if unproven
                                   else "cross_boot_timer")
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


def _composer_line(capture):
    # capture-pane -J (required to re-join wrapped lines) PRESERVES
    # trailing spaces, so the live composer captures as "❯"+NBSP+padding
    # (verified live in the gate suite). Strip trailing ASCII spaces/CR
    # only — the NBSP signature stays significant, so a shell's bare
    # "❯ " prompt still fails the idle check.
    lines = [line.rstrip("\r ") for line in capture.splitlines()
             if line.startswith("❯")]
    return lines[-1] if lines else None


def composer_idle(capture):
    if "esc to interrupt" in capture.lower():
        return False
    return _composer_line(capture) == "❯\u00a0"


def composer_exact(capture, text):
    return _composer_line(capture) == "❯\u00a0" + text


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
    # R6' re-OBSERVES the transcript rather than trusting the tick's
    # cursor (which is stale by the whole ladder duration), and uses the
    # full packet classification: an auto-trigger packet is foreign even
    # after a Layer-1 seq reset, and a late own-nonce packet also means
    # do-not-press-Enter (our retry text is typed but the first
    # submission already landed). A scan error means the boundary state
    # is unprovable — same abort.
    classification = packet_classification(attempt, packet)
    fresh = scan_cursor(dict(cursor), binding["transcript_path"])
    # The rescan must COMPLETE: caught_up=False means bytes beyond the
    # budget were not examined and could hold a boundary — an explicitly
    # incomplete view must not authorize Enter.
    generation_unchanged = (not fresh.get("_scan_error") and
                            fresh.get("caught_up") and
                            generation_key(generation(fresh)) ==
                            generation_key(attempt["generation"]))
    if (not ok or capture is None or not composer_exact(capture, text) or
            classification != "NONE" or not generation_unchanged):
        if not ok:
            detail = str(why)
        elif capture is None or not composer_exact(capture, text):
            detail = "composer_mismatch"
        elif classification == "OWN":
            detail = "own_packet_late"
        elif classification == "FOREIGN":
            detail = "foreign_packet"
        elif fresh.get("_scan_error"):
            detail = "scan_unavailable"
        elif not fresh.get("caught_up"):
            detail = "scan_incomplete"
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
    # display-message expands #{...} formats; "##" renders a literal "#".
    message = message.replace("#", "##")
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
        if not (self.attempt and self.attempt.get("state") in ALERT_STATES and
                self.attempt.get("state") != previous):
            return
        # THRESHOLD latches are informational hysteresis, not operator
        # hazards; alerting them would re-fire on every native boundary
        # while pct stays above the re-arm band.
        if (self.attempt.get("state") == "LATCHED" and
                self.attempt.get("latch_kind") == "THRESHOLD"):
            return
        notify(self.binding, self.cfg, self.paths, self.attempt, self.run_tmux)

    def _transient(self, reason):
        """Validation failures that mean WAIT, not retire: job-control
        suspends, tmux copy-mode, and a pane hiccup while the bound
        claude is provably still alive. Everything else (tty change,
        command change, claude death, lease loss) retires."""
        if reason in ("foreground_lost", "pane_in_mode"):
            return True
        if reason != "pane_missing":
            return False
        # A zombie claude is dead for this purpose — hot-waiting on an
        # exited-but-unreaped process would spin until the deadline.
        live = proc_stat(self.binding["claude_pid"], self.proc_root)
        try:
            return (live is not None and live.get("state") != "Z" and
                    live["starttime"] == int(self.binding["claude_start"]))
        except (TypeError, ValueError, OverflowError):
            return False

    def _defer_triggered(self, reason, now):
        """A pending pre-typing attempt must not stay durably TRIGGERED
        across a transient: journal the defer once."""
        if self.attempt and self.attempt.get("state") == "TRIGGERED":
            self.attempt["state"] = "DEFERRED"
            self.attempt["reason"] = reason
            self.attempt.setdefault("timers", {})["next_attempt_at"] = (
                now + self.backoff)
            journal_record(self.paths["journal"], "DEFERRED",
                           self.attempt, reason=reason)

    def _journal_typed_hazard(self, reason):
        """Retiring with a typed-state attempt leaves bytes of unproven
        disposition; the durable tail must say so and alert."""
        if self.attempt and self.attempt.get("state") in (
                "PREPARED", "TYPED_VERIFIED", "SUBMITTED", "ACKED"):
            self.attempt.update(state="CLEANUP_REQUIRED", reason=reason)
            journal_record(self.paths["journal"], "CLEANUP_REQUIRED",
                           self.attempt, reason=reason)
            self.alert_if_needed()

    def _heartbeat_due(self, now):
        """Beat both leases if due; False means ownership was lost."""
        if now >= self.next_heartbeat:
            try:
                if not heartbeat_leases(self.paths, self.binding["run_token"],
                                        self.wall()):
                    return False
            except Exception as exc:
                cm.ledger_append(self.cfg, {"event": "managed_heartbeat_error",
                                            "error": cm.sanitize(repr(exc), 200)})
            self.next_heartbeat = now + HEARTBEAT_S
        return True

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
            self._journal_typed_hazard("watcher_deadline")
            return False, "deadline"
        ok, reason = validate_binding(self.binding, self.cfg, self.paths,
                                      self.run_tmux, self.proc_root)
        if not ok:
            typed = (self.attempt and self.attempt.get("state") in
                     ("PREPARED", "TYPED_VERIFIED", "SUBMITTED", "ACKED"))
            if self._transient(reason) and not typed:
                # Heartbeats must survive a long transient (copy mode can
                # last minutes); a starved lease invites reclaim races.
                if not self._heartbeat_due(now):
                    return False, "lease_lost"
                self._defer_triggered(reason, now)
                return True, reason
            self._journal_typed_hazard(reason)
            return False, reason
        if not self._heartbeat_due(now):
            return False, "lease_lost"
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
        if self.cursor.get("_scan_missing"):
            return True, "transcript_pending"
        if self.cursor.get("_scan_error"):
            self._journal_typed_hazard("transcript_unavailable")
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
                self.backoff = BACKOFF_INITIAL_S
                eff = cm.window_for(self.cfg, self.cursor.get("model"))
                pct = self.cursor.get("current", 0) / eff["context_window"]
                if pct >= (self.cfg["managed_trigger_pct"] -
                           self.cfg["rearm_band_pct"]):
                    # The completed compaction left the session at/near the
                    # trigger; re-firing immediately would loop. THRESHOLD
                    # latch until meaningful NEW content accumulates (the
                    # LATCHED branch above clears it).
                    self.attempt = transition_attempt(
                        self.attempt, "threshold_latch", now, self.cfg)
                    self.attempt["generation"] = current_generation
                    # The attempt's injection lifecycle is OVER: its own
                    # already-consumed packet must not re-ACK the latch, so
                    # drop the nonce identity and rebase the packet floor.
                    self.attempt["nonces"] = []
                    self.attempt["nonce"] = ""
                    self.attempt["attempt_packet_seq_floor"] = packet_seq(packet)
                    self.attempt["latch_tokens"] = self.cursor.get("current", 0)
                    journal_record(self.paths["journal"], "LATCHED",
                                   self.attempt, latch_kind="THRESHOLD",
                                   reason="still_above_rearm_band")
                    return True, "latched"
                self.attempt = None
            elif self.attempt.get("state") == "LATCHED":
                if self.attempt.get("latch_kind") == "THRESHOLD":
                    eff = cm.window_for(self.cfg, self.cursor.get("model"))
                    window = eff["context_window"]
                    pct = self.cursor.get("current", 0) / window
                    # Context only grows between compactions, so a pure
                    # pct-drop re-arm would latch managed mode forever
                    # after one compaction. Re-arm when meaningful NEW
                    # content accumulated past the latch point (or on a
                    # genuine pct drop) — each re-fire then costs a full
                    # re-arm band of real work, so no compaction churn.
                    grown = (self.cursor.get("current", 0) -
                             self.attempt.get("latch_tokens", 0))
                    if (pct < (self.cfg["managed_trigger_pct"] -
                               self.cfg["rearm_band_pct"]) or
                            grown >= self.cfg["rearm_band_pct"] * window):
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
            if self.stop_requested:
                return
            now = self.monotonic()
            if now >= self.deadline:
                self._journal_typed_hazard("watcher_deadline")
                return
            valid, why = validate_binding(
                self.binding, self.cfg, self.paths, self.run_tmux,
                self.proc_root)
            if not valid:
                if not self._transient(why):
                    # Same contract as tick(): retiring with a recovered
                    # typed-state attempt must leave a durable hazard.
                    self._journal_typed_hazard(why)
                    return
                if not self._heartbeat_due(now):
                    return
                self._defer_triggered(why, now)
                self.wait(min(self.cfg["managed_poll_s"], HEARTBEAT_S))
                continue
            before = (self.cursor.get("file_epoch"), self.cursor.get("offset"))
            self.cursor = scan_cursor(
                self.cursor, self.binding["transcript_path"], SCAN_MAX_BYTES)
            if self.cursor.get("_scan_missing"):
                # Virgin session: wait for the first turn to create the
                # transcript (deadline-bounded; claude death retires).
                if not self._heartbeat_due(self.monotonic()):
                    return
                self.wait(min(self.cfg["managed_poll_s"], HEARTBEAT_S))
                continue
            if self.cursor.get("_scan_error"):
                self._journal_typed_hazard("transcript_unavailable")
                return
            save_cursor(self.paths["scan"], self.cursor)
            if not self._heartbeat_due(self.monotonic()):
                return
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
            # Floor keeps an overdue heartbeat from degenerating into a
            # zero-sleep hot loop.
            self.wait(max(0.5, min(interval, self.next_heartbeat - now)))


def _write_handshake(fd, value):
    try:
        os.write(fd, (json.dumps(value) + "\n").encode())
    except OSError:
        pass


def watcher_entry(args):
    cfg = load_config()
    binding = json.loads(args.binding_json)
    # The parent generated the token before the spawn so that its
    # handshake-timeout cleanup can conditionally remove exactly this
    # child's leases and no other watcher's. Fall back only if absent.
    run_token = binding.get("run_token") or ""
    if not re.match(r"^[0-9a-f]{16}$", run_token):
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
        # A synthesized conservative mapping (PREPARED->CLEANUP_REQUIRED,
        # TYPED_VERIFIED->SUBMISSION_UNCERTAIN, unproven/cross-boot->
        # LATCHED) must be DURABLE before readiness: otherwise the raw
        # tail keeps claiming the less conservative state to status,
        # resolve, and any later recovery.
        raw_tail = attempt_tail(paths["journal"])
        if (recovered is not None and raw_tail is not None and
                recovered.get("state") != raw_tail.get("state")):
            journal_record(paths["journal"], recovered["state"], recovered,
                           reason=recovered.get("reason"))
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
    binding = dict(binding, run_token=os.urandom(8).hex())
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
            # Daemon fd hygiene: the watcher must NOT inherit the CLI's
            # stdio — a caller capturing our output (command substitution,
            # a pipe) would otherwise block until the watcher exits, hours
            # later. The handshake uses its own dedicated pipe fds.
            devnull = os.open(os.devnull, os.O_RDWR)
            for stdio in (0, 1, 2):
                os.dup2(devnull, stdio)
            if devnull > 2:
                os.close(devnull)
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
            # Cleanup may remove ONLY the token this parent generated for
            # its own child — never a token it merely observed in a lease
            # file, which could belong to a live successor (the
            # loser-deletes-successor hole). release_leases re-verifies
            # the token under the transaction; any other lease is left
            # for conservative staleness-based reclaim.
            cfg = load_config()
            paths = managed_paths(cfg, binding["session_id"],
                                  binding["socket"], binding["pane_id"])
            release_leases(paths, binding["run_token"])
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
    current_boot = boot_id()
    for sid in sorted(set(leases) | journal_sids):
        lease = leases.get(sid, {})
        paths = managed_paths(cfg, sid)
        # The pane hash is unavailable from a session-only listing; status is
        # lease/journal oriented and does not guess a pane binding. The
        # displayed state is the CONSERVATIVE recovery mapping of the raw
        # tail (a crashed watcher's PREPARED tail is a cleanup hazard, and
        # must be shown as one even before any re-adopt persists it).
        tail = recover_attempt(paths["journal"], current_boot)
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
        # Judge resolvability on the conservative recovery mapping, not
        # the raw tail: a crashed watcher's PREPARED/TYPED_VERIFIED tail
        # IS the hazard the operator is resolving.
        current_boot = boot_id()
        tail = recover_attempt(paths["journal"], current_boot)
        if tail is None or tail.get("state") not in ALERT_STATES:
            return False, "nothing resolvable"
        token = tail.get("run_token")
        lease, _ = read_json_inode(paths["session_lease"])
        if lease is None:
            return False, "session lease is unreadable"
        raw = attempt_tail(paths["journal"])
        if (current_boot == "unknown" and raw is not None and
                raw.get("state") != tail.get("state") and
                lease and lease_is_live(lease)):
            # The escalation to a resolvable state rests on an UNPROVEN
            # boot identity while a watcher may be live and mid-attempt;
            # resolving over it could clear a real in-flight submission.
            return False, "boot identity unproven; stop the watcher first"
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
        # The human has dispositioned this attempt; a LATE own-nonce
        # packet must not resurrect it to ACKED, so the resolved latch
        # drops the nonce identity.
        resolved = dict(tail, state="LATCHED", latch_kind="SAFETY",
                        reason="operator_resolved", nonce="", nonces=[],
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


def overview_text(cfg, now=None):
    """Deterministic status report: config, watcher rows (with the
    attention flags status.md used to ask the model to derive), and
    per-session usage from the advisor's state files. The slash
    command relays this instead of re-deriving the logic in prose."""
    now = time.time() if now is None else now
    lines = []
    overrides = ", ".join(
        "%s→%s" % (k, v.get("context_window"))
        for k, v in sorted(cfg.get("models", {}).items())) or "none"
    lines.append("mode=%s window=%s model-overrides: %s"
                 % (cfg["mode"], cfg["context_window"], overrides))
    rows = status_rows(cfg)
    lines.append("watchers: %d" % len(rows))
    for r in rows:
        flags = []
        if r["state"] in ALERT_STATES:
            flags.append("ATTENTION")
        if not r["live"] and r["state"] != "WATCHER_RETIRED":
            flags.append("DEAD-LEASE")
        if r["live"] and r.get("pid") is None:
            flags.append("MALFORMED-LEASE")
        lines.append("  %s pid=%s state=%s live=%s reason=%s%s" % (
            r["session_id"], r["pid"], r["state"], r["live"],
            r["reason"], ("  <-- " + ",".join(flags)) if flags else ""))
    state_dir = os.path.join(cfg["state_dir"], "state")
    entries = []
    try:
        names = os.listdir(state_dir)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(state_dir, name)
        try:
            mtime = os.path.getmtime(path)
            if now - mtime > 86400:
                continue
            with open(path) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(st, dict):
            entries.append((mtime, name[:-5], st))
    entries.sort(key=lambda e: e[0], reverse=True)
    lines.append("sessions (state files touched in the last 24h): %d"
                 % len(entries))
    for i, (mtime, sid, st) in enumerate(entries):
        # Degrade per-row, never lose the whole readout to one bad
        # state file (verify-round finding).
        try:
            window = cm.window_for(cfg, st.get("model"))["context_window"]
            current = st.get("current")
            current = current if isinstance(current, (int, float)) \
                and not isinstance(current, bool) else 0
            peak = st.get("peak")
            peak = peak if isinstance(peak, (int, float)) \
                and not isinstance(peak, bool) else 0
            lines.append(
                "  %s%s model=%s current=%s peak=%s window=%s pct=%.1f%%"
                % ("CURRENT>> " if i == 0 else "          ", sid,
                   st.get("model") or "?", current, peak, window,
                   100.0 * current / window))
        except Exception:
            lines.append("  %s%s (unreadable state row)" % (
                "CURRENT>> " if i == 0 else "          ", sid))
    if entries:
        lines.append("(CURRENT>> = most recently updated state file; the "
                     "advisor touches it on every tool call, so this is "
                     "almost certainly the invoking session)")
    return "\n".join(lines)


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
    sub.add_parser("overview")
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
        # 30s: a cold claude start on a loaded machine can take >10s to
        # write its registry entry (observed live).
        binding, error = _cli_binding("", pane, False, run_tmux, wait_s=30)
        if error:
            print("compact-manager: %s" % error, file=sys.stderr)
            print("compact-manager: the tmux session is left running "
                  "(pane %s). A startup dialog (e.g. folder trust) blocks "
                  "the session registry: attach, complete it, then use "
                  "'adopt -t %s --attended'." % (pane, pane),
                  file=sys.stderr)
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
    elif args.command == "overview":
        print(overview_text(cfg))
        return 0
    elif args.command == "stop":
        tail = recover_attempt(managed_paths(cfg, args.sid)["journal"],
                               boot_id())
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
