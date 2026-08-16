#!/usr/bin/env python3
"""One-screen readout for the suite: every plugin's effective config
(with where each value came from: default, config file, or env var),
hook wiring found in ~/.claude/settings*.json, on-disk state, and live
compact-manager watchers.

Usage: tools/status.py [--json]

Read-only. Imports each plugin's own config loader so the numbers
shown are exactly what the hooks compute — never a re-parse that can
drift. A plugin whose loader fails to import is reported, not fatal.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PLUGINS = [
    {
        "name": "compact-manager",
        "module": "plugins/compact-manager/hooks/compact_manager.py",
        "defaults_attr": "_DEFAULTS",
        "env_prefix": "COMPACT_MANAGER_",
        "config_path": "~/.claude/compact-manager.json",
        "config_max_bytes": 1_000_000,
    },
    {
        "name": "subagent-context",
        "module": "plugins/subagent-context/hooks/subagent_context.py",
        "defaults_attr": "_DEFAULTS",
        "env_prefix": "SUBAGENT_CONTEXT_",
        "config_path": "~/.claude/subagent-context.json",
        "config_max_bytes": None,  # this loader reads any size
    },
    {
        "name": "cross-session-send-guard",
        "module": "plugins/cross-session-send-guard/hooks/config.py",
        "defaults_attr": "DEFAULTS",
        "env_prefix": "CROSS_SESSION_SEND_GUARD_",
        "config_path": "~/.claude/cross-session-send-guard.json",
        "config_max_bytes": 1_000_000,
    },
]

SETTINGS_FILES = ["~/.claude/settings.json", "~/.claude/settings.local.json",
                  ".claude/settings.json", ".claude/settings.local.json"]


def _import(path, name):
    hooks_dir = os.path.dirname(path)
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _read_file_cfg(spec):
    """The raw config-file dict, resolved the same way the loader does
    (including the loader's size cap, where it has one)."""
    path = os.environ.get(spec["env_prefix"] + "CONFIG",
                          os.path.expanduser(spec["config_path"]))
    cap = spec["config_max_bytes"]
    try:
        if cap is not None and os.path.getsize(path) > cap:
            return path, {}, f"larger than {cap} bytes — loader ignores it"
        with open(path) as fh:
            loaded = json.load(fh)
        return path, loaded if isinstance(loaded, dict) else {}, "ok"
    except FileNotFoundError:
        return path, {}, "absent"
    except Exception as exc:
        return path, {}, f"unreadable ({exc.__class__.__name__})"


_UNKNOWN = object()


def _coerce_like_plugin(mod, default, raw, choices):
    """Run the plugin's own coercion (None = the loader rejects the
    value). _UNKNOWN when the module has no _coerce — managed.py clamps
    inline instead — or coercion itself blows up."""
    coerce = getattr(mod, "_coerce", None)
    if coerce is None:
        return _UNKNOWN
    try:
        try:
            return coerce(default, raw, choices)
        except TypeError:
            return coerce(default, raw)
    except Exception:
        return _UNKNOWN


def _lands_unchanged(eff, raw):
    """Loose raw-vs-effective comparison for knobs without a plugin
    _coerce. None = can't tell; never false-flag those."""
    try:
        if isinstance(eff, (int, float)) and not isinstance(eff, bool):
            return float(raw) == float(eff)
        if isinstance(eff, list):
            return (json.loads(raw) if isinstance(raw, str) else raw) == eff
    except Exception:
        return None
    return None


def _knob_rows(mod, defaults, effective, file_cfg, env_prefix):
    choices_map = getattr(mod, "_CHOICES", {})
    rows = []
    for k, dflt in defaults.items():
        choices = choices_map.get(k)
        env_raw = os.environ.get(env_prefix + k.upper())
        file_raw = file_cfg.get(k, _UNKNOWN)
        env_val = (_coerce_like_plugin(mod, dflt, env_raw, choices)
                   if env_raw is not None else _UNKNOWN)
        file_val = (_coerce_like_plugin(mod, dflt, file_raw, choices)
                    if file_raw is not _UNKNOWN else _UNKNOWN)
        eff = effective.get(k, dflt)
        notes = []
        # Source = the layer whose value the loader actually kept: env
        # beats file only when the loader accepts it (rejected values
        # are ignored in place). managed.py knobs have no reject path
        # (env always wins by presence), which _UNKNOWN handles.
        if env_raw is not None and env_val is not None:
            source, coerced = "env", env_val
        elif file_raw is not _UNKNOWN and file_val is not None:
            source, coerced = "file", file_val
        else:
            source, coerced = "default", _UNKNOWN
        if env_raw is not None and env_val is None:
            notes.append(f"env {env_raw!r} rejected")
        if file_raw is not _UNKNOWN and file_val is None:
            notes.append(f"file {file_raw!r} rejected")
        if source != "default":
            raw = env_raw if source == "env" else file_raw
            unchanged = (coerced == eff if coerced is not _UNKNOWN
                         else _lands_unchanged(eff, raw))
            if unchanged is False:
                notes.append(f"raw {raw!r} adjusted")
        elif dflt is None:
            notes.append("computed default")
        elif not _same_as_default(dflt, eff):
            # e.g. soft_pct pulled down to hard_pct, block_tokens pulled
            # up to warn_tokens: another knob's clamp moved this one.
            notes.append(f"default {dflt!r} moved by another knob's clamp")
        rows.append({"knob": k, "value": eff, "source": source,
                     "note": "; ".join(notes)})
    return rows


def _same_as_default(dflt, eff):
    if dflt == eff:
        return True
    # Path defaults get expanded/absolutized by the loaders.
    if isinstance(dflt, str) and isinstance(eff, str):
        return os.path.abspath(os.path.expanduser(dflt)) == eff
    return False


def _models_row(effective, file_cfg, env_prefix):
    # Empty-string env falls through to the file in the loaders, so
    # only a non-empty value counts as env; an unparseable env value
    # loses to the file (loaders differ in how, so just flag it).
    env_raw = os.environ.get(env_prefix + "MODELS")
    note = ""
    env_ok = False
    if env_raw:
        try:
            env_ok = isinstance(json.loads(env_raw), dict)
        except ValueError:
            pass
        if not env_ok:
            note = f"env {env_raw!r} rejected"
    if env_raw and env_ok:
        source = "env"
    elif "models" in file_cfg:
        source = "file"
    else:
        source = "default"
    return {"knob": "models", "value": effective.get("models", {}),
            "source": source, "note": note}


def _hook_wiring():
    """Map plugin name -> wiring evidence found in the user's and the
    project's settings files: direct hook commands (script installs)
    and enabledPlugins entries (marketplace installs, whose hooks come
    from the plugin's own hooks.json). Duplicates are marked."""
    seen = {}  # (plugin, event, matcher, script, settings-file) -> count
    enabled = {spec["name"]: [] for spec in PLUGINS}
    markers = {spec["name"]:
               re.compile(r"""(?:^|[\s"'`=:;,()&|/])plugins/"""
                          + re.escape(spec["name"]) + r"/hooks/([\w.-]+)")
               for spec in PLUGINS}
    for settings in SETTINGS_FILES:
        path = os.path.expanduser(settings)
        try:
            with open(path) as fh:
                loaded = json.load(fh)
        except Exception:
            continue
        if not isinstance(loaded, dict):
            continue
        plugins_on = loaded.get("enabledPlugins")
        if isinstance(plugins_on, dict):
            for plugin_id, on in plugins_on.items():
                for spec in PLUGINS:
                    if on and str(plugin_id).startswith(spec["name"] + "@"):
                        enabled[spec["name"]].append(
                            f"enabled as {plugin_id} [{settings}]")
        hooks = loaded.get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, rules in hooks.items():
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                matcher = rule.get("matcher", "")
                entries = rule.get("hooks")
                if not isinstance(entries, list):
                    continue
                for hook in entries:
                    if not isinstance(hook, dict):
                        continue
                    cmd = hook.get("command")
                    if not isinstance(cmd, str):
                        continue
                    for name, marker in markers.items():
                        # One command mentions the script repeatedly
                        # (python3 x || python x); that's one wiring.
                        for script in set(marker.findall(cmd)):
                            key = (name, event, matcher, script, settings)
                            seen[key] = seen.get(key, 0) + 1
    wiring = {spec["name"]: list(enabled[spec["name"]]) for spec in PLUGINS}
    for (name, event, matcher, script, settings), count in sorted(seen.items()):
        entry = f"{event}({matcher}) {script}" if matcher else f"{event} {script}"
        if settings != SETTINGS_FILES[0]:
            entry += f" [{settings}]"
        if count > 1:
            entry += f"  !! registered {count}x"
        wiring[name].append(entry)
    return wiring


def _dir_state(cfg):
    """state_dir existence + ledger size, for plugins that have one."""
    state_dir = cfg.get("state_dir")
    if not state_dir:
        return None
    out = {"state_dir": state_dir, "exists": os.path.isdir(state_dir)}
    ledger = os.path.join(state_dir, "ledger.jsonl")
    if os.path.isfile(ledger):
        out["ledger_bytes"] = os.path.getsize(ledger)
    return out


def _watchers():
    cli = os.path.join(REPO, "plugins/compact-manager/bin/compact-manager")
    try:
        proc = subprocess.run([cli, "status"], capture_output=True,
                              text=True, timeout=10)
        return json.loads(proc.stdout)
    except Exception as exc:
        return f"unavailable ({exc.__class__.__name__})"


def collect():
    report = {"plugins": {}, "hook_wiring": _hook_wiring(),
              "watchers": _watchers()}
    for spec in PLUGINS:
        name = spec["name"]
        entry = {}
        report["plugins"][name] = entry
        module_path = os.path.join(REPO, spec["module"])
        try:
            mod = _import(module_path, spec["module"].replace("/", "_")
                          .replace(".py", "").replace("-", "_"))
        except Exception as exc:
            entry["error"] = f"loader import failed: {exc!r}"
            continue
        cfg_path, file_cfg, file_status = _read_file_cfg(spec)
        entry["config_file"] = {"path": cfg_path, "status": file_status}
        try:
            effective = mod.load_config()
        except Exception as exc:
            entry["error"] = f"load_config failed: {exc!r}"
            continue
        defaults = dict(getattr(mod, spec["defaults_attr"]))
        rows = _knob_rows(mod, defaults, effective, file_cfg,
                          spec["env_prefix"])
        if "models" in file_cfg or hasattr(mod, "_PER_MODEL_KEYS"):
            rows.append(_models_row(effective, file_cfg, spec["env_prefix"]))
        if name == "compact-manager":
            try:
                managed = _import(os.path.join(
                    REPO, "plugins/compact-manager/hooks/managed.py"),
                    "cm_managed_status")
                managed_eff = managed.load_config(base=effective)
                rows += _knob_rows(managed, managed.MANAGED_DEFAULTS,
                                   managed_eff, file_cfg, spec["env_prefix"])
            except Exception as exc:
                entry["managed_error"] = f"managed layer: {exc!r}"
        entry["knobs"] = rows
        state = _dir_state(effective)
        if state:
            entry["state"] = state
    return report


def _fmt_value(v):
    return json.dumps(v) if isinstance(v, (dict, list)) else str(v)


def render(report):
    lines = []
    for name, entry in report["plugins"].items():
        lines.append(f"=== {name} ===")
        if "error" in entry:
            lines.append(f"  ERROR: {entry['error']}")
            lines.append("")
            continue
        cf = entry["config_file"]
        lines.append(f"  config file: {cf['path']} ({cf['status']})")
        for row in entry["knobs"]:
            note = f"  <- {row['note']}" if row["note"] else ""
            src = row["source"]
            marker = src if src == "default" else src.upper()
            lines.append(f"  {row['knob']:<28} {_fmt_value(row['value']):<28}"
                         f" ({marker}){note}")
        if "managed_error" in entry:
            lines.append(f"  {entry['managed_error']}")
        state = entry.get("state")
        if state:
            status = "exists" if state["exists"] else "MISSING"
            extra = ""
            if "ledger_bytes" in state:
                extra = f", ledger {state['ledger_bytes'] / 1e6:.1f} MB"
            lines.append(f"  state: {state['state_dir']} ({status}{extra})")
        wired = report["hook_wiring"].get(name, [])
        if wired:
            lines.append(f"  hooks wired: {'; '.join(wired)}")
        else:
            lines.append("  hooks wired: NONE FOUND (no hook command or "
                         "enabledPlugins entry in user/project settings)")
        lines.append("")
    lines.append("=== compact-manager watchers ===")
    watchers = report["watchers"]
    if isinstance(watchers, str):
        lines.append(f"  {watchers}")
    elif not watchers:
        lines.append("  none running")
    else:
        for w in watchers:
            flag = "" if w.get("state") == "WATCHER_READY" else "  <-- ATTENTION"
            lines.append(f"  session {w.get('session_id')}  pid {w.get('pid')}"
                         f"  {w.get('state')}"
                         f"{'' if w.get('live') else ' (DEAD)'}"
                         f"{' reason: ' + str(w['reason']) if w.get('reason') else ''}"
                         f"{flag}")
    return "\n".join(lines)


def main(argv):
    report = collect()
    if "--json" in argv:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
