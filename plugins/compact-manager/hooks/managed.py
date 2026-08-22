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
LIFECYCLE_STATES = {"WATCHER_READY", "ALERT_DELIVERY", "WATCHER_RETIRED",
                    "STARVATION_ALERT"}
STARVATION_ALERT_S = 120.0
INSTRUCTION_TEMPLATE = ("/compact [cm-{nonce}] Preserve the task list and "
                        "open decisions to the handoff file")
INSTRUCTION_TEMPLATE_SHORT = "/compact [cm-{nonce}]"
# Columns kept free at the right edge when deciding whether the full
# instruction can render unwrapped (the composer pads before wrapping).
WRAP_MARGIN_COLS = 4
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

    # Per-model trigger overrides. cm.load_config's models cleaner only
    # keeps its own keys (and drops patterns left empty), so re-read the
    # raw models map — `env or file`, byte-for-byte the same precedence
    # expression as cm's, so an empty env var falls through to the file
    # in both loaders. Kept in a SEPARATE map (never grafted onto
    # cfg["models"]) so a trigger-only pattern cannot shadow another
    # pattern's window/soft/hard in window_for's single longest-match
    # merge. Range-validated here; the soft<trigger ordering is checked
    # per-model in trigger_for.
    raw_models = env.get("COMPACT_MANAGER_MODELS") or raw_file.get("models")
    if isinstance(raw_models, str):
        try:
            raw_models = json.loads(raw_models)
        except ValueError:
            raw_models = None
    triggers = {}
    if isinstance(raw_models, dict):
        for pat, overrides in raw_models.items():
            if not (isinstance(pat, str) and pat.strip()
                    and isinstance(overrides, dict)):
                continue
            value = _finite_number(overrides.get("managed_trigger_pct"))
            if value is not None and 0 < value <= 1.0:
                triggers[pat] = value
    cfg["managed_model_triggers"] = triggers

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


def trigger_for(cfg, model):
    """Effective managed trigger for a model id. A per-model override
    (longest matching pattern, same matching rule as window_for) wins
    when it sits above the model's EFFECTIVE soft; otherwise the global
    trigger applies exactly as load_config validated it — so a config
    with no per-model triggers behaves identically to before this knob
    existed, even when a per-model soft override sits above the global
    trigger. Bare-dict callers (no load_config) fall back to effective
    hard, matching the loader's own default."""
    eff = cm.window_for(cfg, model)
    triggers = cfg.get("managed_model_triggers") or {}
    name = str(model or "").lower()
    best = ""
    for pat in triggers:
        if len(pat) > len(best) and pat.lower() in name:
            best = pat
    if best:
        value = triggers[best]
        if eff["soft_pct"] < value <= 1.0:
            return value
    value = _finite_number(cfg.get("managed_trigger_pct"))
    if value is not None and 0 < value <= 1.0:
        return value
    return eff["hard_pct"]


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
        # Cleanup authority needs a real token BEFORE anything is
        # deleted: a malformed tokenless lease must stay untouched, not
        # have its artifacts removed and then KeyError out of
        # acquire_leases, poisoning every later adoption (Sol round-3
        # blocker). It would also make token-less pane leases "match".
        token = lease.get("run_token")
        if not isinstance(token, str) or not token:
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
        # Zero matches is reclaimable too: the fresh-state deferral
        # below can hold a dead pair across a pane reuse, whose
        # acquisition overwrites the pane lease with a new token — the
        # session lease would then be orphaned forever if exactly one
        # match were required (Sol round-2). More than one match, or
        # any unreadable pane lease, stays ambiguous and untouched.
        if ambiguous or len(matches) > 1 or (
                matches and lease_is_live(matches[0][1], now, proc_root)):
            continue
        # Retention age keys off the last heartbeat, not merely an old scan.
        heartbeat = _finite_number(lease.get("heartbeat_at"))
        if heartbeat is None or heartbeat >= cutoff:
            continue
        sid = name[len("session-"):-len(".json")]
        # A session still on the overview (state file touched <24h, the
        # window overview_data uses) keeps its journal even though its
        # watcher is long dead: deleting it would flip a real fired-
        # compacts count to null ("never watched") mid-display (Sol
        # round-1). Cleanup just waits until the session ages out.
        try:
            state_mtime = os.path.getmtime(
                os.path.join(cfg["state_dir"], "state", sid + ".json"))
            if now - state_mtime <= 86_400:
                continue
        except OSError:
            pass
        session_paths = managed_paths(cfg, sid)
        for artifact in (session_paths["journal"], session_paths["scan"],
                         session_paths["request"]):
            try:
                info = os.lstat(artifact)
                if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    os.unlink(artifact)
            except OSError:
                pass
        if matches:
            conditional_remove(matches[0][0], token,
                               matches[0][2], locked=True)
        conditional_remove(session_path, token, session_inode,
                           locked=True)
    # Journal/scan/request files whose session lease is already gone
    # (clean retirement releases both leases; the fresh-state deferral
    # above can also outlive its lease) would otherwise accumulate
    # forever — nothing ever revisits them once the lease loop can't
    # see them (Sol round-3). Sweep them once their own mtime passes
    # the same TTL and the session has aged off the overview. The
    # session being acquired stays untouched: its journal tail may
    # require recovery.
    exclude_sid = ""
    if exclude_session_lease:
        base_name = os.path.basename(exclude_session_lease)
        if base_name.startswith("session-") and base_name.endswith(".json"):
            exclude_sid = base_name[len("session-"):-len(".json")]
    for directory, suffix in (
            (os.path.join(cfg["state_dir"], "managed", "watchers"),
             ".journal.jsonl"),
            (os.path.join(cfg["state_dir"], "managed", "watchers"),
             ".scan.json"),
            (os.path.join(cfg["state_dir"], "managed", "requests"),
             ".json")):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith(suffix):
                continue
            sid = name[:len(name) - len(suffix)]
            if (not sid or sid == exclude_sid or
                    ("session-%s.json" % sid) in session_names):
                continue
            path = os.path.join(directory, name)
            try:
                before = os.lstat(path)
                if stat.S_ISLNK(before.st_mode) or \
                        not stat.S_ISREG(before.st_mode):
                    continue
                if before.st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            try:
                state_mtime = os.path.getmtime(os.path.join(
                    cfg["state_dir"], "state", sid + ".json"))
                if now - state_mtime <= 86_400:
                    continue
            except OSError:
                pass  # no state file — nothing keeps the artifact alive
            try:
                # Re-prove identity at the last instant: the advisor's
                # atomic replace and the model's request writes are NOT
                # under this txn lock, so the pathname may now hold a
                # FRESH file (Sol round-4). A changed inode or mtime
                # means eligibility was judged on a different file —
                # leave it for a future prune.
                after = os.lstat(path)
                if (after.st_ino != before.st_ino or
                        after.st_mtime != before.st_mtime or
                        not stat.S_ISREG(after.st_mode)):
                    continue
                os.unlink(path)
            except OSError:
                pass


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


