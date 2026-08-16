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
    },
    {
        "name": "subagent-context",
        "module": "plugins/subagent-context/hooks/subagent_context.py",
        "defaults_attr": "_DEFAULTS",
        "env_prefix": "SUBAGENT_CONTEXT_",
        "config_path": "~/.claude/subagent-context.json",
    },
    {
        "name": "cross-session-send-guard",
        "module": "plugins/cross-session-send-guard/hooks/config.py",
        "defaults_attr": "DEFAULTS",
        "env_prefix": "CROSS_SESSION_SEND_GUARD_",
        "config_path": "~/.claude/cross-session-send-guard.json",
    },
]

SETTINGS_FILES = ["~/.claude/settings.json", "~/.claude/settings.local.json"]


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
    """The raw config-file dict, resolved the same way the loader does."""
    path = os.environ.get(spec["env_prefix"] + "CONFIG",
                          os.path.expanduser(spec["config_path"]))
    try:
        with open(path) as fh:
            loaded = json.load(fh)
        return path, loaded if isinstance(loaded, dict) else {}, "ok"
    except FileNotFoundError:
        return path, {}, "absent"
    except Exception as exc:
        return path, {}, f"unreadable ({exc.__class__.__name__})"


def _coerce_like_plugin(mod, default, raw, choices):
    """Best effort: run the plugin's own coercion so a raw value that
    lands unchanged doesn't get flagged as adjusted."""
    try:
        try:
            return mod._coerce(default, raw, choices)
        except TypeError:
            return mod._coerce(default, raw)
    except Exception:
        return None


def _knob_rows(mod, defaults, effective, file_cfg, env_prefix):
    choices_map = getattr(mod, "_CHOICES", {})
    rows = []
    for k, dflt in defaults.items():
        env_raw = os.environ.get(env_prefix + k.upper())
        if env_raw is not None:
            source, raw = "env", env_raw
        elif k in file_cfg:
            source, raw = "file", file_cfg[k]
        else:
            source, raw = "default", None
        eff = effective.get(k, dflt)
        note = ""
        if source != "default":
            coerced = _coerce_like_plugin(mod, dflt, raw, choices_map.get(k))
            if coerced != eff:
                note = f"raw {raw!r} adjusted"
        rows.append({"knob": k, "value": eff, "source": source, "note": note})
    return rows


def _models_row(effective, file_cfg, env_prefix):
    if os.environ.get(env_prefix + "MODELS") is not None:
        source = "env"
    elif "models" in file_cfg:
        source = "file"
    else:
        source = "default"
    return {"knob": "models", "value": effective.get("models", {}),
            "source": source, "note": ""}


def _hook_wiring():
    """Map plugin name -> list of 'Event script.py' strings found in the
    user's settings files, with duplicate registrations marked."""
    seen = {}  # (plugin, event, script, settings-file) -> count
    for settings in SETTINGS_FILES:
        path = os.path.expanduser(settings)
        try:
            with open(path) as fh:
                hooks = json.load(fh).get("hooks", {})
        except Exception:
            continue
        if not isinstance(hooks, dict):
            continue
        for event, rules in hooks.items():
            if not isinstance(rules, list):
                continue
            for rule in rules:
                matcher = (rule or {}).get("matcher", "")
                for hook in (rule or {}).get("hooks", []):
                    cmd = (hook or {}).get("command", "")
                    for spec in PLUGINS:
                        marker = f"plugins/{spec['name']}/hooks/"
                        if marker not in cmd:
                            continue
                        script = cmd.split(marker, 1)[1].split()[0]
                        key = (spec["name"], event, matcher, script, settings)
                        seen[key] = seen.get(key, 0) + 1
    wiring = {spec["name"]: [] for spec in PLUGINS}
    for (name, event, matcher, script, settings), count in sorted(seen.items()):
        entry = f"{event}({matcher}) {script}" if matcher else f"{event} {script}"
        if "local" in settings:
            entry += " [settings.local.json]"
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
            lines.append("  hooks wired: NONE FOUND in ~/.claude/settings*.json")
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
