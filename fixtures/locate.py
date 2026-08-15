"""Locator for the shared fixture corpus.

Import via a path computed by walking UP from the importing test file to
the repo root (the directory containing this `fixtures/` dir and
`.claude-plugin/marketplace.json`), then add it to sys.path — do NOT
hardcode `parents[N]` in plugin tests; the depth differs between the
root plugin and `plugins/<name>/`.

Usage from any test in this repo:

    from pathlib import Path
    import sys
    root = next(p for p in Path(__file__).resolve().parents
                if (p / "fixtures" / "locate.py").is_file())
    sys.path.insert(0, str(root / "fixtures"))
    import locate
    locate.FIXTURES_DIR  # -> Path to fixtures/
"""
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
TRANSCRIPTS = FIXTURES_DIR / "transcripts"
SESSIONS = FIXTURES_DIR / "sessions"