def live_session_ids(sessions_dir=None, proc_root="/proc", limit=256):
    """Session ids with a positively-proven-live claude process: a
    sessions-registry entry (<pid>.json) whose pid is alive, not a
    zombie, and whose procStart matches /proc starttime (pid-reuse
    guard). Returns (ids, complete): complete=True only when EVERY
    registry entry was examined and judged, so callers may read
    absence as dead. complete=False (registry/proc unreadable, an
    unreadable or malformed entry, the scan bound hit) means absence
    proves nothing — a session could be live behind the entry we
    could not judge — and callers must render unknown, never dead.
    Mirrors lease_is_live's philosophy: declaring death needs proof.
    Never raises; any surprise degrades to incomplete."""
    ids = set()
    try:
        sessions_dir = sessions_dir or os.path.expanduser(
            "~/.claude/sessions")
        if not os.path.isdir(proc_root):
            return ids, False
        try:
            names = os.listdir(sessions_dir)
        except OSError:
            return ids, False
        complete = True
        seen = 0
        for name in names:
            if not name.endswith(".json"):
                continue
            stem = name[:-len(".json")]
            # Length-bounded before int(): CPython raises on absurdly
            # long digit strings, and no real pid exceeds 9 digits.
            if not stem.isdigit() or len(stem) > 9:
                continue
            seen += 1
            if seen > limit:
                complete = False  # unexamined entries could be live
                break
            try:
                pid = int(stem)
                entry = _registry_entry(pid, sessions_dir)
                if not entry:
                    complete = False  # unreadable: could name anyone
                    continue
                sid = entry.get("sessionId")
                if not isinstance(sid, str) or not _SESSION_ID.match(sid):
                    complete = False
                    continue
                st = proc_stat(pid, proc_root)
                if st is None:
                    # Missing /proc dir is proof of death; an existing
                    # but unreadable one (hidepid, races) is not proof
                    # of anything — unknown, not live (start-time
                    # unverifiable) and not dead.
                    if os.path.exists(os.path.join(proc_root, str(pid))):
                        complete = False
                    continue
                if st.get("state") in ("Z", "X", "x"):
                    continue  # zombie/dead per proc_pid_stat(5): exited
                try:
                    if int(entry.get("procStart")) == st["starttime"]:
                        ids.add(sid)
                    # mismatch: pid reused — positive proof this entry
                    # is stale, completeness unaffected
                except (TypeError, ValueError, OverflowError):
                    complete = False  # malformed procStart: unjudged
            except Exception:
                complete = False
        return ids, complete
    except Exception:
        return ids, False


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
                     proc_root="/proc", check_leases=True,
                     sessions_dir=None):
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
    # /clear and in-app /resume rotate the session id inside the SAME
    # claude process; without this check the watcher babysits the dead
    # id's frozen transcript until its deadline while the live session
    # goes unwatched. Positive proof only: a readable registry entry
    # for the bound pid, same starttime (same process), carrying a
    # DIFFERENT valid session id. A missing or malformed entry proves
    # nothing and must never retire a healthy watcher.
    try:
        entry = _registry_entry(
            binding["claude_pid"],
            sessions_dir or os.path.expanduser("~/.claude/sessions"))
        if entry:
            sid = entry.get("sessionId")
            try:
                same_proc = (int(entry.get("procStart"))
                             == binding["claude_start"])
            except (TypeError, ValueError, OverflowError):
                same_proc = False
            if (same_proc and isinstance(sid, str) and _SESSION_ID.match(sid)
                    and sid != binding["session_id"]):
                return False, "session_rotated"
    except Exception:
        pass  # registry trouble is never a retirement reason
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
        # Without this, recovery loses the request/threshold distinction
        # and a recovered request attempt would drop out of the
        # turn-boundary lane (Sol round-1 finding).
        "trigger_source": attempt.get("trigger_source", "threshold"),
        "lane": attempt.get("lane"),
        "defer_class": attempt.get("defer_class"),
        "activity_rev_at_defer": attempt.get("activity_rev_at_defer"),
        "starvation_alerted": bool(attempt.get("starvation_alerted")),
        # Own-packet proof (fast completion in transition_attempt, or a
        # late own packet at ladder R6'): without persisting it,
        # cm_compact_count loses the fired compact. `is True`, not
        # bool(): a hostile journal value like "false" replayed through
        # recovery must not launder into true (Sol round-3).
        "own_packet_proof": attempt.get("own_packet_proof") is True,
        "timers": dict(attempt.get("timers", {})),
    }
    record.update(extra)
    journal_append(path, record)
    return record


def _journal_records(lines):
    records = []
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


def read_journal(path):
    try:
        with open(path, "rb") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    return _journal_records(lines)


def cm_compact_count(cfg, session_id):
    """Compactions this manager fired for the session, from its watcher
    journal: attempts with own-packet proof — ACKED (the [cm-nonce]
    instruction provably arrived at PreCompact) or any record stamped
    own_packet_proof (a fast completion's BOUNDARY_CONFIRMED when
    packet and boundary landed inside one poll, or a retry's
    CLEANUP_REQUIRED whose late own packet proved the first submission
    fired). Deduped by attempt_id (retries re-nonce within one
    attempt; recovery replays re-journal a state), so each fired
    compact counts once. Deferred-foreign attempts (native
    auto-compact, a human /compact) never carry own proof and never
    count. None = count unavailable: no retained journal (unmanaged)
    or a journal that cannot be read — never a definite zero. A
    malformed-but-readable journal degrades to whatever parses, never
    an exception."""
    path = os.path.join(cfg["state_dir"], "managed", "watchers",
                        session_id + ".journal.jsonl")
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        # Read directly (not via read_journal) so a PRESENT journal
        # that cannot be read reports unknown, never a definite 0
        # (Sol round-3).
        with open(path, "rb") as fh:
            lines = fh.readlines()
        fired = set()
        for r in _journal_records(lines):
            if r.get("state") != "ACKED" and \
                    r.get("own_packet_proof") is not True:
                continue
            # Only manager-shaped ids count: a hostile schema-valid
            # record with a list/int/bool attempt_id must neither
            # crash the readout nor fabricate a fired compact.
            attempt_id = r.get("attempt_id")
            if isinstance(attempt_id, str) and attempt_id:
                fired.add(attempt_id)
        return len(fired)
    except Exception:
        return None


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
    return recover_attempt_records(read_journal(path), current_boot_id)


def _stamp_recovered_proof(recovered, cfg, session_id):
    """A recovered attempt that terminalizes (e.g. PREPARED ->
    CLEANUP_REQUIRED after a retry crash) never re-inspects the packet
    — but the FIRST submission's own packet may have landed while the
    watcher was down. Classify once at recovery so the durable record
    keeps the fired-compact proof (Sol round-3). Fail-open: journal-
    derived fields are untrusted and must not break watcher startup
    for a metadata stamp."""
    if recovered is None or recovered.get("own_packet_proof") is True:
        return
    try:
        if packet_classification(
                recovered, load_layer1_packet(cfg, session_id)) == "OWN":
            recovered["own_packet_proof"] = True
    except Exception:
        pass


def recover_attempt_records(records, current_boot_id):
    tail = None
    for record in reversed(records):
        if record.get("state") in ATTEMPT_STATES:
            tail = record
            break
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


def packet_classification(attempt, packet, cursor=None):
    if not isinstance(packet, dict):
        return "NONE"
    custom = str(packet.get("custom_instructions") or "")
    if any("[cm-%s]" % nonce in custom for nonce in attempt.get("nonces", [])):
        return "OWN"
    # A packet whose compaction already completed (boundary advanced past
    # its base count) is a leftover file, not an in-flight compaction —
    # without this, a finished auto-compact's packet reclassifies every
    # later attempt FOREIGN forever (the fbeb0bf1 wedge). OWN is checked
    # first: staleness must never erase fired-compact proof.
    if cursor is not None and boundary_confirmed_for_packet(packet, cursor):
        return "NONE"
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
                       generation_value=None, cursor=None):
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
        # A fast completion can land the OWN packet and the boundary
        # inside one poll, so ACKED is never journaled; without this
        # flag the durable record loses the only proof that the
        # confirmed compaction was OURS (Sol round-1 blocker: the
        # fired-compacts counter read 0 for a normal fast success).
        if packet_classification(out, packet) == "OWN":
            out["own_packet_proof"] = True
        return out
    classification = packet_classification(out, packet, cursor)
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
            # The deadline must fire even while the packet persists: this
            # early return skips the timer branch below, and a foreign
            # packet that never resolves (failed compact, epoch-crossed
            # base count) would otherwise defer forever with no alert.
            if now_mono >= out["timers"]["foreign_deadline_mono"]:
                out["state"], out["latch_kind"] = "LATCHED", "SAFETY"
                out["reason"] = "foreign_uncertain"
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


def instruction_text(nonce, width=None):
    if not re.match(r"^[0-9a-f]{16}$", nonce):
        raise ValueError("invalid nonce")
    text = INSTRUCTION_TEMPLATE.format(nonce=nonce)
    # The composer soft-wraps long input in narrow panes, and R5/R6'
    # verify against the single bottom-anchored "❯ " line — a wrapped
    # instruction can NEVER verify, so every attempt ends
    # CLEANUP_REQUIRED/R5_composer_mismatch (observed live: 76-col
    # pane, fbeb0bf1). The nonce marker is the load-bearing part; drop
    # the guidance tail when the full line cannot render unwrapped.
    if width is not None and len("❯ " + text) > width - WRAP_MARGIN_COLS:
        text = INSTRUCTION_TEMPLATE_SHORT.format(nonce=nonce)
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


def capture_pane(binding, run_tmux=default_run_tmux, ansi=False):
    args = ["capture-pane", "-p", "-J"]
    if ansi:
        args.append("-e")
    rc, out, _ = _result(run_tmux(_tmux(
        binding["socket"], args + ["-t", binding["pane_id"]])))
    return out if rc == 0 else None


_CSI_PARAM = "0123456789;:"
# A permission/selection modal's cursor row is "❯ 1. Yes" -- ASCII
# space, not NBSP (pinned live 2026-08-18) -- but reject the whole
# layout, not just that row: a modal can leave a stale composer visible
# elsewhere.
# Requires leading indentation: the live modal cursor row renders
# indented, while a history echo of a prompt that BEGINS with "1. "
# renders at column 0 — matching that would permanently starve the
# watcher behind a false modal (audit finding).
_MODAL_ROW = re.compile("^\\s+❯ \\d+\\.\\s")


