"""uv run python test_ade.py: the non-Orca backends' parsing (remote -> project, orca flags, drafts, T3 state)."""
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import os, tempfile; os.environ.update(GREENLIGHT_HOME=tempfile.mkdtemp(), GREENLIGHT_ME="octo")  # before greenlight: never the real ~/.greenlight or glab
import tempfile
from pathlib import Path
from unittest import mock

import ade_common
import ade_t3

assert ade_common.project_id("git@gitlab.com:acme/widgets.git") == "git:gitlab.com/acme/widgets"
assert ade_common.project_id("https://gitlab.com/acme/ops/relay") == "git:gitlab.com/acme/ops/relay"
assert ade_common.flags(["--terminal", "t1", "--text", "hi", "--enter", "--wait-submit", "10"]) == \
    {"terminal": "t1", "text": "hi", "enter": True, "wait-submit": "10"}

assert ade_common.draft(["───", "❯ ", "───"]) == ""
assert ade_common.draft(["───", "❯ half typed", "───"]) == "half typed"  # never typed over
assert ade_common.draft(["Which one?", "❯ 1. Keep it", "  2. Port it"]) == ""  # a menu, not a draft

# herdr reads ANSI: Claude's dim prompt suggestion isn't a draft, typed text is
ghost = "\x1b[1m❯\x1b[0m\xa0\x1b[0m\x1b[2mlist both words\x1b[0m\r"
assert ade_common.plain(ghost) == "❯\xa0list both words"
assert ade_common.draft([ade_common.plain(ghost, ghost=False)]) == ""
assert ade_common.draft([ade_common.plain("\x1b[1m❯\x1b[0m half \x1b[32mtyped\x1b[0m", ghost=False)]) == "half typed"

with tempfile.TemporaryDirectory() as d, mock.patch.object(ade_common, "GOALS", Path(d) / "goals.json"):
    ade_common.set_goal("/w/a", "Merge !1")
    ade_common.set_goal("/w/b", "Review !2")
    ade_common.set_goal("/w/a", "")  # clearing removes it
    assert ade_common.goals() == {"/w/b": "Review !2"}

assert ade_t3.state({"status": "running"}) == "working"
assert ade_t3.state({"status": "running", "pendingRequestCount": 1}) == "waiting"
assert ade_t3.state({"status": "completed"}) == "done"
assert ade_t3.ms("2026-10-27T20:55:40.176Z") == 1793134540176
# shims answer `repo list` in Orca's shape, so dash.py's New task can list their repos
import dash
W = [{"repoId": "/r/widgets", "projectId": "git:gitlab.com/acme/widgets"}, {"repoId": "/r/widgets", "projectId": "git:gitlab.com/acme/widgets"},
     {"repoId": "/r/local", "projectId": None}, {"repoId": ""}]
with mock.patch.object(dash.greenlight, "orca", return_value=ade_common.repo_list(W)):
    assert dash.repos() == [{"id": "/r/widgets", "name": "widgets", "host": "gitlab", "path": "acme/widgets"}], dash.repos()
print("ok")
