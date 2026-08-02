"""Shared library for subagent-gauge hooks.

Design notes live in docs/DESIGN.md. Everything here fails open: a gauge
must never block agent work, so callers wrap entry points and exit 0 on
any exception (after best-effort error logging).

The subagent transcript JSONL is an UNDOCUMENTED Claude Code internal.
Parsing is defensive; when the format is unrecognizable we record
"unmeasurable" rather than guessing.
"""
import fcntl
import json
import os
import sys
import time

SCHEMA_VERSION = 1

_DEFAULTS = {
    # Context size (tokens) at/above which reports are flagged and the
    # guard warns before re-tasking the agent. 150k is conservative for
    # mixed models; raise it if you routinely run 1M-context agents.
    "warn_tokens": 150_000,
    # Only enqueue dispatcher reports for agents at/above this size.
    # 0 = report every subagent stop (recommended: reports are cheap).
    "report_min_tokens": 0,
    # If true, the guard asks for user confirmation (permissionDecision
    # "ask") before messaging an over-threshold agent, instead of just
    # injecting a warning.
    "hard_block": False,
    # Also show each report to the human as a hook systemMessage.
    "system_message": True,
    # Max queued reports injected per drain (protects parent context).
    "drain_batch_max": 20,
    # How long to wait for the stopping agent's transcript to finish
    # flushing before measuring (see measure(): stability wait).
    "flush_grace_ms": 4000,
    # Where state (per-agent readings, queues, ledger) lives.
    "state_dir": "~/.claude/subagent-gauge",
    # Append every observation to <state_dir>/ledger.jsonl.
    "ledger": True,
    "ledger_max_bytes": 5_000_000,
    # Queue/state files for sessions idle longer than this get pruned.
    "state_ttl_days": 7,
}

_ENV_PREFIX = "SUBAGENT_GAUGE_"


def load_config():
    """Defaults <- optional JSON config file <- environment variables."""
    cfg = dict(_DEFAULTS)
    path = os.environ.get(
        _ENV_PREFIX + "CONFIG",
        os.path.expanduser("~/.claude/subagent-gauge.json"))
    try:
        with open(path) as fh:
            file_cfg = json.load(fh)
        if isinstance(file_cfg, dict):
            for k, v in file_cfg.items():
                if k in cfg:
                    cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception:
        _log_error(f"config file {path} unreadable; using defaults")
    # File values must match the default's type (bool is checked before
    # int since bool subclasses int); wrong-typed values are ignored
    # loudly rather than exploding later in a comparison.
    for k, dflt in _DEFAULTS.items():
        v = cfg[k]
        if isinstance(dflt, bool):
            ok = isinstance(v, bool)
        elif isinstance(dflt, int):
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
        else:
            ok = isinstance(v, str) and v.strip()
        if not ok:
            _log_error(f"config {k}={v!r} has wrong type; using default")
            cfg[k] = dflt
    for k in cfg:
        env = os.environ.get(_ENV_PREFIX + k.upper())
        if env is None:
            continue
        if isinstance(cfg[k], bool):
            cfg[k] = env.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cfg[k], int):
            try:
                cfg[k] = max(0, int(env))
            except ValueError:
                _log_error(f"env {_ENV_PREFIX}{k.upper()}={env!r} not an int")
        else:
            cfg[k] = env
    # Range clamps: values that would make the tool destructive or
    # inert are pulled back to sane minimums.
    cfg["drain_batch_max"] = max(1, cfg["drain_batch_max"])
    cfg["state_ttl_days"] = max(1, cfg["state_ttl_days"])
    cfg["ledger_max_bytes"] = max(65_536, cfg["ledger_max_bytes"])
    cfg["state_dir"] = os.path.abspath(os.path.expanduser(cfg["state_dir"]))
    return cfg


def path_component(value, fallback="unknown"):
    """Session/agent ids become path components; anything outside a
    conservative charset (or empty) is replaced, closing traversal via
    malformed hook payloads."""
    value = str(value or "")
    if value and all(c.isalnum() or c in "._-" for c in value) \
            and value not in (".", ".."):
        return value
    return fallback


def is_subagent_payload(payload):
    """True when a hook payload originated inside a subagent rather than
    the root session (agent fields present, or the transcript lives
    under a subagents/ directory)."""
    if payload.get("agent_id") or payload.get("agent_transcript_path"):
        return True
    tp = str(payload.get("transcript_path") or "")
    return "/subagents/" in tp.replace("\\", "/")


def sanitize(text, max_len=600):
    """One printable line: injected report text must not be able to smuggle
    extra lines or control sequences into the orchestrator's context."""
    text = "".join(c if c.isprintable() else " " for c in str(text))
    return text[:max_len]