def _strip_ansi_line(raw):
    """(plain, per-char dim flags, unknown). Interprets ONLY SGR (CSI
    ... 'm'); any other escape sequence marks the line unknown -- UI
    drift must classify as unparseable, never be regex-generalized
    past (Sol round-2)."""
    plain, dim_flags = [], []
    dim = False
    unknown = False
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\x1b":
            if raw[i + 1:i + 2] == "[":
                j = i + 2
                while j < n and raw[j] in _CSI_PARAM:
                    j += 1
                if j < n and raw[j] == "m":
                    params = raw[i + 2:j].split(";")
                    k = 0
                    while k < len(params):
                        param = params[k]
                        # SGR 0/"" reset and 22 end dim; 2 starts it.
                        # 38/48/58 take grouped color operands (5;n or
                        # 2;r;g;b) whose literal 2/5/0 values are NOT
                        # opcodes — treating them as such classified
                        # colored typed text as dim (audit blocker).
                        if param in ("", "0"):
                            dim = False
                            k += 1
                        elif param == "2":
                            dim = True
                            k += 1
                        elif param == "22":
                            dim = False
                            k += 1
                        elif param in ("38", "48", "58"):
                            # Operands must actually be present:
                            # "38;5" or "38;2;r;g" truncations are
                            # ambiguous and must fail closed (round-2
                            # audit: ESC[2;38;5m classified typed text
                            # as empty).
                            if (k + 2 < len(params) and
                                    params[k + 1] == "5" and
                                    params[k + 2].isdigit()):
                                k += 3
                            elif (k + 4 < len(params) and
                                    params[k + 1] == "2" and
                                    all(p.isdigit()
                                        for p in params[k + 2:k + 5])):
                                k += 5
                            else:
                                unknown = True
                                k = len(params)
                        else:
                            # Colon-form tokens ("38:5:2") are single
                            # self-contained tokens that cannot alter
                            # dim state; everything else is a non-dim
                            # attribute.
                            k += 1
                    i = j + 1
                    continue
                unknown = True
                i = j + 1 if j < n else n
                continue
            unknown = True
            if raw[i + 1:i + 2] in ("]", "P"):
                # OSC/DCS: swallow the body to BEL or ST so hyperlink
                # payloads don't leak into the plain text (the line is
                # already unknown; this only keeps plain deterministic
                # and free of address noise).
                j = i + 2
                while j < n and raw[j] != "\x07" and \
                        raw[j:j + 2] != "\x1b\\":
                    j += 1
                i = j + (2 if raw[j:j + 2] == "\x1b\\" else 1)
                continue
            i += 2
            continue
        if ch in ("\x9b", "\x90", "\x9d"):
            unknown = True
            i += 1
            continue
        plain.append(ch)
        dim_flags.append(dim)
        i += 1
    return "".join(plain), dim_flags, unknown


def parse_pane(ansi_capture):
    """Bottom-anchored structured parse of ONE coherent -J -e snapshot.
    composer: "empty" (bare composer marker, or marker followed only by
    whitespace and DIM ghost-suggestion text -- pinned live 2026-08-18:
    suggestions render ESC[2m-wrapped per word and are byte-identical
    to typed text once stripped), "nonempty" (any non-dim
    non-whitespace character after the NBSP -- real typed input),
    "absent", or "unknown" (unparseable escapes, or a last-marker line
    without the NBSP signature)."""
    plain_lines, parsed = [], []
    for raw in ansi_capture.splitlines():
        plain, dim_flags, unknown = _strip_ansi_line(raw)
        plain_lines.append(plain)
        parsed.append((plain, dim_flags, unknown))
    out = {"modal": any(_MODAL_ROW.match(p) for p in plain_lines),
           "plain": "\n".join(plain_lines),
           "row": None, "text": None, "composer": "absent"}
    for idx in range(len(plain_lines) - 1, -1, -1):
        if plain_lines[idx].startswith("❯"):
            out["row"] = idx
            break
    if out["row"] is None:
        return out
    plain, dim_flags, unknown = parsed[out["row"]]
    out["text"] = plain.rstrip("\r ")
    if unknown:
        out["composer"] = "unknown"
        return out
    if not plain.startswith("❯ "):
        out["composer"] = "unknown"
        return out
    for ch, dim in zip(plain[2:], dim_flags[2:]):
        if ch not in " \r" and not dim:
            out["composer"] = "nonempty"
            return out
    out["composer"] = "empty"
    return out


def snapshot_exact(snapshot, text):
    """R5/R6' typed-text verification on the bottom-anchored composer:
    exact instruction, known layout, no modal."""
    return (snapshot is not None and not snapshot["modal"] and
            snapshot["composer"] != "unknown" and
            snapshot["text"] == "❯ " + text)


def _projection(snapshot, dims):
    """Safety-critical regions only: harmless background repaint
    (spinners, elapsed counters, footer chrome) must not veto the
    boundary lane the way the whole-pane digest did (the live
    R3_changed defer in fbeb0bf1)."""
    return (snapshot["text"], snapshot["row"], snapshot["composer"],
            snapshot["modal"], dims)


def ladder_preflight(binding, cfg, paths, run_tmux=default_run_tmux,
                     proc_root="/proc", wait=lambda seconds: time.sleep(seconds),
                     sessions_dir=None, boundary_ok=False,
                     activity_reader=None, now_wall=time.time):
    """R1-R3 with two authorization lanes.

    strict: the original hook-independent contract — exact-empty
    composer, no busy chrome anywhere, whole-(plain-)pane stability.

    boundary: only for request-triggered or >=hard attempts
    (boundary_ok).  Accepts an input-empty composer while background
    chrome stays busy, proven by the paired activity marker (foreground
    turn ended) plus a safety projection that ignores volatile
    background repaint.  The fbeb0bf1 starvation fix.

    Returns (ok, reason, defer_class, lane, activity_rev)."""
    ok, facts = validate_binding(binding, cfg, paths, run_tmux, proc_root,
                                 sessions_dir=sessions_dir)
    if not ok:
        return False, "R1_%s" % facts, "structural", None, None
    activity = ("unknown", None)
    if activity_reader is not None:
        activity = activity_reader()
    first = capture_pane(binding, run_tmux, ansi=True)
    leases_ok, leases_reason = validate_binding(
        binding, cfg, paths, run_tmux, proc_root, sessions_dir=sessions_dir)
    if not leases_ok:
        return False, "R2_%s" % leases_reason, "structural", None, None
    if first is None:
        return False, "R2_capture_failed", "structural", None, None
    snap1 = parse_pane(first)
    # The modal veto applies to BOTH lanes: a selection modal's Enter
    # semantics are unknown, and a stale composer may sit underneath.
    # So does an AFFIRMATIVE running marker: mid-generation the
    # composer is byte-identical to idle and "esc to interrupt"
    # flickers off, so two quiet strict captures can align inside a
    # running turn (audit blocker). Only positive evidence vetoes —
    # sessions whose hooks never wrote markers read "unknown" and keep
    # the original hook-independent strict contract.
    strict1 = (composer_idle(snap1["plain"]) and not snap1["modal"] and
               activity[0] != "running")
    boundary1 = False
    boundary_block = None
    if boundary_ok:
        if activity[0] != "ended":
            boundary_block = "activity_%s" % activity[0]
        elif now_wall() - activity[1][3] < 1.0:
            boundary_block = "activity_unsettled"
        elif snap1["modal"]:
            boundary_block = "modal"
        elif snap1["composer"] == "empty":
            boundary1 = True
        else:
            boundary_block = "composer_%s" % snap1["composer"]
    if not strict1 and not boundary1:
        if boundary_block:
            reason = "R2_%s" % boundary_block
        elif snap1["modal"]:
            reason = "R2_modal"
        elif activity[0] == "running":
            reason = "R2_activity_running"
        else:
            reason = "R2_not_idle"
        return False, reason, "opportunity", None, None
    dims = (facts["width"], facts["height"])
    digest = hashlib.sha256(snap1["plain"].encode()).digest()
    proj1 = _projection(snap1, dims)
    stable_ms = cfg["managed_stable_ms"]
    if boundary1:
        # The boundary lane's stability window is its substitute for
        # whole-pane quiet; do not let a tiny configured window weaken it.
        stable_ms = max(stable_ms, 500)
    wait(stable_ms / 1000.0)
    second = capture_pane(binding, run_tmux, ansi=True)
    ok, facts2 = validate_binding(binding, cfg, paths, run_tmux, proc_root,
                                  sessions_dir=sessions_dir)
    if not ok:
        return False, "R3_%s" % facts2, "structural", None, None
    if second is None:
        return False, "R3_capture_failed", "structural", None, None
    if (facts2["width"], facts2["height"]) != dims:
        return False, "R3_dimension_changed", "structural", None, None
    snap2 = parse_pane(second)
    if (strict1 and composer_idle(snap2["plain"]) and not snap2["modal"] and
            hashlib.sha256(snap2["plain"].encode()).digest() == digest):
        if activity_reader is not None and activity_reader()[0] == "running":
            return False, "R3_activity_running", "opportunity", None, None
        return True, "ready", None, "strict", activity[1]
    if boundary1:
        check = activity_reader()
        if (check[0] == "ended" and check[1] == activity[1] and
                _projection(snap2, dims) == proj1):
            return True, "ready", None, "boundary", activity[1]
    return False, "R3_changed", "opportunity", None, None


def _activity_unchanged(activity_reader, activity_rev):
    """The boundary lane's evidence must be the SAME ended turn at every
    later rail; any revision change means a prompt started or evidence
    decayed (Sol round-2: re-read immediately before R4 and at R6')."""
    check = activity_reader()
    return check[0] == "ended" and check[1] == activity_rev


