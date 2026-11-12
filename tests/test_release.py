#!/usr/bin/env python3
"""Nothing private ships: strings you list in $GREENLIGHT_HOME/leak.re (one regex per line, default ~/.greenlight)
must not appear in the code. The list lives outside the repo so the names themselves don't ship. No file: skipped.
os.walk, not git grep: the deployed copy is rsynced without .git. python3 tests/test_release.py"""
import os
import re
from pathlib import Path

f = Path(os.environ.get("GREENLIGHT_HOME", "~/.greenlight")).expanduser() / "leak.re"
if not f.exists():
    print(f"test_release skipped: no {f}")
    raise SystemExit(0)
LEAK = re.compile("|".join(x for x in f.read_text().splitlines() if x.strip()), re.I)
SKIP = {".git", "memory", "node_modules", "__pycache__", "vendor"}  # memory/: gitignored, owner-only
root = Path(__file__).resolve().parents[1]
hits = []
for d, dirs, files in os.walk(root):
    dirs[:] = [x for x in dirs if x not in SKIP]
    for name in files:
        p = Path(d, name)
        if name.endswith((".png", ".jpg", ".icns")):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{p.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1}: {m[0]}" for m in LEAK.finditer(text)]
assert not hits, "private strings:\n" + "\n".join(hits)
print("test_release ok")
