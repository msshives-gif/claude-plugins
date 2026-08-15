"""Config for cross-session-send-guard: defaults <- JSON file <- env.

Same pattern as subagent-context but its own namespace
(CROSS_SESSION_SEND_GUARD_* / ~/.claude/cross-session-send-guard.json).
Defaults are deliberately LOWER than subagent-context's: a cold peer
wake replays the peer's whole transcript at full input price, so a
smaller peer is already worth flagging (the motivating incident was
~100k tokens).
"""
import json
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
_PER_MODEL_KEYS = ("warn_tokens", "block_tokens")


def _coerce(default, value):
    """_core._coerce plus a float branch (usd_per_mtok)."""
    if isinstance(default, float):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value) if value >= 0 else None
        if isinstance(value, str):
            try:
                v = float(value)
            except ValueError:
                return None
            return v if v >= 0 else None
        return None
    return _core._coerce(default, value)


def load_config():
    path = os.environ.get(
        _ENV_PREFIX + "CONFIG",
        os.path.expanduser("~/.claude/cross-session-send-guard.json"))
    file_cfg = {}
    try:
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
                kept = {k: _coerce(DEFAULTS[k], v)
                        for k, v in overrides.items()
                        if k in _PER_MODEL_KEYS}
                kept = {k: v for k, v in kept.items() if v is not None}
                if kept:
                    clean[pat] = kept
            cfg["models"] = clean
    cfg["cache_ttl_seconds"] = max(60, cfg["cache_ttl_seconds"])
    cfg["sessions_dir"] = os.path.expanduser(cfg["sessions_dir"])
    cfg["projects_dir"] = os.path.expanduser(cfg["projects_dir"])
    return cfg


def thresholds(cfg, model):
    """Longest matching model-substring override, like the sibling."""
    eff = {k: cfg[k] for k in _PER_MODEL_KEYS}
    model = str(model or "").lower()
    best = ""
    for pat in cfg.get("models", {}):
        if len(pat) > len(best) and pat.lower() in model:
            best = pat
    if best:
        eff.update(cfg["models"][best])
    return eff