def _boundary_still_safe(binding, run_tmux, activity_reader, activity_rev):
    """Pre-R4 revalidation: same ended turn AND a still-input-safe,
    still-empty composer at the moment before the first typed byte.
    (At R6' the typed-text snapshot already proves the layout; only the
    activity revision needs re-proving there.)"""
    capture = capture_pane(binding, run_tmux, ansi=True)
    if capture is None:
        return False
    snap = parse_pane(capture)
    if snap["modal"] or snap["composer"] != "empty":
        return False
    # Marker read LAST — as close to the first typed byte as possible,
    # so a prompt submitted after the capture still skews the revision
    # (audit blocker: marker-before-capture ordering).
    return _activity_unchanged(activity_reader, activity_rev)


def run_ladder(binding, cfg, paths, attempt, cursor, journal_path,
               packet_loader, run_tmux=default_run_tmux, proc_root="/proc",
               wait=lambda seconds: time.sleep(seconds), now_mono=time.monotonic,
               sessions_dir=None, boundary_ok=False, activity_reader=None,
               now_wall=time.time):
    """Execute R1-R6'.  Never sends a clearing key."""
    ok, reason, defer_class, lane, activity_rev = ladder_preflight(
        binding, cfg, paths, run_tmux, proc_root, wait,
        sessions_dir=sessions_dir, boundary_ok=boundary_ok,
        activity_reader=activity_reader, now_wall=now_wall)
    if not ok:
        return dict(attempt, state="DEFERRED", reason=reason,
                    defer_class=defer_class)
    attempt = dict(attempt, lane=lane)
    ok, r4_facts = validate_binding(binding, cfg, paths, run_tmux, proc_root,
                                    sessions_dir=sessions_dir)
    if not ok:
        return dict(attempt, state="DEFERRED", reason="R4_%s" % r4_facts,
                    defer_class="structural")
    if lane == "boundary" and not _boundary_still_safe(
            binding, run_tmux, activity_reader, activity_rev):
        # Still pre-typing: a new prompt or modal appearing after the
        # preflight is an input opportunity that closed, not a hazard.
        return dict(attempt, state="DEFERRED", reason="R4_boundary_changed",
                    defer_class="opportunity")
    attempt = transition_attempt(attempt, "prepared", now_mono(), cfg)
    journal_record(journal_path, "PREPARED", attempt)
    text = instruction_text(attempt["nonce"], width=r4_facts["width"])
    rc, _, _ = _result(run_tmux(_tmux(
        binding["socket"], ["send-keys", "-t", binding["pane_id"], "-l", text])))
    if rc != 0:
        attempt.update(state="CLEANUP_REQUIRED", reason="R4_send_failed")
        journal_record(journal_path, "CLEANUP_REQUIRED", attempt,
                       reason=attempt["reason"])
        return attempt
    wait(0.4)
    capture = capture_pane(binding, run_tmux, ansi=True)
    snap = parse_pane(capture) if capture is not None else None
    ok, reason = validate_binding(binding, cfg, paths, run_tmux, proc_root,
                                  sessions_dir=sessions_dir)
    if not ok or not snapshot_exact(snap, text):
        detail = reason if not ok else "composer_mismatch"
        attempt.update(state="CLEANUP_REQUIRED", reason="R5_%s" % detail)
        journal_record(journal_path, "CLEANUP_REQUIRED", attempt,
                       reason=attempt["reason"])
        return attempt
    attempt = transition_attempt(attempt, "typed_verified", now_mono(), cfg)
    journal_record(journal_path, "TYPED_VERIFIED", attempt)
    ok, why = validate_binding(binding, cfg, paths, run_tmux, proc_root,
                               sessions_dir=sessions_dir)
    capture = capture_pane(binding, run_tmux, ansi=True)
    snap = parse_pane(capture) if capture is not None else None
    packet = packet_loader()
    # R6' re-OBSERVES the transcript rather than trusting the tick's
    # cursor (which is stale by the whole ladder duration), and uses the
    # full packet classification: an auto-trigger packet is foreign even
    # after a Layer-1 seq reset, and a late own-nonce packet also means
    # do-not-press-Enter (our retry text is typed but the first
    # submission already landed). A scan error means the boundary state
    # is unprovable — same abort. The fresh cursor feeds classification
    # so a leftover packet from an already-completed compaction cannot
    # abort the ladder as "foreign" (the fbeb0bf1 wedge).
    fresh = scan_cursor(dict(cursor), binding["transcript_path"])
    classification = packet_classification(attempt, packet, cursor=fresh)
    if classification == "OWN":
        # The late own packet proves the FIRST submission of this
        # attempt reached PreCompact — the compact fired no matter
        # which abort below wins (binding loss and composer mismatch
        # take precedence over the own_packet_late detail, but must
        # not discard the proof; Sol round-3). CLEANUP_REQUIRED is
        # terminal and never re-inspects the packet, so this is the
        # last chance to stamp it.
        attempt["own_packet_proof"] = True
    # The rescan must COMPLETE: caught_up=False means bytes beyond the
    # budget were not examined and could hold a boundary — an explicitly
    # incomplete view must not authorize Enter.
    generation_unchanged = (not fresh.get("_scan_error") and
                            fresh.get("caught_up") and
                            generation_key(generation(fresh)) ==
                            generation_key(attempt["generation"]))
    boundary_safe = (lane != "boundary" or _activity_unchanged(
        activity_reader, activity_rev))
    if (not ok or not snapshot_exact(snap, text) or
            classification != "NONE" or not generation_unchanged or
            not boundary_safe):
        if not ok:
            detail = str(why)
        elif not snapshot_exact(snap, text):
            detail = "composer_mismatch"
        elif classification == "OWN":
            detail = "own_packet_late"
        elif classification == "FOREIGN":
            detail = "foreign_packet"
        elif fresh.get("_scan_error"):
            detail = "scan_unavailable"
        elif not fresh.get("caught_up"):
            detail = "scan_incomplete"
        elif not boundary_safe:
            detail = "activity_changed"
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
    after = capture_pane(binding, run_tmux, ansi=True)
    snap_after = parse_pane(after) if after is not None else None
    if after is None:
        attempt.update(state="SUBMISSION_UNCERTAIN", reason="submit_unverifiable")
        journal_record(journal_path, "SUBMISSION_UNCERTAIN", attempt,
                       reason=attempt["reason"])
    elif snapshot_exact(snap_after, text):
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


ACTIVITY_MAX_BYTES = 4096
_PROMPT_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def activity_paths(cfg, session_id):
    sid = cm.path_component(session_id)
    base = os.path.join(cfg["state_dir"], "managed", "activity")
    return {"running": os.path.join(base, "running-%s.json" % sid),
            "ended": os.path.join(base, "ended-%s.json" % sid)}


def write_activity(cfg, payload, phase):
    """Hook-side half of the paired turn marker: UserPromptSubmit writes
    running-<sid>.json, Stop writes ended-<sid>.json.  Each hook owns
    exactly one file and never reads the other, so no lock is needed —
    the pairing (ended iff ended.prompt_id == running.prompt_id) happens
    at watcher read time, where a stale Stop simply fails to pair.
    Callers are fail-open; a refused write leaves the lane unavailable,
    never authorizes it."""
    if phase not in ("running", "ended"):
        return False
    session_id = payload.get("session_id")
    prompt_id = payload.get("prompt_id")
    transcript = payload.get("transcript_path")
    if not (isinstance(session_id, str) and _SESSION_ID.match(session_id)):
        return False
    if not (isinstance(prompt_id, str) and _PROMPT_ID.match(prompt_id)):
        return False
    if not isinstance(transcript, str) or not transcript:
        return False
    device = inode = None
    try:
        info = os.stat(transcript)
        device, inode = info.st_dev, info.st_ino
    except OSError:
        pass
    record = {
        "schema": SCHEMA, "session_id": session_id, "prompt_id": prompt_id,
        "transcript_path": transcript, "transcript_device": device,
        "transcript_inode": inode, "written_at": time.time()}
    if phase == "ended":
        # Diagnostic only: a co-installed BLOCKING Stop hook can force
        # the turn to continue after this write, making the pair read
        # "ended" during live generation — a documented boundary-lane
        # limitation no marker protocol can self-detect (audit 6eb959e).
        record["stop_hook_active"] = bool(payload.get("stop_hook_active"))
    _atomic_json(activity_paths(cfg, session_id)[phase], record)
    return True


