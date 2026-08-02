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
    cfg["state_dir"] = os.path.expanduser(cfg["state_dir"])
    return cfg


def _log_error(msg):
    # Errors go to stderr; the shell wrappers redirect stderr to
    # <state_dir>/errors.log.
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
           "terminal": False, "rows": 0}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("isCompactSummary") or row.get("type") == "summary":
                res["compactions"] += 1
            msg = row.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue

            def tok(key):
                v = u.get(key, 0)
                return v if isinstance(v, int) else 0

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
            elif not res["terminal"]:
                res["current"] = total
                res["prompt"] = prompt
                res["peak"] = max(res["peak"], total)
    return res


def measure(path, grace_ms):
    """Measure a transcript, waiting out the stop-time flush race.

    At SubagentStop time the final usage row is often not yet written
    (verified 2026-07-31), and a previously-used agent already has stale
    rows — so "a usage row exists" is NOT enough. We wait until the file
    has been stable (same size) across two polls, or the grace period
    ends, then parse. stat() polls are cheap; the file is parsed at most
    twice.
    """
    deadline = time.monotonic() + grace_ms / 1000.0
    try:
        last_size = os.stat(path).st_size
    except OSError:
        return None
    stable = 0
    while time.monotonic() < deadline and stable < 2:
        time.sleep(0.25)
        try:
            size = os.stat(path).st_size
        except OSError:
            return None
        stable = stable + 1 if size == last_size else 0
        last_size = size
    try:
        res = _scan(path)
    except OSError:
        return None
    if res["rows"] == 0:
        # One retry after the remaining grace: maybe the first row is
        # still flushing (brand-new agent).
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        try:
            res = _scan(path)
        except OSError:
            return None
    return res if res["rows"] else None


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

def _locked_open(path, mode):
    fh = open(path, mode)
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def state_paths(cfg, session_id):
    root = cfg["state_dir"]
    return {
        "agents": os.path.join(root, "agents", session_id or "unknown"),
        "queue": os.path.join(root, "queue", f"{session_id or 'unknown'}.jsonl"),
        "ledger": os.path.join(root, "ledger.jsonl"),
        "errors": os.path.join(root, "errors.log"),
    }


def write_agent_state(cfg, session_id, record):
    d = state_paths(cfg, session_id)["agents"]
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{record['agent_id']}.tmp")
    dst = os.path.join(d, f"{record['agent_id']}.json")
    with open(tmp, "w") as fh:
        json.dump(record, fh)
    os.replace(tmp, dst)


def load_agent_states(cfg, session_id):
    d = state_paths(cfg, session_id)["agents"]
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n)) as fh:
                rec = json.load(fh)
            if isinstance(rec, dict):
                out.append(rec)
        except Exception:
            continue
    return out


def enqueue(cfg, session_id, text):
    q = state_paths(cfg, session_id)["queue"]
    os.makedirs(os.path.dirname(q), exist_ok=True)
    with _locked_open(q, "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "text": text}) + "\n")


def drain_queue(cfg, session_id, batch_max):
    """Read-and-truncate under lock. Returns list of report texts."""
    q = state_paths(cfg, session_id)["queue"]
    if not os.path.isfile(q) or os.path.getsize(q) == 0:
        return []
    texts = []
    with _locked_open(q, "r+") as fh:
        lines = fh.readlines()
        fh.seek(0)
        fh.truncate()
        keep = lines[batch_max:]
        if keep:
            fh.writelines(keep)
    for line in lines[:batch_max]:
        try:
            texts.append(json.loads(line)["text"])
        except Exception:
            continue
    return texts


def ledger_append(cfg, entry):
    if not cfg["ledger"]:
        return
    path = state_paths(cfg, "")["ledger"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > cfg["ledger_max_bytes"]:
            os.replace(path, path + ".1")
    except OSError:
        pass
    entry = {"v": SCHEMA_VERSION, "ts": time.time(), **entry}
    with _locked_open(path, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def prune_stale(cfg):
    """Best-effort removal of queue files and agent-state dirs untouched
    for state_ttl_days. Called opportunistically from the observer."""
    cutoff = time.time() - cfg["state_ttl_days"] * 86400
    root = cfg["state_dir"]
    try:
        qdir = os.path.join(root, "queue")
        for n in os.listdir(qdir) if os.path.isdir(qdir) else []:
            p = os.path.join(qdir, n)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        adir = os.path.join(root, "agents")
        for sess in os.listdir(adir) if os.path.isdir(adir) else []:
            sp = os.path.join(adir, sess)
            if os.path.getmtime(sp) < cutoff:
                for f in os.listdir(sp):
                    os.remove(os.path.join(sp, f))
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
    line = f"[subagent-gauge] {name}"
    if model:
        line += f" ({model})"
    line += ": " + ", ".join(parts)
    if res["current"] >= warn_tokens or res["compactions"]:
        line += (" — OVER THRESHOLD: prefer spawning a fresh agent over "
                 "re-tasking this one; long-context agents degrade.")
    return line
