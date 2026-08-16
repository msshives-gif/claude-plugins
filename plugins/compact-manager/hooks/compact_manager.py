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
import re
import stat as stat_module
import sys
import time

SCHEMA_VERSION = 1

_DEFAULTS = {
    # off: installing changes nothing. advisory: measure own context,
    # inject soft/hard advisories, persist + reinject the wake packet.
    # managed: + Layer-2 watcher coordination (hooks/managed.py; a
    # watcher must still be attached per session via bin/compact-manager).
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
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        return None  # garbage keeps the default, not False
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
    # Sanity: percentages in (0, 1], soft below hard. Zero would make
    # every reading an instant advisory — restore the default instead.
    for k in ("soft_pct", "hard_pct"):
        if cfg[k] <= 0:
            cfg[k] = _DEFAULTS[k]
        cfg[k] = min(cfg[k], 1.0)
    cfg["rearm_band_pct"] = min(cfg["rearm_band_pct"], 1.0)
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
    # Same sanity clamps load_config applies to the globals — a typo'd
    # per-model override must degrade, not silence advisories forever.
    for k in ("soft_pct", "hard_pct"):
        if eff[k] <= 0:
            eff[k] = cfg[k]  # globals are already validated
        eff[k] = min(eff[k], 1.0)
    eff["context_window"] = max(10_000, eff["context_window"])
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
    """O_EXCL sidecar lock (portable — deliberately not fcntl/msvcrt,
    which don't share semantics across platforms). Returns a release
    callable. Stale locks older than 10s are broken (a crashed hook
    must not wedge the session's state forever). Residual races
    (re-checked rename break; token-checked release) need a >10s-stale
    lock plus a sub-ms interleave; hooks die at their 5s timeout, so a
    LIVE owner can never look stale. Worst case is one lost state
    update or duplicate reorientation — both harmless by design."""
    deadline = time.monotonic() + timeout
    token = f"{os.getpid()}.{time.monotonic_ns()}"
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                         0o600)
            os.write(fd, token.encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 10:
                    # Break via atomic rename (of concurrent racers,
                    # exactly one replace() succeeds), then RE-CHECK the
                    # displaced file: a peer may have finished its own
                    # break and re-acquired between our staleness check
                    # and the rename. A displaced FRESH lock is restored
                    # via O_EXCL so a third acquirer is never clobbered;
                    # if the path was retaken meanwhile, the displaced
                    # owner simply loses its lock file (its token-checked
                    # release stays safe). This mutual-exclusion is
                    # best-effort, not perfect: overlapping holders
                    # remain possible after a >10s-stale break, costing
                    # at worst one lost state update or a duplicate
                    # reorientation — both harmless by design.
                    stale = f"{lock_path}.stale.{os.getpid()}"
                    os.replace(lock_path, stale)
                    if time.time() - os.path.getmtime(stale) > 10:
                        os.unlink(stale)
                        continue
                    try:
                        with open(stale) as sfh:
                            displaced = sfh.read(64)
                        rfd = os.open(lock_path,
                                      os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                      0o600)
                        os.write(rfd, displaced.encode())
                        os.close(rfd)
                    except OSError:
                        pass
                    os.unlink(stale)
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise LockTimeout(lock_path)
            time.sleep(0.05)

    def release():
        # Owner-aware: if a peer stale-broke this lock and re-took it,
        # the file now holds THEIR token — leave it alone (audit
        # finding: unconditional unlink could free a successor's lock).
        try:
            with open(lock_path) as fh:
                if fh.read(64) != token:
                    return
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


_STATE_DEFAULTS = {"schema": SCHEMA_VERSION, "inode": None, "size": 0,
                   "offset": 0, "current": 0, "peak": 0, "boundaries": 0,
                   "auto_boundaries": 0, "model": "",
                   "advisory_level": "none", "armed_at_ts": 0,
                   "packet_seq": 0, "last_drained_packet_seq": -1,
                   "discard_to_newline": False}


def load_state(paths):
    """Field-validated load: a corrupt value resets THAT field to its
    default (self-recovery) instead of making every later hook raise
    on the same bad file forever (audit finding)."""
    st = dict(_STATE_DEFAULTS)
    try:
        with open(paths["state"]) as fh:
            raw = json.load(fh)
    except Exception:
        return st
    if not isinstance(raw, dict):
        return st
    for k, dflt in _STATE_DEFAULTS.items():
        v = raw.get(k, dflt)
        if k == "inode":
            st[k] = v if (v is None or isinstance(v, int)
                          and not isinstance(v, bool)) else None
        elif k == "advisory_level":
            st[k] = v if v in ("none", "soft", "hard") else "none"
        elif k == "model":
            st[k] = v if isinstance(v, str) else ""
        elif k == "discard_to_newline":
            st[k] = v if isinstance(v, bool) else False
        elif k == "armed_at_ts":
            # Timestamp: non-negative int of any size, or finite
            # non-negative float. math.isfinite(huge_int) raises
            # OverflowError, which would wedge every hook (audit
            # finding) — so ints skip the finiteness check.
            ok = (isinstance(v, int) and not isinstance(v, bool)
                  and v >= 0) or \
                 (isinstance(v, float) and math.isfinite(v) and v >= 0)
            st[k] = v if ok else dflt
        else:
            # Counters/offsets/seqs: exact ints only — a float offset
            # would make fh.seek() raise on EVERY invocation, leaving
            # the hook permanently inert (audit finding). Floor is -1
            # for last_drained_packet_seq, 0 for everything else.
            floor = -1 if k == "last_drained_packet_seq" else 0
            st[k] = v if (isinstance(v, int)
                          and not isinstance(v, bool)
                          and v >= floor) else dflt
    return st


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

# Per-invocation read budget. A tail bigger than this is processed
# across successive hook invocations; a single LINE bigger than this
# is skipped via discard_to_newline (otherwise every invocation would
# reread it and stall against the 5s hook deadline — audit finding).
SCAN_MAX_BYTES = 16_000_000


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
                  peak=0, boundaries=0, auto_boundaries=0,
                  discard_to_newline=False)
    if stat.st_size == st.get("offset", 0):
        st["size"] = stat.st_size
        return st
    try:
        with open(transcript, "rb") as fh:
            fh.seek(st.get("offset", 0))
            chunk = fh.read(SCAN_MAX_BYTES)
    except OSError:
        return st
    if st.get("discard_to_newline"):
        nl = chunk.find(b"\n")
        if nl == -1:
            st["offset"] = st.get("offset", 0) + len(chunk)
            st["size"] = stat.st_size
            return st
        st["offset"] = st.get("offset", 0) + nl + 1
        st["discard_to_newline"] = False
        chunk = chunk[nl + 1:]
    end = chunk.rfind(b"\n")
    if end == -1:
        if len(chunk) >= SCAN_MAX_BYTES:
            st["offset"] = st.get("offset", 0) + len(chunk)
            st["discard_to_newline"] = True
            st["size"] = stat.st_size
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
            # The pre-compact reading is stale the moment the boundary
            # lands; without this reset the next advisory tells a
            # freshly-compacted session its context is still full
            # (observed live, M2 verification). postTokens is the
            # compaction's own post-size; a later usage row overrides.
            post = md.get("postTokens")
            st["current"] = post if isinstance(post, int) and post >= 0 else 0
        m = row.get("message")
        if not isinstance(m, dict):
            continue
        if m.get("model"):
            st["model"] = str(m["model"])
        u = m.get("usage")
        if not isinstance(u, dict):
            continue
        counts = [u.get(k) for k in _USAGE_KEYS
                  if isinstance(u.get(k), int)
                  and not isinstance(u.get(k), bool)]
        if not counts:
            continue
        total = sum(counts)
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

    # Re-arm downward STEPWISE: hard drops to an armed soft first, and
    # only to none below the soft re-arm point. A straight reset to
    # none would emit a fresh soft advisory on a purely downward move
    # (audit finding: hard at 85% -> drop to 71% must stay silent).
    while level != "none" and pct < thresholds[level] - rearm_band:
        level = "soft" if level == "hard" else "none"

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


