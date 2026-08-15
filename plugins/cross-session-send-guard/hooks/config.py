"""Config for cross-session-send-guard: defaults <- JSON file <- env.

Same pattern as subagent-context but its own namespace
(CROSS_SESSION_SEND_GUARD_* / ~/.claude/cross-session-send-guard.json).
Defaults are deliberately LOWER than subagent-context's: a cold peer
wake replays the peer's whole transcript at full input price, so a
smaller peer is already worth flagging (the motivating incident was
~100k tokens).
"""
import json
import math
import os
import sys

_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _CORE_DIR)
import _core  # noqa: E402

DEFAULTS = {
    # Master switch.
    "enabled": True,
    # At/above this measured peer size the warning is injected.
    "warn_tokens": 50_000,
    # At/above this AND cold, a root-session send needs confirmation
    # ("ask"). 0 disables the gate.
    "block_tokens": 150_000,
    # Peer transcript idle longer than this (mtime age, seconds) counts
    # as cold — its prompt cache has expired and a wake replays the
    # whole transcript at full price.
    "cache_ttl_seconds": 3600,
    # Price used ONLY for the estimated cold-read cost line (USD per
    # million input tokens). An estimate, not billing truth.
    "usd_per_mtok": 3.0,
    # Also surface warnings to the human as a hook systemMessage.
    "system_message": True,
    # Transcripts bigger than this aren't parsed (the scanner has no
    # internal deadline; stay inside the 5s hook budget). Oversized
    # peers still warn as size-unknown and still gate when cold.
    "measure_max_bytes": 50_000_000,
    # Where the harness's per-session registry and transcripts live,
    # and the proc filesystem for liveness checks. Overridable for
    # tests only.
    "sessions_dir": "~/.claude/sessions",
    "projects_dir": "~/.claude/projects",
    "proc_root": "/proc",
}

_ENV_PREFIX = "CROSS_SESSION_SEND_GUARD_"
# Bigger config files than this are not ours (bounded reads: nothing
# in this hook may stall the 5s budget).
_CONFIG_MAX_BYTES = 1_000_000


def _coerce(default, value):
    """_core._coerce plus a float branch (usd_per_mtok)."""
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
            return v if v >= 0 and math.isfinite(v) else None
        return None
    return _core._coerce(default, value)


def load_config():
    path = os.environ.get(
        _ENV_PREFIX + "CONFIG",
        os.path.expanduser("~/.claude/cross-session-send-guard.json"))
    file_cfg = {}
    try:
        if os.path.isfile(path) and os.path.getsize(path) <= _CONFIG_MAX_BYTES:
            with open(path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                file_cfg = loaded
    except FileNotFoundError:
        pass
    except Exception:
        print(f"cross-session-send-guard: config {path} unreadable; "
              "using defaults", file=sys.stderr)

    cfg = dict(DEFAULTS)
    for k, dflt in DEFAULTS.items():
        for raw in (file_cfg.get(k), os.environ.get(_ENV_PREFIX + k.upper())):
            if raw is None:
                continue
            v = _coerce(dflt, raw)
            if v is None:
                print(f"cross-session-send-guard: config {k}={raw!r} is "
                      "unusable; ignoring it", file=sys.stderr)
            else:
                cfg[k] = v
    cfg["cache_ttl_seconds"] = max(60, cfg["cache_ttl_seconds"])
    # A nonzero gate below the warn threshold is unreachable (the hook
    # returns early below warn_tokens): pull it up rather than let it
    # silently never fire.
    if cfg["block_tokens"] and cfg["block_tokens"] < cfg["warn_tokens"]:
        cfg["block_tokens"] = cfg["warn_tokens"]
    cfg["sessions_dir"] = os.path.expanduser(cfg["sessions_dir"])
    cfg["projects_dir"] = os.path.expanduser(cfg["projects_dir"])
    return cfg