def _log_error(msg):
    # stderr only: on exit 0 Claude Code keeps hook stderr out of the
    # conversation (visible in --debug), which is the failure visibility
    # this tool can afford without risking sessions.
    print(f"subagent-gauge: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- measure

def _scan(path):
    """One full pass over a transcript JSONL.

    Returns dict with:
      current   - context tokens of the last terminal usage row
                  (input + cache_read + cache_creation + output)
      prompt    - same minus output (context before the final response)
      peak      - max of the current-style sum over all terminal rows
      compactions - count of compaction summary rows
      terminal  - whether a terminal (stop_reason set) usage row was seen
    Any structural surprise in a line skips that line.
    """
    res = {"current": 0, "prompt": 0, "peak": 0, "compactions": 0,
           "terminal": False, "rows": 0, "last_terminal_ts": ""}
    known = ("input_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens", "output_tokens")
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("isCompactSummary"):
                res["compactions"] += 1
            msg = row.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            # A schema change that renames the token fields must read as
            # "unmeasurable", not as a believable ~0k report.
            if not any(isinstance(u.get(k), int) for k in known):
                continue

            def tok(key):
                v = u.get(key, 0)
                return v if isinstance(v, int) and not isinstance(v, bool) \
                    and v >= 0 else 0

            prompt = (tok("input_tokens") + tok("cache_read_input_tokens")
                      + tok("cache_creation_input_tokens"))
            total = prompt + tok("output_tokens")
            res["rows"] += 1
            # Streaming writes preliminary usage rows (stop_reason null)
            # before the terminal row for the same request; prefer
            # terminal rows but fall back to any usage row.
            if msg.get("stop_reason") is not None:
                res["current"] = total
                res["prompt"] = prompt
                res["terminal"] = True
                res["peak"] = max(res["peak"], total)
                ts = row.get("timestamp")
                if isinstance(ts, str):
                    res["last_terminal_ts"] = ts
            elif not res["terminal"]:
                res["current"] = total
                res["prompt"] = prompt
                res["peak"] = max(res["peak"], total)
    return res


def _ts_age_seconds(iso_ts):
    """Age of an ISO-8601 row timestamp, or None if unparsable."""
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return None


# A terminal row younger than this is taken as belonging to the burst
# that just stopped; older means the flush hasn't landed yet (reused
# agent showing its previous burst) and we keep waiting.
FRESH_TERMINAL_S = 30


def measure(path, grace_ms):
    """Measure a transcript, waiting out the stop-time flush race.

    At SubagentStop time the final usage row is often not yet written
    (verified 2026-07-31), and a previously-used agent already has stale
    rows — so "a usage row exists" is NOT enough, and neither is "the
    file went quiet" (the flush may simply not have started). Rows carry
    timestamps, so we poll (parsing is cheap: ~0.07s for a 5MB
    transcript) until the newest terminal usage row is recent, and
    otherwise return the best reading flagged stale=True so downstream
    reports it honestly instead of as current.
    """
    deadline = time.monotonic() + grace_ms / 1000.0
    res = None
    while True:
        try:
            res = _scan(path)
        except OSError:
            return None
        if res["rows"] and res["terminal"]:
            age = _ts_age_seconds(res["last_terminal_ts"])
            if age is None or age <= FRESH_TERMINAL_S:
                res["stale"] = False
                return res
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if not res or not res["rows"]:
        return None
    res["stale"] = True
    return res


def read_meta(transcript_path):
    """Sidecar meta.json holds the reliable identity (name, agentType,
    model, spawnDepth). Absent or unreadable -> {}."""
    meta_path = transcript_path[:-6] + ".meta.json" \
        if transcript_path.endswith(".jsonl") else transcript_path + ".meta.json"
    try:
        with open(meta_path) as fh:
            meta = json.load(fh)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- state IO

def _locked_open(path, mode, timeout=2.0):
    """Open with a BOUNDED exclusive lock; raises TimeoutError if a
    holder is stuck, so callers fail open instead of eating the hook
    timeout on every tool call."""
    fh = open(path, mode)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                raise TimeoutError(f"lock on {path}")
            time.sleep(0.05)


def _private_makedirs(path):
    """makedirs + 0o700 on every component that did not exist before
    (makedirs' mode= is umask-masked and leaf-only, so chmod explicitly)."""
    missing = []
    p = os.path.abspath(path)
    while p and not os.path.isdir(p):
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    os.makedirs(path, exist_ok=True)
    for m in missing:
        try:
            os.chmod(m, 0o700)
        except OSError:
            pass


def state_paths(cfg, session_id):
    root = cfg["state_dir"]
    sess = path_component(session_id)
    return {
        "agents": os.path.join(root, "agents", sess),
        "queue": os.path.join(root, "queue", f"{sess}.jsonl"),
        "ledger": os.path.join(root, "ledger.jsonl"),
        "sentinel": os.path.join(root, ".subagent-gauge-state"),
    }


def write_agent_state(cfg, session_id, record):
    if not record.get("agent_id"):
        return  # anonymous agents would all collide on one file
    d = state_paths(cfg, session_id)["agents"]
    _private_makedirs(d)
    _write_sentinel(cfg)
    aid = path_component(record["agent_id"])
    tmp = os.path.join(d, f".{aid}.{os.getpid()}.tmp")
    dst = os.path.join(d, f"{aid}.json")
    with open(tmp, "w") as fh:
        json.dump(record, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)


def load_agent_states(cfg, session_id):
    """Newest-first: with reused agent names (the harness's own naming
    rule is latest-wins) the caller must see the most recent record."""
    d = state_paths(cfg, session_id)["agents"]
    entries = []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(d, n)
        try:
            with open(path) as fh:
                rec = json.load(fh)
            if isinstance(rec, dict):
                entries.append((os.path.getmtime(path), rec))
        except Exception:
            continue
    return [rec for _, rec in sorted(entries, key=lambda e: e[0], reverse=True)]


def enqueue(cfg, session_id, text):
    q = state_paths(cfg, session_id)["queue"]
    _private_makedirs(os.path.dirname(q))
    _write_sentinel(cfg)
    with _locked_open(q, "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "text": sanitize(text)}) + "\n")


def drain_queue(cfg, session_id, batch_max):
    """Take up to batch_max entries under lock, leaving the remainder.

    The remainder is written to a temp file and swapped in, so a crash
    mid-drain loses nothing (worst case: the batch is re-delivered).
    Texts are re-sanitized on the way out — the queue file is the trust
    boundary; anything could have appended to it."""
    q = state_paths(cfg, session_id)["queue"]
    if not os.path.isfile(q) or os.path.getsize(q) == 0:
        return []
    texts = []
    with _locked_open(q, "r+") as fh:
        lines = fh.readlines()
        keep = lines[batch_max:]
        tmp = q + ".tmp"
        with open(tmp, "w") as out:
            out.writelines(keep)
        os.chmod(tmp, 0o600)
        os.replace(tmp, q)
    for line in lines[:batch_max]:
        try:
            texts.append(sanitize(json.loads(line)["text"]))
        except Exception:
            continue
    return texts


def _write_sentinel(cfg):
    """Marks the state dir as ours; prune refuses to run without it, so
    a misconfigured state_dir can never make prune touch user files."""
    p = state_paths(cfg, "")["sentinel"]
    if not os.path.isfile(p):
        with open(p, "w") as fh:
            fh.write("Managed by subagent-gauge. Safe to delete this "
                     "whole directory.\n")
        os.chmod(p, 0o600)


def ledger_append(cfg, entry):
    if not cfg["ledger"]:
        return
    path = state_paths(cfg, "")["ledger"]
    _private_makedirs(os.path.dirname(path))
    _write_sentinel(cfg)
    entry = {"v": SCHEMA_VERSION, "ts": time.time(), **entry}
    with _locked_open(path, "a") as fh:
        # Rotate under the same lock: copy-then-truncate keeps the
        # locked inode as the live ledger.
        try:
            if os.fstat(fh.fileno()).st_size > cfg["ledger_max_bytes"]:
                import shutil
                shutil.copyfile(path, path + ".1")
                os.chmod(path + ".1", 0o600)
                fh.truncate(0)
        except OSError:
            pass
        fh.write(json.dumps(entry) + "\n")


def prune_stale(cfg):
    """Best-effort removal of queue files and agent-state dirs untouched
    for state_ttl_days. Called opportunistically from the observer.

    Deletion safety: refuses to run unless the sentinel file marks the
    state dir as gauge-owned (a typo'd state_dir must never get its
    contents pruned); never follows symlinks; only deletes filenames
    matching what this tool writes."""
    root = cfg["state_dir"]
    if not os.path.isfile(state_paths(cfg, "")["sentinel"]):
        return
    cutoff = time.time() - cfg["state_ttl_days"] * 86400

    def expendable(name):
        return name.endswith((".json", ".jsonl", ".tmp"))

    def old(path):
        st = os.lstat(path)
        return st.st_mtime < cutoff

    try:
        qdir = os.path.join(root, "queue")
        for n in os.listdir(qdir) if os.path.isdir(qdir) else []:
            p = os.path.join(qdir, n)
            if expendable(n) and not os.path.islink(p) and old(p):
                os.remove(p)
        adir = os.path.join(root, "agents")
        for sess in os.listdir(adir) if os.path.isdir(adir) else []:
            sp = os.path.join(adir, sess)
            if os.path.islink(sp) or not os.path.isdir(sp) or not old(sp):
                continue
            for f in os.listdir(sp):
                fp = os.path.join(sp, f)
                if expendable(f) and not os.path.islink(fp):
                    os.remove(fp)
            if not os.listdir(sp):
                os.rmdir(sp)
    except OSError:
        pass


def fmt_report(name, model, res, warn_tokens):
    """One-line report string shown to the orchestrator."""
    parts = [f"~{res['current'] / 1000:.0f}k tokens"]
    if res["peak"] > res["current"] + 2000:
        parts.append(f"peak ~{res['peak'] / 1000:.0f}k")
    if res["compactions"]:
        parts.append(f"COMPACTED x{res['compactions']}")
    line = f"[subagent-gauge] {sanitize(name, 80)}"
    if model:
        line += f" ({sanitize(model, 40)})"
    line += ": " + ", ".join(parts)
    if res.get("stale") or not res.get("terminal", True):
        line += " [reading may lag: final transcript row not yet flushed]"
    if res["current"] >= warn_tokens or res["compactions"]:
        line += (" — OVER THRESHOLD: prefer spawning a fresh agent over "
                 "re-tasking this one; long-context agents degrade.")
    return line
