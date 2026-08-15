"""Shared library for compact-manager: config, incremental own-session
measurement, advisory decisions, and the wake packet.

Hand-ported subset of the sibling's subagent_context.py (locking,
sentinel, sanitize, coercion) plus what is genuinely new here:
incremental scanning of the session's OWN transcript and the
compaction-survival packet. This copy is owned by compact-manager —
deliberately NOT drift-synced (it diverges: float config, boundary
detection via compactMetadata, per-model context windows).

Ground truth for transcript shapes: docs/spikes/compact-manager.md and
fixtures/transcripts/compact_rows.jsonl (real sanitized rows).
Everything fails open; hooks exit 0 on every path.
"""
import json
import math
import os
import sys
import time

SCHEMA_VERSION = 1

_DEFAULTS = {
    # off: installing changes nothing. advisory: measure own context,
    # inject soft/hard advisories, persist + reinject the wake packet.
    # managed: + Layer-2 watcher coordination (NOT BUILT YET: managed
    # currently behaves as advisory; the mode value is reserved).
    "mode": "off",
    # Fractions of the model's context window.
    "soft_pct": 0.70,
    "hard_pct": 0.80,
    # Re-arm an advisory level only after pct falls this far below it
    # (or after a compaction). Prevents boundary chatter.
    "rearm_band_pct": 0.08,
    # Window size in tokens; per-model overrides via "models".
    "context_window": 200_000,
    # Show advisories to the human too.
    "system_message": True,
    # Max bytes of the model-written handoff embedded in the packet.
    "handoff_excerpt_bytes": 4_000,
    "state_dir": "~/.claude/compact-manager",
    "ledger": True,
    "ledger_max_bytes": 5_000_000,
    "state_ttl_days": 7,
}

_ENV_PREFIX = "COMPACT_MANAGER_"
_CHOICES = {"mode": ("off", "advisory", "managed")}
_PER_MODEL_KEYS = ("soft_pct", "hard_pct", "context_window")
_CONFIG_MAX_BYTES = 1_000_000


def _log_error(msg):
    print(f"compact-manager: {msg}", file=sys.stderr)


def _coerce(default, value, choices=None):
    """Sibling's coercion plus float (pct knobs). None = unusable."""
    if choices is not None:
        v = value.strip().lower() if isinstance(value, str) else value
        return v if v in choices else None
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return None
    if isinstance(default, float):
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v if 0 <= v and math.isfinite(v) else None
        return None
    if isinstance(default, int):
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                return None
        return value if isinstance(value, int) and value >= 0 else None
    return value if isinstance(value, str) and value.strip() else None