def _read_activity_file(path, session_id, now):
    try:
        info = os.lstat(path)
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
                info.st_size > ACTIVITY_MAX_BYTES):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(fd, ACTIVITY_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > ACTIVITY_MAX_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            return None
        if value.get("session_id") != session_id:
            return None
        prompt_id = value.get("prompt_id")
        if not (isinstance(prompt_id, str) and _PROMPT_ID.match(prompt_id)):
            return None
        written_at = value.get("written_at")
        if (not isinstance(written_at, (int, float)) or
                isinstance(written_at, bool) or not math.isfinite(written_at)):
            return None
        if written_at > now + 30:
            return None
        if not isinstance(value.get("transcript_path"), str):
            return None
        return value
    except Exception:
        return None


def read_activity(cfg, session_id, transcript_path, now=None):
    """Watcher-side pairing, three-valued.

    ("ended", revision) — a coherent, identity-bound running/ended pair
    proves the foreground turn ended for THIS session and transcript.
    ("running", None) — valid marker evidence AFFIRMATIVELY shows a
    turn in flight (unpaired running, or ended older than running):
    used to veto even the strict lane, since an empty composer is
    byte-identical mid-generation (audit blocker).
    ("unknown", None) — missing, corrupt, mismatched, torn, or
    future-dated evidence: never authorizes anything.

    The re-read of running after ended linearizes the two lock-free
    files: a new prompt landing between the reads skews the re-read
    and degrades the result (audit blocker: torn A/A read after B
    installed).  No max age on ended: it is semantic state, not a
    freshness lease (Sol round-2/3)."""
    now = time.time() if now is None else now
    paths = activity_paths(cfg, session_id)
    running = _read_activity_file(paths["running"], session_id, now)
    ended = _read_activity_file(paths["ended"], session_id, now)
    running2 = _read_activity_file(paths["running"], session_id, now)
    if running is None or running2 is None:
        return "unknown", None
    coherent = (running2["prompt_id"] == running["prompt_id"] and
                running2["written_at"] == running["written_at"])
    try:
        info = os.stat(transcript_path)
        identity = (info.st_dev, info.st_ino)
    except OSError:
        return "unknown", None

    def bound(record):
        # Identity must be present AND match: a record stat'd before
        # the transcript existed carries (None, None) and is not
        # acceptable PAIRING evidence (audit major).
        return (record.get("transcript_path") == transcript_path and
                (record.get("transcript_device"),
                 record.get("transcript_inode")) == identity)

    if not coherent:
        return "unknown", None
    running_identity = (running.get("transcript_device"),
                        running.get("transcript_inode"))
    if running.get("transcript_path") != transcript_path:
        return "unknown", None
    if running_identity != (None, None) and running_identity != identity:
        # Positively mismatched running identity: stale evidence from a
        # replaced transcript — trust nothing.
        return "unknown", None
    # From here the running half is trustworthy: bound, or identity-less
    # because the FIRST prompt's hook fired before the transcript file
    # existed (round-2 audit blocker: calling that "unknown" let the
    # strict lane type mid-first-turn, and starved the lane until a
    # second prompt). Weak identity is acceptable for the VETO side;
    # pairing to "ended" still demands a fully bound ended half.
    if ended is None or not bound(ended):
        return "running", None
    if (ended["prompt_id"] != running["prompt_id"] or
            ended["written_at"] < running["written_at"]):
        return "running", None
    return "ended", (running["prompt_id"], running["written_at"],
                     ended["prompt_id"], ended["written_at"])


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
                 wall=time.time, wait=None, sessions_dir=None):
        self.binding, self.cfg, self.paths = binding, cfg, paths
        self.run_tmux, self.proc_root = run_tmux, proc_root
        self.sessions_dir = sessions_dir
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

    def _thresholds(self):
        """(eff, trigger) from ONE snapshot of this session's override
        file — a single decision must never mix two override versions
        (e.g. an old window with a new trigger; audit finding). Re-read
        on every call so a RUNNING watcher follows a change the human
        makes via the CLI `override` subcommand within one poll (tiny
        file; any error reads as no overrides inside the helper)."""
        overrides = cm.session_overrides(self.cfg,
                                         self.binding["session_id"])
        eff = cm.apply_overrides(
            cm.window_for(self.cfg, self.cursor.get("model")), overrides)
        trigger = overrides.get("managed_trigger_pct")
        if trigger is None:
            trigger = trigger_for(self.cfg, self.cursor.get("model"))
        return eff, trigger

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
            self.attempt["defer_class"] = "structural"
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
                                      self.run_tmux, self.proc_root,
                                      sessions_dir=self.sessions_dir)
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
                current_generation, cursor=self.cursor)
            if self.attempt.get("state") != previous:
                journal_record(self.paths["journal"], self.attempt["state"],
                               self.attempt, reason=self.attempt.get("reason"))
                self.alert_if_needed(previous)
            if self.attempt.get("state") in (
                    "CLEANUP_REQUIRED", "SUBMISSION_UNCERTAIN"):
                return True, self.attempt["state"]
            if self.attempt.get("state") == "BOUNDARY_CONFIRMED":
                self.backoff = BACKOFF_INITIAL_S
                eff, trigger = self._thresholds()
                pct = self.cursor.get("current", 0) / eff["context_window"]
                if pct >= trigger - self.cfg["rearm_band_pct"]:
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
                    # An explicit model request is NEW intent (a fresh
                    # handoff was just written): it must override the
                    # anti-churn latch, or the request starves until
                    # the re-arm band fills (live-suite catch: a
                    # REQUEST_OBSERVED during still_above_rearm_band
                    # was ignored for 250+ seconds).
                    if pending_request_id(
                            self.request_history,
                            self.consumed_request_generations,
                            generation_key(current_generation)) is not None:
                        self.attempt = transition_attempt(
                            self.attempt, "pct_rearmed", now, self.cfg,
                            generation_value=current_generation)
                        journal_record(self.paths["journal"], "READY",
                                       self.attempt,
                                       reason="request_overrides_latch")
                        self.attempt = None
                        return True, "latched"
                    eff, trigger = self._thresholds()
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
                    if (pct < trigger - self.cfg["rearm_band_pct"] or
                            grown >= self.cfg["rearm_band_pct"] * window):
                        self.attempt = transition_attempt(
                            self.attempt, "pct_rearmed", now, self.cfg,
                            generation_value=current_generation)
                        journal_record(self.paths["journal"], "READY", self.attempt)
                        self.attempt = None
                return True, "latched"
            elif self.attempt.get("state") in ("SUBMITTED", "ACKED", "DEFERRED"):
                if self.attempt["state"] == "DEFERRED" and \
                        self.attempt.get("reason") != "foreign_packet":
                    self._maybe_starvation_alert(now)
                if self.attempt["state"] != "DEFERRED" or \
                        self.attempt.get("reason") == "foreign_packet" or \
                        (now < self.attempt.get("timers", {}).get(
                            "next_attempt_at", float("inf")) and
                         not self._activity_advanced()):
                    return True, self.attempt["state"]
                self.attempt["state"] = "TRIGGERED"
            if self.attempt and self.attempt.get("state") == "TRIGGERED":
                return self._execute_attempt(now)
        if not self.cursor.get("caught_up"):
            return True, "catching_up"
        eff, trigger = self._thresholds()
        pct = self.cursor.get("current", 0) / eff["context_window"]
        req_generation = generation_key(current_generation)
        # One override per generation regardless of request-id churn.  A file
        # observed in an older generation was satisfied by that generation's
        # advance and remains ignored until the model writes a new id.
        request_id = pending_request_id(self.request_history,
                                        self.consumed_request_generations,
                                        req_generation)
        if pct < trigger and request_id is None:
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
        return self._execute_attempt(now)

    def _activity_reader(self):
        return read_activity(self.cfg, self.binding["session_id"],
                             self.binding["transcript_path"], self.wall())

    def _activity_advanced(self):
        """A new ended turn marker makes an opportunity-deferred attempt
        due immediately — the input window just opened, and waiting out
        next_attempt_at would waste it (Sol round-2)."""
        if not self.attempt or self.attempt.get("defer_class") != "opportunity":
            return False
        stored = self.attempt.get("activity_rev_at_defer")
        phase, revision = self._activity_reader()
        if phase != "ended" or revision is None:
            return False
        return stored is None or tuple(stored) != revision

    def _boundary_ok(self):
        """Recomputed on EVERY attempt run from one threshold snapshot:
        a threshold attempt created below hard is promoted to the
        turn-boundary lane the moment usage reaches hard, instead of
        inheriting the strict lane's starvation (Sol round-1)."""
        eff, _ = self._thresholds()
        pct = self.cursor.get("current", 0) / eff["context_window"]
        return (self.attempt.get("trigger_source") == "request" or
                pct >= eff["hard_pct"])

    def _schedule_defer(self, now):
        """Reason-aware scheduling: input-opportunity failures retry at
        the poll cadence — a fleeting composer window must not decay
        into a five-minute sampler (the fbeb0bf1 starvation); structural
        failures keep bounded exponential backoff."""
        if (self._boundary_ok() and
                "eligible_mono" not in self.attempt.setdefault("timers", {})):
            # The starvation clock starts at the first eligible defer —
            # lazy init inside the alert check alone started it one
            # poll late (round-2 audit).
            self.attempt["timers"]["eligible_mono"] = now
        if self.attempt.get("defer_class") == "opportunity":
            delay = self.cfg["managed_poll_s"]
            phase, revision = self._activity_reader()
            self.attempt["activity_rev_at_defer"] = (
                list(revision) if revision else None)
        else:
            delay = self.backoff
            self.backoff = min(BACKOFF_MAX_S, self.backoff * 2)
        self.attempt["timers"]["next_attempt_at"] = now + delay
        journal_record(self.paths["journal"], "DEFERRED", self.attempt,
                       reason=self.attempt.get("reason"))

    def _maybe_starvation_alert(self, now):
        """Starvation must not be invisible, but it is attention, not a
        resolvable hazard latch — safe conditions may still appear, so
        the attempt keeps retrying (Sol rounds 1-2)."""
        if not self.attempt or self.attempt.get("starvation_alerted"):
            return
        if not self._boundary_ok():
            return
        # Clock starts at first ELIGIBILITY (request observed, or hard
        # crossed), not attempt creation: a threshold attempt blocked
        # below hard for ten minutes has not been starved-while-urgent.
        since = self.attempt.setdefault("timers", {}).get("eligible_mono")
        if since is None:
            self.attempt["timers"]["eligible_mono"] = now
            return
        if not isinstance(since, (int, float)) or \
                now - since < STARVATION_ALERT_S:
            return
        self.attempt["starvation_alerted"] = True
        journal_record(self.paths["journal"], "STARVATION_ALERT", self.attempt,
                       reason=self.attempt.get("reason"),
                       starved_for_s=round(now - since, 1))
        # Recovery reads only ATTEMPT rows; without re-journaling the
        # DEFERRED tail the flag would be lost on crash and the alert
        # (and display message) would re-fire after recovery.
        journal_record(self.paths["journal"], "DEFERRED", self.attempt,
                       reason=self.attempt.get("reason"))
        try:
            self.run_tmux(_tmux(self.binding["socket"], [
                "display-message", "-t", self.binding["pane_id"],
                "compact-manager: compaction pending %ds; composer "
                "unavailable" % int(now - since)]))
        except Exception:
            pass

    def _poll_interval(self):
        """Fast cadence whenever an attempt is in flight — including
        SUBMITTED/ACKED, whose ack/boundary/timeout handling would
        otherwise wait out the slow idle interval (round-2 audit).
        Slow only when far below the trigger with nothing pending."""
        eff, trigger = self._thresholds()
        pct = self.cursor.get("current", 0) / eff["context_window"]
        pending = (self.attempt is not None and self.attempt.get("state")
                   in ("TRIGGERED", "DEFERRED", "SUBMITTED", "ACKED"))
        if pct < trigger - 0.20 and not pending:
            return 60
        return self.cfg["managed_poll_s"]

    def _execute_attempt(self, now):
        previous = "TRIGGERED"
        boundary_ok = self._boundary_ok()
        self.typed_critical = True
        try:
            self.attempt = run_ladder(
                self.binding, self.cfg, self.paths, self.attempt, self.cursor,
                self.paths["journal"],
                lambda: load_layer1_packet(self.cfg, self.binding["session_id"]),
                self.run_tmux, self.proc_root, self.wait, self.monotonic,
                sessions_dir=self.sessions_dir, boundary_ok=boundary_ok,
                activity_reader=self._activity_reader, now_wall=self.wall)
        finally:
            self.typed_critical = False
        if self.attempt["state"] == "DEFERRED":
            self._schedule_defer(now)
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
                # An operator stop is a clean exit and must say so —
                # a silent return leaves READY + DEAD-LEASE in status.
                journal_record(
                    self.paths["journal"], "WATCHER_RETIRED",
                    self.attempt or {
                        "run_token": self.binding["run_token"],
                        "generation": generation(self.cursor)},
                    reason="stop_requested")
                return
            now = self.monotonic()
            if now >= self.deadline:
                self._journal_typed_hazard("watcher_deadline")
                return
            valid, why = validate_binding(
                self.binding, self.cfg, self.paths, self.run_tmux,
                self.proc_root, sessions_dir=self.sessions_dir)
            if not valid:
                if not self._transient(why):
                    # Same contract as tick(): retiring with a recovered
                    # typed-state attempt must leave a durable hazard —
                    # and the retirement itself must be journaled, or
                    # status keeps reading WATCHER_READY + DEAD-LEASE
                    # for a watcher that left cleanly (audit finding).
                    self._journal_typed_hazard(why)
                    journal_record(
                        self.paths["journal"], "WATCHER_RETIRED",
                        self.attempt or {
                            "run_token": self.binding["run_token"],
                            "generation": generation(self.cursor)},
                        reason=why)
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
                if keep:
                    # Operator stop mid-idle: journal the actual cause,
                    # not the incidental last-tick status (which would
                    # read e.g. "below_threshold").
                    reason = "stop_requested"
                journal_record(self.paths["journal"], "WATCHER_RETIRED",
                               self.attempt or {"run_token": self.binding["run_token"],
                                                "generation": generation(self.cursor)},
                               reason=reason)
                break
            now = self.monotonic()
            # Floor keeps an overdue heartbeat from degenerating into a
            # zero-sleep hot loop.
            self.wait(max(0.5, min(self._poll_interval(),
                                   self.next_heartbeat - now)))


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
        _stamp_recovered_proof(recovered, cfg, binding["session_id"])
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
        # Per-watcher guard: one malformed journal record (unhashable
        # state, non-iterable nonces) must degrade to a flagged default
        # row, never abort the whole readout.
        try:
            records = read_journal(paths["journal"])
            tail = recover_attempt_records(records, current_boot)
            state = (tail or {}).get("state")
            reason = (tail or {}).get("reason")
            last = records[-1] if records else None
            if state is None:
                # No attempt rows in the journal: fall back to the last
                # lifecycle record, so a cleanly retired watcher reads
                # WATCHER_RETIRED instead of a default READY that the
                # overview would flag as a DEAD-LEASE false alarm.
                for record in reversed(records):
                    if record.get("state") in LIFECYCLE_STATES:
                        state = record.get("state")
                        reason = record.get("reason")
                        break
            elif (last is not None
                  and last.get("state") == "WATCHER_RETIRED"
                  and state not in ALERT_STATES):
                # The journal's FINAL word is a clean retirement: an
                # older completed attempt row must not resurrect READY
                # plus a DEAD-LEASE false alarm. Alert states still win
                # — a retirement record never masks a recovered hazard.
                state = "WATCHER_RETIRED"
                reason = last.get("reason")
            starved = None
            if state in ("TRIGGERED", "DEFERRED") and tail and \
                    tail.get("attempt_id"):
                # Attention, not a resolvable hazard: shown only while
                # the SAME attempt is still pending; a submission or a
                # new attempt clears it by construction.
                for record in reversed(records):
                    if (record.get("state") == "STARVATION_ALERT" and
                            record.get("attempt_id") == tail.get("attempt_id")):
                        starved = record.get("starved_for_s")
                        break
        except Exception:
            state = None
            reason = "journal unreadable (malformed record)"
            starved = None
        rows.append({"session_id": sid, "run_token": lease.get("run_token"),
                     "pid": lease.get("pid"),
                     "live": bool(lease) and lease_is_live(lease),
                     "has_lease": bool(lease),
                     "state": state or "WATCHER_READY",
                     "reason": reason,
                     "starved_for_s": starved})
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


