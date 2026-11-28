"""Agents stopped on an interactive prompt (ade_common.PROMPTS): read as waiting on the user everywhere, never done.

The screens are captured from real tmux panes (codex, agy) and Claude's permission dialog, with paths and names generic."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("GREENLIGHT_HOME", tempfile.mkdtemp(prefix="gl-prompt-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ade_basement  # noqa: E402
import ade_common  # noqa: E402
import cleanup  # noqa: E402
import dash  # noqa: E402
import greenlight  # noqa: E402
import lifestage  # noqa: E402

CODEX_RUN = """\
› Goal: add a "Start over" button to the regular chat …
    + 9 lines (^t to expand)
• The UI change is in place. I'm rerunning the affected checks with the required access.
• Running make lint
  Would you like to run the following command?
  Environment: local
  Reason: May I run the required lint gate with access to uv's cache outside this
  workspace?
  $ make lint
› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `make lint` (p)
  3. No, and tell Codex what to do differently (esc)
  Press enter to confirm or esc to cancel
""".splitlines()

CODEX_UPDATE = """\
  ✨ Update available! 0.98.0 -> 0.99.1
  Release notes: see the changelog
› 1. Update now (runs `npm install -g @openai/codex`)
  2. Skip
  3. Skip until next version
  Press enter to continue
""".splitlines()

AGY_RUN = """\
● Ran 3 commands (git fetch: failed · git rev-parse · git status) (ctrl+o to expand)
Command
──────────────────────────────────────────────────────────────────────
Requesting permission for:
   git status --short --branch && git rev-parse HEAD
Run this command?
> 1. Yes, run command
  2. Yes, and always allow in this conversation for commands that start with 'git status'
  3. Yes, and always allow for commands that start with 'git status' (Persist to settings.json)
  4. No, cancel
  ↑/↓ Navigate · tab Amend · ctrl+g edit/expand command
[Gemini 4 ⚡high] ░░░░░░░░░░ 2% | 📁 widgets | ✏️  +0 -0 | 🚀 0.0 tok/s
""".splitlines()

AGY_TRUST = """\
Do you trust the contents of this project?
Antigravity can read, edit and run files in this folder.
> 1. Yes, I trust this project
  2. No, exit
  ↑/↓ Navigate · enter Select
""".splitlines()

CLAUDE_BASH = """\
╭──────────────────────────────────────────────────────────────╮
│ Bash command                                                 │
│                                                              │
│   make test-py                                               │
│   Run the Python tests                                       │
│                                                              │
│ Do you want to proceed?                                      │
│ ❯ 1. Yes                                                     │
│   2. Yes, and don't ask again for make test-py commands      │
│   3. No, and tell Claude what to do differently (esc)        │
╰──────────────────────────────────────────────────────────────╯
""".splitlines()

CLAUDE_SCRIPT = """\
──────────────────────────────────────────────────────────────────────
 Bash command
 Run shell command
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 │ cd ~/work/widgets.fix;
 │ S=$TMPDIR/try; rm -rf $S; mkdir -p $S/gl $S/repo
""" + " │ echo step\n" * 12 + """\
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 This shell -c script runs rm and could not be checked
 ⚠ Claude Code will automatically block this request in 1:09, to avoid holding up an unattended session
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel · Tab to amend
"""
CLAUDE_SCRIPT = CLAUDE_SCRIPT.splitlines()

# after the user answered: the agent ran on and came back to its composer
CODEX_ANSWERED = CODEX_RUN + """\
• Ran make lint
  └ ok
• Lint passes.
› Ask Codex to do anything
  ? for shortcuts
