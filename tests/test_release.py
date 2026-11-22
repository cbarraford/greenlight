"""Nothing private ships: strings listed in $GREENLIGHT_HOME/leak.re must not appear in the repo.

Uses os.walk rather than git grep because deployed copies may be rsynced without .git.
"""
import os
import re
from pathlib import Path

from conftest import ORIG_GREENLIGHT_HOME, ROOT

IGNORED_DIRS = {".git", ".claude", "memory", "node_modules", "__pycache__", "vendor"}
BINARY_EXTS = (".png", ".jpg", ".icns")


def find_leaks(root: Path, patterns: list[str]) -> list[str]:
    if not patterns:
        return []
    leak_re = re.compile("|".join(f"(?:{x})" for x in patterns), re.I)
    hits: list[str] = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in IGNORED_DIRS]
        for name in files:
            if name.endswith(BINARY_EXTS):
                continue
            p = Path(d, name)
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits.extend(
                f"{p.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1}: {m[0]}"
                for m in leak_re.finditer(text)
            )
    return hits


def test_no_private_strings_in_repo():
    base = Path(ORIG_GREENLIGHT_HOME).expanduser() if ORIG_GREENLIGHT_HOME else (Path.home() / ".greenlight")
    leak_file = base / "leak.re"
    if not leak_file.exists():
        return
    patterns = [x.strip() for x in leak_file.read_text().splitlines() if x.strip()]
    if not patterns:
        return
    hits = find_leaks(ROOT, patterns)
    assert not hits, "private strings:\n" + "\n".join(hits)


def test_find_leaks_scans_tree_and_ignores_excluded_dirs(tmp_path):
    (tmp_path / "app.py").write_text("line1\nsecret_zebracorn_token\n")
    (tmp_path / "img.png").write_text("secret_zebracorn_token")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "note.md").write_text("secret_zebracorn_token")

    hits = find_leaks(tmp_path, ["zebracorn"])
    assert hits == ["app.py:2: zebracorn"]
    assert find_leaks(tmp_path, []) == []
