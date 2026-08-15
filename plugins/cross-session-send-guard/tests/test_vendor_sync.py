"""Drift guard: the vendored _core.py must equal what tools/sync-core.py
would generate from the upstream source (ignoring the provenance
header's sha/date line). Hand-edits to the vendored copy, or an
upstream change without a re-sync, fail here."""
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(PLUGIN))


def _body(path, skip_header_lines=0):
    with open(path) as fh:
        lines = fh.read().splitlines(keepends=True)
    return "".join(lines[skip_header_lines:])


class VendorSyncTests(unittest.TestCase):
    def test_vendored_core_matches_upstream(self):
        upstream = _body(os.path.join(REPO, "plugins", "subagent-context", "hooks", "subagent_context.py"))
        vendored = _body(os.path.join(PLUGIN, "hooks", "_core.py"),
                         skip_header_lines=4)
        self.assertEqual(
            vendored, upstream,
            "vendored _core.py is out of sync — run "
            "python3 tools/sync-core.py from the repo root")

    def test_header_present(self):
        with open(os.path.join(PLUGIN, "hooks", "_core.py")) as fh:
            head = fh.readline()
        self.assertIn("VENDORED — DO NOT EDIT", head)


if __name__ == "__main__":
    unittest.main()
