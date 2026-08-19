#!/usr/bin/env python3
"""Signal-forensics wrapper for Claude Code background tasks.

Runs the given shell command as a child and, while it runs, waits for
TERM/INT/HUP/QUIT. Each signal is logged with the sender's pid, uid,
cmdline, and parent chain, then forwarded to the child so behavior is
unchanged. On child exit the wrapper logs the exit status (including
death by an untrappable SIGKILL, which is detected but cannot be
attributed) and exits with the same status.

Usage: sigwrap.py [--session SID] -- <command string>

Fails open on every path: if anything about the wrapper itself breaks,
the command is exec'd unwrapped.
"""
import json
import os
import signal
import subprocess
import sys
import time

SIGS = ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT")
LOG_MAX_BYTES = 10 * 1024 * 1024
CMD_SNIPPET_LEN = 500


def log_path():
    base = os.environ.get("TASK_FORENSICS_LOG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "task-forensics")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "log.jsonl")


def append(record):
    try:
        path = log_path()
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass  # logging must never break the task


def snapshot(pid):
    """Best-effort /proc description of a signal sender."""
    out = {"pid": pid}
    for name in ("cmdline", "comm"):
        try:
            with open("/proc/%d/%s" % (pid, name), "rb") as fh:
                out[name] = fh.read(4096).replace(b"\0", b" ").decode(
                    "utf-8", "replace").strip()
        except OSError as exc:
            out[name] = "unreadable: %r" % (exc,)
    try:
        out["ppid_chain"] = []
        cur = pid
        for _ in range(10):
            with open("/proc/%d/stat" % cur) as fh:
                parts = fh.read().rsplit(")", 1)[1].split()
            cur = int(parts[1])
            if cur <= 1:
                break
            with open("/proc/%d/cmdline" % cur, "rb") as fh:
                cl = fh.read(300).replace(b"\0", b" ").decode(
                    "utf-8", "replace").strip()
            out["ppid_chain"].append({"pid": cur, "cmdline": cl})
    except Exception as exc:
        out["ppid_chain_error"] = repr(exc)
    return out


def parse_args(argv):
    session = None
    i = 1
    while i < len(argv) and argv[i] != "--":
        if argv[i] == "--session" and i + 1 < len(argv):
            session = argv[i + 1]
            i += 2
        else:
            i += 1
    if i >= len(argv) - 1:
        raise ValueError("no command after --")
    return session, " ".join(argv[i + 1:])


def run_unwrapped(command):
    os.execvp("bash", ["bash", "-c", command])


def child_preexec(sigset):
    # The child must see default signal handling; also ask the kernel to
    # TERM it if the wrapper is SIGKILLed out from under it (Linux).
    signal.pthread_sigmask(signal.SIG_UNBLOCK, sigset)
    try:
        import ctypes
        PR_SET_PDEATHSIG = 1
        ctypes.CDLL(None).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


def main():
    try:
        session, command = parse_args(sys.argv)
    except Exception:
        # Cannot even tell what the command is; nothing sane to run.
        sys.stderr.write("sigwrap: bad arguments\n")
        return 2

    try:
        if not hasattr(signal, "sigtimedwait"):
            raise OSError("no sigtimedwait on this platform")
        sigset = {getattr(signal, n) for n in SIGS}
        signal.pthread_sigmask(signal.SIG_BLOCK, sigset)
        child = subprocess.Popen(
            ["bash", "-c", command],
            preexec_fn=lambda: child_preexec(sigset))
    except Exception:
        run_unwrapped(command)  # never returns
        return 1  # unreachable; keeps type-checkers calm

    base = {"session": session, "wrapper_pid": os.getpid(),
            "child_pid": child.pid, "pgid": os.getpgid(0),
            "cmd": command[:CMD_SNIPPET_LEN]}
    append(dict(base, event="armed", ts=time.time()))

    rc = None
    while rc is None:
        try:
            info = signal.sigtimedwait(sigset, 1.0)
        except InterruptedError:
            info = None
        except Exception:
            info = None
        if info is not None:
            append(dict(
                base, event="signal", ts=time.time(),
                signo=info.si_signo, si_code=info.si_code,
                si_pid=info.si_pid, si_uid=info.si_uid,
                sender=snapshot(info.si_pid) if info.si_pid else None))
            try:
                os.kill(child.pid, info.si_signo)
            except OSError:
                pass
        rc = child.poll()

    append(dict(base, event="exit", ts=time.time(), returncode=rc,
                killed_by_signal=-rc if rc < 0 else None))
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
