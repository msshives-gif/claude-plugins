"""Resolution tests: address parsing, liveness, collision, self-skip.

/proc is faked via a temp dir (proc_root param) so liveness — including
the pid-recycle case — is testable without real processes.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "hooks"))
import peers  # noqa: E402


def make_proc(root, pid, starttime):
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    # Minimal /proc/<pid>/stat shape: pid (comm with spaces) state + 19
    # more fields so field 22 (starttime) exists.
    tail = ["S"] + ["0"] * 18 + [str(starttime), "0", "0"]
    with open(os.path.join(d, "stat"), "w") as fh:
        fh.write(f"{pid} (some (weird) name) " + " ".join(tail))


class ParseAddressTests(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(peers.parse_address("projects-f7"),
                         ("name", "projects-f7"))

    def test_name_with_ref_suffix(self):
        self.assertEqual(peers.parse_address("worker-a [fc9877]"),
                         ("name", "worker-a"))

    def test_uds_socket(self):
        self.assertEqual(
            peers.parse_address("uds:/run/user/1000/cc-socks/2144162.sock"),
            ("pid", 2144162))

    def test_uds_garbage(self):
        self.assertIsNone(peers.parse_address("uds:/tmp/not-a-sock"))


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = os.path.join(self.tmp.name, "sessions")
        self.projects = os.path.join(self.tmp.name, "projects")
        self.proc = os.path.join(self.tmp.name, "proc")
        os.makedirs(self.sessions)
        self.cfg = {"sessions_dir": self.sessions,
                    "projects_dir": self.projects}
        self.payload = {"session_id": "my-own-session"}

    def add_session(self, pid, name, session_id, cwd="/home/u/proj",
                    proc_start=777, updated=1000, registry_start=None,
                    transcript=True):
        entry = {"pid": pid, "name": name, "sessionId": session_id,
                 "cwd": cwd, "updatedAt": updated,
                 "procStart": (proc_start if registry_start is None
                               else registry_start)}
        with open(os.path.join(self.sessions, f"{pid}.json"), "w") as fh:
            json.dump(entry, fh)
        if proc_start is not None:
            make_proc(self.proc, pid, proc_start)
        if transcript:
            d = os.path.join(self.projects, cwd.replace("/", "-"))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{session_id}.jsonl"), "w") as fh:
                fh.write("{}\n")

    def resolve(self, to):
        return peers.resolve_peer(to, self.payload, self.cfg,
                                  proc_root=self.proc)

    def test_resolves_live_name(self):
        self.add_session(101, "peer-a", "sess-a")
        got = self.resolve("peer-a")
        self.assertEqual(got["session_id"], "sess-a")
        self.assertTrue(got["transcript"].endswith("sess-a.jsonl"))

    def test_main_and_empty_are_none(self):
        self.assertIsNone(self.resolve("main"))
        self.assertIsNone(self.resolve(""))

    def test_unknown_name_is_none(self):
        self.add_session(101, "peer-a", "sess-a")
        self.assertIsNone(self.resolve("stranger"))

    def test_dead_pid_is_none(self):
        self.add_session(101, "peer-a", "sess-a", proc_start=None)
        self.assertIsNone(self.resolve("peer-a"))

    def test_recycled_pid_is_none(self):
        # /proc pid exists but with a different kernel starttime than
        # the registry recorded: a recycled pid, not our session.
        self.add_session(101, "peer-a", "sess-a", proc_start=777,
                         registry_start=555)
        self.assertIsNone(self.resolve("peer-a"))

    def test_collision_resolves_to_newest(self):
        self.add_session(101, "peer-a", "sess-old", updated=1000)
        self.add_session(102, "peer-a", "sess-new", updated=2000)
        self.assertEqual(self.resolve("peer-a")["session_id"], "sess-new")

    def test_own_session_is_none(self):
        self.add_session(101, "peer-a", "my-own-session")
        self.assertIsNone(self.resolve("peer-a"))

    def test_missing_transcript_is_none(self):
        self.add_session(101, "peer-a", "sess-a", transcript=False)
        self.assertIsNone(self.resolve("peer-a"))

    def test_uds_form_resolves_by_pid(self):
        self.add_session(101, "peer-a", "sess-a")
        got = self.resolve("uds:/run/user/1000/cc-socks/101.sock")
        self.assertEqual(got["session_id"], "sess-a")

    def test_ref_suffix_stripped(self):
        self.add_session(101, "peer-a", "sess-a")
        self.assertEqual(self.resolve("peer-a [fc9877]")["session_id"],
                         "sess-a")

    def test_missing_sessions_dir_is_none(self):
        cfg = {"sessions_dir": os.path.join(self.tmp.name, "nope"),
               "projects_dir": self.projects}
        self.assertIsNone(peers.resolve_peer("x", self.payload, cfg,
                                             proc_root=self.proc))


if __name__ == "__main__":
    unittest.main()