# The most a hook reads from stdin. PostToolUse payloads embed the
# tool_response, which can be huge; a truncated payload fails open to
# a skipped scan, so keep the cap generous.
STDIN_MAX_BYTES = 10_000_000


def read_payload():
    # buffer.read so the cap is BYTES (a str read counts characters
    # and could pull ~4x the bytes); json.loads accepts bytes.
    return json.loads(sys.stdin.buffer.read(STDIN_MAX_BYTES))


def delivery_cap(cfg):
    """Reorientation-text budget: the configured excerpt plus framing."""
    return cfg["handoff_excerpt_bytes"] + 2_000


# -------------------------------------------------------------- packet

def write_packet(cfg, paths, st, trigger, custom_instructions, cwd):
    """PreCompact: persist the wake packet. seq is an independent
    monotonic counter; base_compaction_count records the boundary count
    BEFORE this compaction so the drain can tell when it completed."""
    excerpt, fresh = "", False
    try:
        mtime = os.path.getmtime(paths["handoff"])
        fresh = mtime >= st.get("armed_at_ts", 0)
        # Binary read so the knob really caps BYTES — a text-mode
        # read(n) counts characters, and a multibyte handoff could
        # blow the cap several times over (audit finding).
        with open(paths["handoff"], "rb") as fh:
            excerpt = fh.read(cfg["handoff_excerpt_bytes"]).decode(
                "utf-8", errors="replace")
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