def _fmt_grouped(value):
    """1000000 -> '1,000,000'; anything non-numeric passes through."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return "{:,}".format(int(value))


def _fmt_window_short(value):
    """Compact window for the overrides list: 1M, 200k, else grouped."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    value = int(value)
    if value and value % 1000000 == 0:
        return "%dM" % (value // 1000000)
    if value and value % 1000 == 0:
        return "%dk" % (value // 1000)
    return _fmt_grouped(value)


def _fmt_pct(value):
    """0.7 -> '70%'; guards float noise like 70.00000000000001."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return "%g%%" % round(value * 100, 3)


def _fmt_age(seconds):
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    return "%.1fh ago" % (seconds / 3600.0)


def overview_data(cfg, now=None, sessions_dir=None, proc_root="/proc"):
    """Everything the overview shows, as one JSON-safe dict (full
    session ids; ages in integer seconds; per-model EFFECTIVE values).
    The text renderer and the `overview --json` consumers (e.g. a
    dashboard poller) share this so they can never disagree. Degrades
    per-row like the text renderer always has. Each session row
    carries session_live: True (claude process proven alive), False
    (no live process — the state file merely lingers, up to 24h), or
    None (liveness could not be judged on this machine)."""
    now = time.time() if now is None else now
    hard = cfg.get("hard_pct", 0.80)
    data = {
        "schema": 1,
        "generated_at": now,
        "mode": cfg.get("mode"),
        "context_window": cfg.get("context_window"),
        "soft_pct": cfg.get("soft_pct", 0.70),
        "hard_pct": hard,
        "managed_trigger_pct": cfg.get("managed_trigger_pct", hard),
        "models": {},
        "watchers": [],
        "sessions": [],
    }
    override_patterns = (set(cfg.get("models", {}))
                         | set(cfg.get("managed_model_triggers") or {}))
    for pat in sorted(override_patterns):
        # window_for/trigger_for give the EFFECTIVE values a matching
        # model gets (override merged onto globals, clamps applied),
        # not the raw override fragment.
        eff = cm.window_for(cfg, pat)
        data["models"][pat] = {
            "context_window": eff["context_window"],
            "soft_pct": eff["soft_pct"],
            "hard_pct": eff["hard_pct"],
            "managed_trigger_pct": trigger_for(cfg, pat),
        }
    for r in status_rows(cfg):
        flags = []
        if r["state"] in ALERT_STATES:
            flags.append("ATTENTION")
        if not r["live"] and r["state"] != "WATCHER_RETIRED":
            flags.append("DEAD-LEASE")
        elif r["state"] == "WATCHER_RETIRED" and r.get("has_lease"):
            # A clean retirement releases its lease; retired WITH a
            # lease left behind means the watcher died in the window
            # between journaling retirement and releasing — that lease
            # still needs eyes, so retirement must not whitewash it.
            flags.append("DEAD-LEASE")
        pid = r.get("pid")
        pid_ok = isinstance(pid, int) and not isinstance(pid, bool) \
            and pid > 0
        # Catches pid-malformed lease shapes; a lease malformed only in
        # fields status_rows does not surface (token/start/heartbeat)
        # can still read live=true unflagged — status.md says so.
        if r["live"] and not pid_ok:
            flags.append("MALFORMED-LEASE")
        if r.get("starved_for_s") is not None:
            flags.append("ATTENTION")
        if r["state"] == "WATCHER_RETIRED" and not flags:
            # A cleanly retired watcher ages off the overview 24h after
            # its last journal write (same window the sessions list
            # uses); the journal itself survives to state_ttl_days so
            # the cm counter keeps its proof. Flagged rows (DEAD-LEASE
            # etc.) are hazards and stay visible regardless of age.
            paths = managed_paths(cfg, r["session_id"])
            if os.path.lexists(paths["session_lease"]):
                # A clean retirement releases its lease. A lease file
                # still present here is one status_rows could not read
                # (a readable one flags DEAD-LEASE above) — it still
                # blocks adoption, so it must stay visible and flagged,
                # never age out as "clean" (Sol audit).
                flags.append("DEAD-LEASE")
            else:
                try:
                    if now - os.path.getmtime(paths["journal"]) > 86_400:
                        continue
                except OSError:
                    pass  # nothing to date the retirement by — keep it
        data["watchers"].append({
            "session_id": r["session_id"], "pid": r["pid"],
            "state": r["state"], "live": r["live"],
            "reason": r["reason"], "flags": flags,
            "starved_for_s": r.get("starved_for_s")})
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
        except Exception:
            # Broad on purpose: e.g. a deeply nested state file raises
            # RecursionError out of json.load, and one bad file must
            # skip its row, never abort the whole readout.
            continue
        if isinstance(st, dict):
            entries.append((mtime, name[:-5], st))
    entries.sort(key=lambda e: e[0], reverse=True)
    live_ids, liveness_complete = live_session_ids(sessions_dir, proc_root)
    for mtime, sid, st in entries:
        # Dead may only be asserted from a COMPLETE scan: with any
        # entry unjudged, this sid could be live behind it.
        row = {"session_id": sid,
               "session_live": (True if sid in live_ids
                                else False if liveness_complete
                                else None)}
        # Degrade per-row, never lose the whole readout to one bad
        # state file (verify-round finding).
        try:
            # sid is the state filename's stem — a path_component fixed
            # point, so it addresses the same override file the advisor
            # reads.
            overrides = cm.session_overrides(cfg, sid)
            eff = cm.window_for(cfg, st.get("model"))

            def _stamped_pct(key, fallback):
                # Advisor-stamped fraction (its env-honoring view);
                # anything outside (0, 1] means unstamped/garbage.
                v = st.get(key)
                ok = (isinstance(v, (int, float))
                      and not isinstance(v, bool) and v == v
                      and 0 < v <= 1)
                return float(v) if ok else fallback

            sw = st.get("eff_window")
            window = (sw if isinstance(sw, int)
                      and not isinstance(sw, bool) and sw >= 10_000
                      else eff["context_window"])
            soft = _stamped_pct("eff_soft_pct", eff["soft_pct"])
            hard = _stamped_pct("eff_hard_pct", eff["hard_pct"])
            trig = _stamped_pct("eff_trigger_pct",
                                trigger_for(cfg, st.get("model")))
            # Per-session override keys win over stamps AND derivation:
            # a mid-session `override` write must read truthfully
            # immediately, not at the advisor's next restamp (which
            # converges to the same values).
            window = overrides.get("context_window", window)
            soft = overrides.get("soft_pct", soft)
            hard = overrides.get("hard_pct", hard)
            soft = min(soft, hard)
            trig = overrides.get("managed_trigger_pct", trig)

            def _token_count(value):
                # 1e11 ceiling: far above any real token count; huge
                # finite floats (1e308) otherwise overflow pct to inf.
                ok = isinstance(value, (int, float)) \
                    and not isinstance(value, bool) \
                    and value == value and 0 <= value <= 1e11
                return value if ok else 0

            current = _token_count(st.get("current"))
            peak = _token_count(st.get("peak"))
            raw_model = st.get("model")
            row.update({
                # Strings only: a hostile model value (nested object,
                # 1e400→inf) would otherwise survive to json.dumps and
                # emit invalid JSON (bare Infinity) or RecursionError —
                # and an object reaches React as an unrenderable child.
                "model": raw_model if isinstance(raw_model, str) else None,
                "current": current, "peak": peak, "window": window,
                "pct": 100.0 * current / window,
                "soft_pct": soft, "hard_pct": hard, "trigger_pct": trig,
                # None = count unavailable: no watcher journal
                # (unmanaged session) OR a journal that could not be
                # read. Either way distinct from a managed session at
                # 0 fired compacts.
                "cm_compacts": cm_compact_count(cfg, sid),
                "updated_epoch": mtime,
            })
            # A future mtime sorts first and would fake a fresh age —
            # surface the anomaly instead of clamping it to 0s. 1s
            # covers fs-timestamp granularity; beyond that is anomaly.
            if mtime <= now + 1:
                row["age_s"] = max(0, int(now - mtime))
            else:
                row["future_mtime"] = True
        except Exception:
            row["unreadable"] = True
        data["sessions"].append(row)
    return data


def overview_text(cfg, now=None, sessions_dir=None, proc_root="/proc"):
    """Deterministic status report: config, watcher rows (with the
    attention flags status.md used to ask the model to derive), and
    per-session usage from the advisor's state files. The slash
    command relays this instead of re-deriving the logic in prose.
    Session ids print as 8-char prefixes; stop/resolve accept them."""
    data = overview_data(cfg, now, sessions_dir, proc_root)
    lines = []
    lines.append("mode=%s  window=%s  soft=%s  hard=%s  trigger=%s" % (
        data["mode"], _fmt_grouped(data["context_window"]),
        _fmt_pct(data["soft_pct"]), _fmt_pct(data["hard_pct"]),
        _fmt_pct(data["managed_trigger_pct"])))
    if data["models"]:
        lines.append("")
        lines.append("model overrides (%d)" % len(data["models"]))
        orows = [("pattern", "window", "soft", "hard", "trigger")]
        for pat, eff in sorted(data["models"].items()):
            orows.append((pat, _fmt_window_short(eff["context_window"]),
                          _fmt_pct(eff["soft_pct"]),
                          _fmt_pct(eff["hard_pct"]),
                          _fmt_pct(eff["managed_trigger_pct"])))
        pat_w = max(len(t[0]) for t in orows)
        win_w = max(len(t[1]) for t in orows)
        soft_w = max(len(t[2]) for t in orows)
        hard_w = max(len(t[3]) for t in orows)
        for t in orows:
            lines.append("  %s  %s  %s  %s  %s" % (
                t[0].ljust(pat_w), t[1].ljust(win_w),
                t[2].ljust(soft_w), t[3].ljust(hard_w), t[4]))
    lines.append("")
    lines.append("watchers (%d)" % len(data["watchers"]))
    wrows = []
    for w in data["watchers"]:
        state = w["state"]
        if isinstance(state, str) and state.startswith("WATCHER_"):
            state = state[len("WATCHER_"):]
        status = ("✗ " + ",".join(w["flags"])) if w["flags"] \
            else ("live" if w["live"] else "")
        if w["reason"]:
            status = (status + "  " if status else "") \
                + "reason=%s" % w["reason"]
        wrows.append((str(w["session_id"])[:8],
                      "—" if w["pid"] is None else str(w["pid"]),
                      str(state), status))
    if wrows:
        header = ("session", "watcher pid", "state", "health")
        id_w = max(len(t[0]) for t in wrows + [header])
        pid_w = max(len(t[1]) for t in wrows + [header])
        st_w = max(len(t[2]) for t in wrows + [header])
        for t in [header] + wrows:
            lines.append(("  %s  %s  %s  %s" % (
                t[0].ljust(id_w), t[1].ljust(pid_w),
                t[2].ljust(st_w), t[3])).rstrip())
    lines.append("")
    label = "sessions touched <24h (%d)" % len(data["sessions"])
    srows = []

    def _alive_text(value):
        if value is True:
            return "live"
        if value is False:
            return "GONE"
        return "?"

    for i, s in enumerate(data["sessions"]):
        marker = "> " if i == 0 else "  "
        alive = _alive_text(s.get("session_live"))
        if s.get("unreadable"):
            # Through the same column machinery as readable rows so the
            # alive verdict stays under its header (audit finding).
            srows.append((marker, str(s["session_id"])[:8],
                          "(unreadable state row)", "", "", "", "", "",
                          "", alive))
            continue
        age = ("FUTURE-MTIME(untrustworthy)" if s.get("future_mtime")
               else _fmt_age(s["age_s"]))
        cm_count = s.get("cm_compacts")
        srows.append((marker, str(s["session_id"])[:8],
                      str(s.get("model") or "?"),
                      _fmt_grouped(s["current"]), _fmt_grouped(s["peak"]),
                      "%.1f%%" % s["pct"], _fmt_pct(s.get("trigger_pct")),
                      "" if cm_count is None else str(cm_count),
                      age, alive))
    if srows:
        lines.append(label)
        id_w = max([len("session")] + [len(t[1]) for t in srows])
        model_w = max([len("model")] + [len(t[2]) for t in srows])
        cur_w = max([len("current")] + [len(t[3]) for t in srows])
        peak_w = max([len("peak")] + [len(t[4]) for t in srows])
        pct_w = max([len("pct")] + [len(t[5]) for t in srows])
        trig_w = max([len("trig")] + [len(t[6]) for t in srows])
        cm_w = max([len("cm")] + [len(t[7]) for t in srows])
        age_w = max([len("updated")] + [len(t[8]) for t in srows])
        lines.append("  %s  %s  %s  %s  %s  %s  %s  %s  %s" % (
            "session".ljust(id_w), "model".ljust(model_w),
            "current".rjust(cur_w), "peak".rjust(peak_w),
            "pct".rjust(pct_w), "trig".rjust(trig_w),
            "cm".rjust(cm_w), "updated".rjust(age_w), "alive"))
        for t in srows:
            lines.append(("%s%s  %s  %s  %s  %s  %s  %s  %s  %s" % (
                t[0], t[1].ljust(id_w), t[2].ljust(model_w),
                t[3].rjust(cur_w), t[4].rjust(peak_w),
                t[5].rjust(pct_w), t[6].rjust(trig_w),
                t[7].rjust(cm_w), t[8].rjust(age_w), t[9])).rstrip())
        lines.append("(> = most recently updated; the advisor touches "
                     "it on every tool call, so a seconds-old age is "
                     "almost certainly the invoking session)")
        lines.append("(cm = compactions this manager fired for the "
                     "session; blank = count unavailable — no or "
                     "unreadable watcher journal)")
        alives = {t[9] for t in srows}
        if "GONE" in alives:
            lines.append("(GONE = no live claude process for this "
                         "session; its state file just lingers and "
                         "ages out of this list after 24h)")
        if "?" in alives:
            lines.append("(alive=? — session liveness could not be "
                         "judged on this machine)")
    else:
        lines.append(label)
    return "\n".join(lines)


def expand_sid(cfg, sid):
    """Expand an unambiguous session-id prefix (overview prints 8-char
    ids) to the full id known from leases, journals, or state files.
    An exact id always passes through; an unknown one is returned
    unchanged so stop/resolve report their own 'no live lease'."""
    candidates = set()
    base = cfg["state_dir"]
    for directory, prefix, suffix in (
            (os.path.join(base, "managed", "leases"),
             "session-", ".json"),
            (os.path.join(base, "managed", "watchers"),
             "", ".journal.jsonl"),
            (os.path.join(base, "state"), "", ".json"),
            (os.path.join(base, "overrides"), "", ".json")):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not (name.startswith(prefix) and name.endswith(suffix)):
                continue
            candidate = name[len(prefix):len(name) - len(suffix)]
            # Ghost-candidate hygiene: only session-id-shaped names from
            # regular files may widen a prefix into ambiguity (the
            # operational lease reads reject symlinks; mirror that).
            # fullmatch, not match: `$` alone would admit a trailing
            # newline in the name. isfile excludes dirs/FIFOs.
            if not _SESSION_ID.fullmatch(candidate):
                continue
            full = os.path.join(directory, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            candidates.add(candidate)
    if sid in candidates:
        return sid, None
    matches = sorted(c for c in candidates if c.startswith(sid))
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, ("ambiguous session-id prefix %s: matches %s"
                      % (sid, ", ".join(matches)))
    return sid, None


_OVERRIDE_KEYMAP = {"trigger": "managed_trigger_pct", "soft": "soft_pct",
                    "hard": "hard_pct", "window": "context_window"}


def _parse_override_assignments(assignments):
    """key=value args for the override subcommand → validated
    override-file fragment, or (None, error). The CLI is human-facing,
    so bad input is loud here — the fail-open silence lives in the
    readers, not the writer."""
    out = {}
    for item in assignments:
        key, sep, raw = item.partition("=")
        key = key.strip().lower()
        if not sep or key not in _OVERRIDE_KEYMAP:
            return None, ("expected key=value with key one of "
                          "trigger/soft/hard/window, got %r" % item)
        raw = raw.strip()
        if key == "window":
            try:
                value = int(raw.replace("_", "").replace(",", ""))
            except ValueError:
                return None, ("window must be an integer token count, "
                              "got %r" % raw)
            # Same bounds the reader enforces; the ceiling keeps a
            # huge int from overflowing float math downstream.
            if not (10_000 <= value <= 1_000_000_000):
                return None, ("window must land in [10000, 1000000000] "
                              "tokens, got %r" % raw)
        else:
            try:
                # Exactly one % suffix: "60%%" is a typo, not 60%.
                value = float(raw[:-1] if raw.endswith("%") else raw)
            except ValueError:
                return None, "%s must be a percentage, got %r" % (key, raw)
            if raw.endswith("%") or value > 1:
                value /= 100.0
            if not (0 < value <= 1):
                return None, ("%s must land in (0%%, 100%%], got %r"
                              % (key, item))
        out[_OVERRIDE_KEYMAP[key]] = value
    return out, None


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
    overview = sub.add_parser("overview")
    overview.add_argument("--json", action="store_true",
                          help="structured output (full session ids)")
    stop = sub.add_parser("stop")
    stop.add_argument("sid")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("sid")
    override = sub.add_parser(
        "override",
        help="per-session threshold overrides (trigger/soft/hard/window)")
    override.add_argument("sid")
    override.add_argument("assignments", nargs="*", metavar="key=value",
                          help="trigger=60%% soft=0.5 hard=55%% "
                               "window=500000 (any subset; none = show)")
    override.add_argument("--clear", action="store_true",
                          help="remove this session's overrides")
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
        if getattr(args, "json", False):
            print(json.dumps(overview_data(cfg), indent=2, sort_keys=True))
        else:
            print(overview_text(cfg))
        return 0
    elif args.command == "stop":
        sid, error = expand_sid(cfg, args.sid)
        if error:
            print("compact-manager: %s" % error, file=sys.stderr)
            return 1
        tail = recover_attempt(managed_paths(cfg, sid)["journal"],
                               boot_id())
        if tail and tail.get("state") in ALERT_STATES:
            print("ALERT %s: %s" % (tail["state"],
                                    tail.get("reason", "operator action required")))
        ok, message = stop_session(cfg, sid)
        print(message)
        return 0 if ok else 1
    elif args.command == "override":
        sid, error = expand_sid(cfg, args.sid)
        if error:
            print("compact-manager: %s" % error, file=sys.stderr)
            return 1
        # path_component canonicalizes (".abcdefgh" and "abcdefgh"
        # address the same file), so an id-shaped-but-invalid sid could
        # silently write ANOTHER session's override — reject anything
        # that is not a valid full session id after prefix expansion.
        if not _SESSION_ID.fullmatch(sid):
            print("compact-manager: %r is not a valid session id "
                  "(or an unambiguous prefix of a known one)" % sid,
                  file=sys.stderr)
            return 1
        path = cm.override_path(cfg, sid)
        if args.clear:
            if args.assignments:
                print("compact-manager: --clear takes no key=value "
                      "assignments", file=sys.stderr)
                return 1
            try:
                os.remove(path)
                print("overrides cleared for %s" % sid)
            except FileNotFoundError:
                print("no overrides were set for %s" % sid)
            except OSError as e:
                print("compact-manager: could not clear %s: %s" % (path, e),
                      file=sys.stderr)
                return 1
        elif args.assignments:
            fragment, error = _parse_override_assignments(args.assignments)
            if error:
                print("compact-manager: %s" % error, file=sys.stderr)
                return 1
            merged = dict(cm.session_overrides(cfg, sid))
            merged.update(fragment)
            try:
                _atomic_json(path, merged)
            except OSError as e:
                print("compact-manager: could not write %s: %s" % (path, e),
                      file=sys.stderr)
                return 1
        current = cm.session_overrides(cfg, sid)
        st = {}
        try:
            with open(cm.state_paths(cfg, sid)["state"]) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                st = loaded
        except Exception:
            st = {}
        eff = cm.apply_overrides(cm.window_for(cfg, st.get("model")),
                                 current)
        trig = current.get("managed_trigger_pct",
                           trigger_for(cfg, st.get("model")))
        print("override file: %s" % (json.dumps(current, sort_keys=True)
                                     if current else "none"))
        print("effective now: window=%s  soft=%s  hard=%s  trigger=%s" % (
            _fmt_grouped(eff["context_window"]), _fmt_pct(eff["soft_pct"]),
            _fmt_pct(eff["hard_pct"]), _fmt_pct(trig)))
        print("(the session's readout stamps refresh on its next tool "
              "call; a running watcher re-reads within one poll)")
        return 0
    else:
        sid, error = expand_sid(cfg, args.sid)
        if error:
            print("compact-manager: %s" % error, file=sys.stderr)
            return 1
        ok, message = resolve_session(cfg, sid)
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