def load_config():
    path = os.environ.get(
        _ENV_PREFIX + "CONFIG",
        os.path.expanduser("~/.claude/compact-manager.json"))
    file_cfg = {}
    try:
        if os.path.isfile(path) and os.path.getsize(path) <= _CONFIG_MAX_BYTES:
            with open(path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                file_cfg = loaded
    except Exception:
        _log_error(f"config file {path} unreadable; using defaults")

    cfg = dict(_DEFAULTS)
    for k, dflt in _DEFAULTS.items():
        for raw in (file_cfg.get(k), os.environ.get(_ENV_PREFIX + k.upper())):
            if raw is None:
                continue
            v = _coerce(dflt, raw, _CHOICES.get(k))
            if v is None:
                _log_error(f"config {k}={raw!r} is unusable; ignoring it")
            else:
                cfg[k] = v
    cfg["models"] = {}
    raw_models = (os.environ.get(_ENV_PREFIX + "MODELS")
                  or file_cfg.get("models"))
    if raw_models is not None:
        if isinstance(raw_models, str):
            try:
                raw_models = json.loads(raw_models)
            except ValueError:
                raw_models = None
        if isinstance(raw_models, dict):
            clean = {}
            for pat, overrides in raw_models.items():
                if not isinstance(overrides, dict) or not pat.strip():
                    continue
                kept = {}
                for k, v in overrides.items():
                    if k not in _PER_MODEL_KEYS:
                        continue
                    c = _coerce(_DEFAULTS[k], v)
                    if c is not None:
                        kept[k] = c
                if kept:
                    clean[pat] = kept
            cfg["models"] = clean
    # Sanity: percentages in (0, 1], soft below hard.
    for k in ("soft_pct", "hard_pct", "rearm_band_pct"):
        cfg[k] = min(cfg[k], 1.0)
    if cfg["soft_pct"] > cfg["hard_pct"]:
        cfg["soft_pct"] = cfg["hard_pct"]
    cfg["context_window"] = max(10_000, cfg["context_window"])
    cfg["ledger_max_bytes"] = max(65_536, cfg["ledger_max_bytes"])
    cfg["state_ttl_days"] = max(1, cfg["state_ttl_days"])
    cfg["state_dir"] = os.path.abspath(os.path.expanduser(cfg["state_dir"]))
    return cfg


def window_for(cfg, model):
    """Effective soft/hard/window for a model id (longest substring
    pattern wins, like the sibling's thresholds())."""
    eff = {k: cfg[k] for k in _PER_MODEL_KEYS}
    model = str(model or "").lower()
    best = ""
    for pat in cfg.get("models", {}):
        if len(pat) > len(best) and pat.lower() in model:
            best = pat
    if best:
        eff.update(cfg["models"][best])
    if eff["soft_pct"] > eff["hard_pct"]:
        eff["soft_pct"] = eff["hard_pct"]
    return eff


# ---------------------------------------------------------------- text

def sanitize(text, limit=600):
    """One printable line, length-capped (sibling's rule)."""
    cleaned = "".join(
        ch if ch.isprintable() else " " for ch in str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def path_component(value):
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in str(value)]
    out = "".join(keep).strip("._") or "unknown"
    return out[:120]


# ------------------------------------------------------------- locking

class LockTimeout(Exception):
    pass


def _locked_open(lock_path, timeout=2.0):
    """O_EXCL sidecar lock (portable). Returns a release callable.
    Stale locks older than 10s are broken (a crashed hook must not
    wedge the session's state forever)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                         0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 10:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise LockTimeout(lock_path)
            time.sleep(0.05)

    def release():
        try:
            os.unlink(lock_path)
        except OSError:
            pass
    return release


def _private_makedirs(path):
    os.makedirs(path, mode=0o700, exist_ok=True)


_SENTINEL = ".compact-manager-state"


def _write_sentinel(cfg):
    p = os.path.join(cfg["state_dir"], _SENTINEL)
    if not os.path.exists(p):
        _private_makedirs(cfg["state_dir"])
        with open(p, "w") as fh:
            fh.write(str(SCHEMA_VERSION))


def state_paths(cfg, session_id):
    base = cfg["state_dir"]
    sid = path_component(session_id)
    return {
        "state": os.path.join(base, "state", f"{sid}.json"),
        "packet": os.path.join(base, "packets", f"{sid}.json"),
        "handoff": os.path.join(base, "handoff", f"{sid}.md"),
        "lock": os.path.join(base, "locks", f"{sid}.lock"),
    }


def load_state(paths):
    try:
        with open(paths["state"]) as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {"schema": SCHEMA_VERSION, "inode": None, "size": 0,
            "offset": 0, "current": 0, "peak": 0, "boundaries": 0,
            "auto_boundaries": 0, "model": "", "advisory_level": "none",
            "armed_at_tokens": 0, "packet_seq": 0,
            "last_drained_packet_seq": -1}


def save_state(cfg, paths, st):
    _write_sentinel(cfg)
    _private_makedirs(os.path.dirname(paths["state"]))
    tmp = paths["state"] + f".{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths["state"])


# --------------------------------------------------------- measurement

_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens", "output_tokens")


def incremental_scan(st, transcript):
    """Advance st over the transcript's new bytes. Compaction appends
    (spike S3), so normal operation is pure forward reads; an inode
    change or shrink means rotation/replacement -> full reparse.
    Consumes only COMPLETE lines (a trailing fragment is a half-flushed
    row). Boundary detection keys on compactMetadata (deterministic and
    source-attributed), not on summary-row counting."""
    try:
        stat = os.stat(transcript)
    except OSError:
        return st
    if st.get("inode") != stat.st_ino or stat.st_size < st.get("size", 0):
        st = dict(st, inode=stat.st_ino, size=0, offset=0, current=0,
                  peak=0, boundaries=0, auto_boundaries=0)
    if stat.st_size == st.get("offset", 0):
        st["size"] = stat.st_size
        return st
    try:
        with open(transcript, "rb") as fh:
            fh.seek(st.get("offset", 0))
            chunk = fh.read()
    except OSError:
        return st
    end = chunk.rfind(b"\n")
    if end == -1:
        return st
    for raw in chunk[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        md = row.get("compactMetadata")
        if isinstance(md, dict):
            st["boundaries"] = st.get("boundaries", 0) + 1
            if md.get("trigger") == "auto":
                st["auto_boundaries"] = st.get("auto_boundaries", 0) + 1
        m = row.get("message")
        if not isinstance(m, dict):
            continue
        if m.get("model"):
            st["model"] = str(m["model"])
        u = m.get("usage")
        if not isinstance(u, dict):
            continue
        if not any(isinstance(u.get(k), int) for k in _USAGE_KEYS):
            continue
        total = sum(u.get(k) for k in _USAGE_KEYS
                    if isinstance(u.get(k), int))
        if m.get("stop_reason") is not None:
            st["current"] = total
            st["peak"] = max(st.get("peak", 0), total)
        elif st.get("current", 0) == 0:
            st["current"] = total
            st["peak"] = max(st.get("peak", 0), total)
    st["offset"] = st.get("offset", 0) + end + 1
    st["size"] = stat.st_size
    return st


# ------------------------------------------------------------ advisory

def advise(st, eff, handoff_path, rearm_band=0.08):
    """Pure decision: -> (new_level, text_or_None). One advisory per
    genuine upward crossing; re-arm only when pct falls below
    level - rearm_band, or after a compaction (caller resets level on a
    boundary advance)."""
    window = eff["context_window"]
    pct = st.get("current", 0) / window if window else 0
    level = st.get("advisory_level", "none")
    order = {"none": 0, "soft": 1, "hard": 2}
    thresholds = {"soft": eff["soft_pct"], "hard": eff["hard_pct"]}

    # Re-arm downward.
    if level != "none" and pct < thresholds.get(level, 1) - rearm_band:
        level = "none"

    target = "none"
    if pct >= eff["hard_pct"]:
        target = "hard"
    elif pct >= eff["soft_pct"]:
        target = "soft"
    if order[target] <= order[level]:
        return level, None
    ctx_k = st.get("current", 0) / 1000
    if target == "soft":
        text = (f"[compact-manager] Your context is ~{pct:.0%} full "
                f"(~{ctx_k:.0f}k tokens). Finish the current step, then "
                f"wrap at a natural boundary: write your task state "
                f"(goals, done, next, key paths, open decisions) to "
                f"{handoff_path} and compact — or ask the user to run "
                f"/compact. Reorientation after compaction depends on "
                f"that file.")
    else:
        text = (f"[compact-manager] Your context is ~{pct:.0%} full "
                f"(~{ctx_k:.0f}k tokens) — compaction is imminent. Save "
                f"your handoff to {handoff_path} NOW; native "
                f"auto-compact fires without warning near the window "
                f"limit.")
    return target, text


# -------------------------------------------------------------- packet

def write_packet(cfg, paths, st, trigger, custom_instructions, cwd):
    """PreCompact: persist the wake packet. seq is an independent
    monotonic counter; base_compaction_count records the boundary count
    BEFORE this compaction so the drain can tell when it completed."""
    excerpt, fresh = "", False
    try:
        mtime = os.path.getmtime(paths["handoff"])
        fresh = mtime >= st.get("armed_at_ts", 0)
        with open(paths["handoff"], "r", errors="replace") as fh:
            excerpt = fh.read(cfg["handoff_excerpt_bytes"])
    except OSError:
        pass
    seq = st.get("packet_seq", 0) + 1
    packet = {
        "seq": seq,
        "base_compaction_count": st.get("boundaries", 0),
        "written_at": time.time(),
        "trigger": trigger,
        "custom_instructions": sanitize(custom_instructions, 500),
        "pre_current": st.get("current", 0),
        "pre_peak": st.get("peak", 0),
        "handoff_excerpt": excerpt,
        "handoff_fresh": fresh,
        "handoff_path": paths["handoff"],
        "cwd": sanitize(cwd, 200),
    }
    _private_makedirs(os.path.dirname(paths["packet"]))
    tmp = paths["packet"] + f".{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(packet, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths["packet"])
    return seq


def load_packet(paths):
    try:
        with open(paths["packet"]) as fh:
            p = json.load(fh)
        return p if isinstance(p, dict) else None
    except Exception:
        return None


def reorientation_text(packet, cap=4000):
    parts = [f"[compact-manager] Reorientation after compaction "
             f"(context was ~{packet.get('pre_current', 0) / 1000:.0f}k, "
             f"peak ~{packet.get('pre_peak', 0) / 1000:.0f}k)."]
    ci = packet.get("custom_instructions")
    if ci:
        parts.append(f'Compact instructions in effect: "{ci}".')
    if packet.get("handoff_fresh") and packet.get("handoff_excerpt"):
        parts.append("Your pre-compaction handoff notes:\n"
                     + packet["handoff_excerpt"])
    else:
        parts.append(f"No fresh handoff file was found at "
                     f"{packet.get('handoff_path', '?')} — re-read any "
                     f"plan/todo files you were maintaining.")
    parts.append("Resume the task; verify state from files/commands "
                 "rather than trusting the summary alone.")
    return "\n".join(parts)[:cap]


def drain_packet(cfg, paths, st):
    """Exactly-once-best-effort: return the packet text if a compaction
    newer than the packet's base count has landed AND this packet seq
    hasn't been drained; advance the CAS under the caller's lock.
    Returns None otherwise. (At-most-once durable: marking before emit
    means a crash can lose one reorientation — acceptable by design.)"""
    packet = load_packet(paths)
    if not packet:
        return None
    if packet.get("seq", 0) <= st.get("last_drained_packet_seq", -1):
        return None
    if st.get("boundaries", 0) <= packet.get("base_compaction_count", 0):
        return None  # the compaction it precedes hasn't landed yet
    st["last_drained_packet_seq"] = packet["seq"]
    return reorientation_text(packet)


# -------------------------------------------------------------- ledger

def ledger_append(cfg, record):
    if not cfg.get("ledger", True):
        return
    try:
        _write_sentinel(cfg)
        path = os.path.join(cfg["state_dir"], "ledger.jsonl")
        try:
            if os.path.getsize(path) > cfg["ledger_max_bytes"]:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a") as fh:
            fh.write(json.dumps(dict(record, ts=time.time())) + "\n")
        os.chmod(path, 0o600)
    except Exception:
        pass