def reorientation_text(packet, cap=6000):
    """cap covers excerpt + framing; callers derive it from
    handoff_excerpt_bytes so raising that knob isn't silently undone
    at delivery (audit finding)."""
    parts = [f"[compact-manager] Reorientation after compaction "
             f"(context was ~{packet.get('pre_current', 0) / 1000:.0f}k, "
             f"peak ~{packet.get('pre_peak', 0) / 1000:.0f}k)."]
    ci = packet.get("custom_instructions") or ""
    # A managed-mode injection prefixes a correlation nonce
    # ("[cm-…] …"); it is watcher plumbing, not instructions — strip
    # it from the display (the raw value stays in the packet).
    m = re.match(r"\[cm-[0-9a-f]{4,32}\]\s*", ci)
    if m:
        ci = ci[m.end():]
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
    return reorientation_text(packet, delivery_cap(cfg))


# --------------------------------------------------------------- prune

_PRUNE_MARKER = ".last-prune"


def prune_state(cfg):
    """Best-effort TTL reaper for per-session files (state/packets/
    handoff/locks). Runs at most once per day; age-based, so a live
    session's freshly-written files are never touched. Deletion is
    deliberately narrow (audit finding): only inside a dir carrying our
    sentinel, never through symlinked subdirs, only regular files whose
    names match what this plugin generates, ages via lstat. Errors
    ignored — housekeeping must never cost a hook its deadline."""
    try:
        base = cfg["state_dir"]
        if not os.path.isfile(os.path.join(base, _SENTINEL)):
            return  # never reap a dir this plugin didn't mark
        marker = os.path.join(base, _PRUNE_MARKER)
        now = time.time()
        try:
            if now - os.path.getmtime(marker) < 86_400:
                return
        except OSError:
            pass
        with open(marker, "w") as fh:
            fh.write(str(now))
        cutoff = now - cfg["state_ttl_days"] * 86_400
        is_junction = getattr(os.path, "isjunction", lambda p: False)
        for sub, ext in (("state", ".json"), ("packets", ".json"),
                         ("handoff", ".md"), ("locks", ".lock")):
            d = os.path.join(base, sub)
            if os.path.islink(d) or is_junction(d):
                continue
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if not name.endswith(ext):
                    continue
                # Only stems this plugin can have generated
                # (path_component output is a fixed point).
                stem = name[:-len(ext)]
                if not stem or stem != path_component(stem):
                    continue
                p = os.path.join(d, name)
                try:
                    stt = os.lstat(p)
                    if not stat_module.S_ISREG(stt.st_mode):
                        continue
                    if stt.st_mtime < cutoff:
                        os.unlink(p)
                except OSError:
                    pass
    except Exception:
        pass


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
