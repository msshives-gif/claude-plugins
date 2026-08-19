#!/usr/bin/env python3
"""PreToolUse hook: wrap background Bash tasks in the sigwrap forensics
wrapper so that whatever signals (kills) them gets logged with the
sender's identity before the task dies.

Emits updatedInput JSON only when there is something to wrap; on any
doubt or error it prints nothing and exits 0 (fail open — the tool call
proceeds unmodified).
"""
import json
import os
import shlex
import sys


def main():
    try:
        if os.environ.get("TASK_FORENSICS_DISABLE"):
            return
        data = json.load(sys.stdin)
        if data.get("tool_name") != "Bash":
            return
        tool_input = data.get("tool_input") or {}
        if not tool_input.get("run_in_background"):
            return
        command = tool_input.get("command") or ""
        if not command or "sigwrap.py" in command:
            return
        here = os.path.dirname(os.path.abspath(__file__))
        sigwrap = os.path.normpath(
            os.path.join(here, "..", "bin", "sigwrap.py"))
        if not os.path.isfile(sigwrap):
            return
        python = sys.executable or "python3"
        session = data.get("session_id") or ""
        wrapped = "%s %s --session %s -- %s" % (
            shlex.quote(python), shlex.quote(sigwrap),
            shlex.quote(session), shlex.quote(command))
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": dict(tool_input, command=wrapped)}}))
    except Exception:
        pass  # fail open: no output, no change


main()