""".splitlines()


def test_known_prompts_are_read_with_what_they_ask():
    cases = [
        (CODEX_RUN, "codex", "command", "Would you like to run the following command?", "make lint"),
        (CODEX_UPDATE, "codex", "update", "✨ Update available! 0.98.0 -> 0.99.1", ""),
        (AGY_RUN, "agy", "command", "Run this command?", "git status --short --branch && git rev-parse HEAD"),
        (AGY_TRUST, "agy", "trust", "Do you trust the contents of this project?", ""),
        (CLAUDE_BASH, "claude", "permission", "Do you want to proceed?", "make test-py"),
        (CLAUDE_SCRIPT, "claude", "permission", "Do you want to proceed?", "cd ~/work/widgets.fix;"),
    ]
    for lines, agent, kind, text, cmd in cases:
        got = ade_common.prompt_ask(lines)
        assert got == {"agent": agent, "kind": kind, "text": text, "cmd": cmd}, (text, got)
        seen = ade_common.screen_state(lines)
        assert seen["state"] == "waiting_for_user" and seen["prob"] >= greenlight.SURE and seen["ask"] == got, seen


def test_answered_or_unrelated_screens_are_not_prompts():
    assert ade_common.prompt_ask(CODEX_ANSWERED) is None
    assert ade_common.screen_state(CODEX_ANSWERED)["state"] == "finished"
    assert ade_common.prompt_ask(CODEX_RUN + ["• Working (3s • esc to interrupt)"]) is None  # answered: running it now
    assert ade_common.prompt_ask(CLAUDE_BASH[:-1] + ["❯ fix the tests please"]) is None  # a draft under the menu
    assert ade_common.prompt_ask(["Pick one", "1. alpha", "2. beta"]) is None  # a numbered list, no known prompt
    assert ade_common.prompt_ask([]) is None and ade_common.prompt_ask(["› "]) is None
    # far below the question: another menu's rows, not this prompt's
    assert ade_common.prompt_ask(["Would you like to run the following command?"] + ["output"] * 20 + ["1. a", "2. b"]) is None


def test_review_findings():
    # a narrow pane: codex wraps its question and footer
    narrow = ["Would you like to run the", "following command?", "$ make lint", "› 1. Yes, proceed", "2. Yes, and don't ask",
              "again for commands", "3. No, and tell Codex", "Press enter to confirm or", "esc to cancel"]
    assert ade_common.prompt_ask(narrow) == {"agent": "codex", "kind": "command", "text": "Would you like to run the following command?",
                                             "cmd": "make lint"}
    # answered, then output that is a numbered list: not the old prompt
    assert ade_common.prompt_ask(CODEX_RUN + ["• Ran make lint", "  └ 1. lint", "    2. typecheck"]) is None
    # a prompt with no command never borrows one from older output
    edit = CODEX_RUN + ["• Ran make lint", "  └ ok", "Would you like to make the following edits?", "  foo.py +2 -1",
                        "› 1. Yes", "  2. No", "  Press enter to confirm or esc to cancel"]
    assert ade_common.prompt_ask(edit) == {"agent": "codex", "kind": "edit", "text": "Would you like to make the following edits?", "cmd": ""}
    claude_edit = ["⏺ I ran:", "  $ npm test", "Edit file", " foo.py", "Do you want to make this edit to foo.py?", "❯ 1. Yes", "  2. No"]
    assert ade_common.prompt_ask(claude_edit)["cmd"] == ""
    # opencode's bare ┃ composer under a numbered list ending in a question: an idle reply, not a prompt
    assert ade_common.prompt_ask(["Do you want to proceed with the refactor?", "1. Yes", "2. No", "┃"]) is None


def test_basement_state_never_done_on_a_prompt():
    for title, cmd, lines in (("[ ! ] Action Required | Add a button", "codex", CODEX_RUN), ("widgets", "agy", AGY_RUN),
                              ("widgets", "agy", AGY_TRUST), ("✳ Tests", "claude", CLAUDE_BASH), ("x", "codex", CODEX_UPDATE)):
        assert ade_basement.state(title, lines, cmd) == "waiting", (title, cmd)
    assert ade_basement.state("x", CODEX_ANSWERED, "codex") == "done"


def test_basement_listing_carries_the_prompt():
    pane = {"handle": "%7", "session": "mc-a", "cwd": "/w/a", "title": "[ ! ] Action Required", "at": "0", "cmd": "codex"}
    with mock.patch.object(ade_basement, "screen", return_value=CODEX_RUN):
        kind, s, ask = ade_basement._pane_agent(pane)
    assert (kind, s, ask["cmd"]) == ("codex", "waiting", "make lint")
    with mock.patch.object(ade_basement, "screen", return_value=CODEX_ANSWERED):
        assert ade_basement._pane_agent(pane) == ("codex", "done", None)


def _ws(state, owner="agent"):
    return {"id": "w", "agent": {"state": state, "type": "codex", "pane_key": "basement:%7", "last": ""},
            "next": {"step": "reviewed", "owner": owner, "action": "run the reviews"}}


def _overlay(ws, lines):
    terms = {"w": [{"tabId": "basement", "leafId": "%7", "handle": "%7", "agentIdentity": "codex", "lastOutputAt": 1}]}
    with mock.patch.object(greenlight, "orca", return_value={"terminal": {"tail": lines}}):
        greenlight.agent_overlay(ws, terms)
    return ws


def test_scan_overlay_says_waiting_with_the_command():
    for state, owner in (("waiting", "agent"), ("done", "agent"), ("working", "greenlight")):
        ws = _overlay(_ws(state, owner), CODEX_RUN)
        assert ws["agent"]["state"] == "waiting", (state, owner, ws)  # never dispatched over, never "done"
        assert ws["next"]["owner"] == "me" and "make lint" in ws["next"]["action"], ws["next"]
        assert ws["agent"]["asking"]["kind"] == "command"
    ws = _overlay(_ws("waiting"), CODEX_ANSWERED)
    assert "asking" not in ws["agent"] and "make lint" not in ws["next"]["action"], ws  # answered: the old prompt isn't claimed


def test_dashboard_and_lifestage_name_the_prompt():
    ask = ade_common.prompt_ask(AGY_TRUST)
    assert greenlight.prompt_text({"asking": ask}) == "Do you trust the contents of this project?"
    assert greenlight.prompt_text({"asking": {"text": "Run this command?", "cmd": "make lint"}}) == "Run this command? `make lint`"
    assert greenlight.prompt_text({}) == "" and greenlight.waiting_action({}) == "agent is waiting on you"
    row = {"agent": {"state": "waiting", "asking": ask}, "next": {"step": "reviewed", "owner": "me", "action": "x"}}
    assert dash.state_of(row) == ("needs", "agent is waiting on your answer: Do you trust the contents of this project?")
    life = lifestage.now(lifestage.place({**row, "goal": "g", "mr": None}), "waiting", asking=ask)
    assert life["pile"] == "yours" and "Do you trust the contents" in life["do"], life
    agent = {"state": "waiting", "asking": ask}
    dash.live_asking(agent, {"state": "working"})  # answered since the scan: the stale prompt goes
    assert "asking" not in agent
    dash.live_asking(agent, {"state": "waiting", "asking": ask})
    assert agent["asking"] == ask


def test_cleanup_keeps_a_workspace_whose_agent_waits_on_a_prompt():
    rp = cleanup.Repo.__new__(cleanup.Repo)
    rp.path, rp.error, rp.host, rp.project, rp.base, rp.default = "/r", None, "gitlab.com", "acme/widgets", "origin/main", "main"
    rp.mrs = lambda *a: [{"label": "!5", "iid": 5, "state": "merged", "sha": "t"}]
    wt = {"id": "/w/a", "path": "/w/a", "repoId": "r", "branch": "refs/heads/fix",
          "agents": [{"state": "waiting", "agentType": "codex", "asking": ade_common.prompt_ask(CODEX_RUN)}]}
    git = lambda path, *a, check=True: "t" if a[0] in ("rev-parse", "config") else ""
    with (mock.patch.object(cleanup, "git", side_effect=git), mock.patch.object(cleanup.os.path, "isdir", return_value=True),
          mock.patch.object(cleanup, "post_merge_left", return_value=None), mock.patch.object(cleanup, "outside_agent", return_value=None)):
        verdict, reason = cleanup.evaluate(wt, rp, set())
        assert verdict == "skip" and "prompt" in reason, reason
        assert cleanup.evaluate({**wt, "agents": [{"state": "done", "agentType": "codex"}]}, rp, set())[0] == "candidate"
        # a bare "waiting" (Orca's stale alert, a question at the end of a reply) is no proof of a prompt
        assert cleanup.evaluate({**wt, "agents": [{"state": "waiting", "agentType": "codex"}]}, rp, set())[0] == "candidate"
    # twin entries for one path: the prompted agent on either one counts
    twins = cleanup.dedupe([{**wt, "id": "a", "agents": []}, {**wt, "id": "b"}])
    assert len(twins) == 1 and twins[0]["agents"][0]["asking"]["cmd"] == "make lint"
