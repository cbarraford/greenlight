"""Agents stopped on an interactive prompt (ade_common.PROMPTS): read as waiting on the user everywhere, never done.

The screens are captured from real tmux panes (codex, agy) and Claude's permission dialog, with paths and names generic."""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

if not os.environ.get("GREENLIGHT_HOME"):
    _tmp_home = tempfile.mkdtemp(prefix="gl-prompt-tests-")
    os.environ["GREENLIGHT_HOME"] = _tmp_home
    atexit.register(shutil.rmtree, _tmp_home, True)
os.environ.setdefault("GREENLIGHT_ME", "octo")
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
    codex_edit = ["  Would you like to make the following edits?", "  Reason: fix lint", "• Edited scripts/foo.py (+2 -1)", "    12 -x",
                  "    12 +y", "  └ b.py (+2 -0)", "› 1. Yes, proceed (y)", "  2. No, and tell Codex (esc)", "  Press enter to confirm or esc to cancel"]
    assert ade_common.prompt_ask(codex_edit)["kind"] == "edit"  # the dialog's own diff summary isn't newer output
    assert ade_common.prompt_ask(["Do you want to proceed?", "● 1. Yes", "  2. No"])["kind"] == "permission"
    agy_bare = ["Command", "git status --short", "Run this command?", "> 1. Yes, run command", "  4. No, cancel"]
    assert ade_common.prompt_ask(agy_bare)["cmd"] == "git status --short"
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
    for state, owner in (("waiting", "agent"), ("done", "agent"), ("working", "greenlight"), ("working", "agent")):
        ws = _overlay(_ws(state, owner), CODEX_RUN)
        assert ws["agent"]["state"] == "waiting", (state, owner, ws)  # never dispatched over, never "done"
        assert ws["next"]["owner"] == "me" and "make lint" in ws["next"]["action"], ws["next"]
        assert "Would you like to run the following command?" in ws["next"]["action"], ws["next"]
        assert ws["agent"]["asking"]["kind"] == "command"
    # pre-populated asking (as scan() copies from worktree ps) is cleared once answered or working again
    stale_ws = _ws("waiting")
    stale_ws["agent"]["asking"] = ade_common.prompt_ask(CODEX_RUN)
    ws = _overlay(stale_ws, CODEX_ANSWERED)
    assert "asking" not in ws["agent"] and "make lint" not in ws["next"]["action"], ws  # answered: the old prompt isn't claimed
    working_ws = _ws("waiting")
    working_ws["agent"]["asking"] = ade_common.prompt_ask(CODEX_RUN)
    ws = _overlay(working_ws, CODEX_RUN + ["• Working (3s • esc to interrupt)"])
    assert ws["agent"]["state"] == "working" and "asking" not in ws["agent"], ws
    # owner="greenlight" on logged_out still becomes "done", not "waiting"
    ws = _overlay(_ws("working", "greenlight"), ["API Error: 401 OAuth access token has been revoked"])
    assert ws["agent"]["state"] == "done" and "asking" not in ws["agent"], ws
    # when screen() returns None (no matching terminal), pre-set asking still formats without "in <term>"
    no_term = _ws("waiting")
    no_term["agent"]["asking"] = ade_common.prompt_ask(AGY_TRUST)
    greenlight.agent_overlay(no_term, {})
    assert no_term["next"]["action"] == "agent is waiting on you: Do you trust the contents of this project?", no_term


def test_dashboard_and_lifestage_name_the_prompt():
    ask = ade_common.prompt_ask(AGY_TRUST)
    cmd_ask = ade_common.prompt_ask(CODEX_RUN)
    assert greenlight.prompt_text({"asking": ask}) == "Do you trust the contents of this project?"
    assert greenlight.prompt_text({"asking": {"text": "Run this command?", "cmd": "make lint"}}) == "Run this command? `make lint`"
    assert greenlight.prompt_text({}) == "" and greenlight.waiting_action({}) == "agent is waiting on you"
    assert greenlight.waiting_action({}, " in %7") == "agent is waiting on you in %7"
    row = {"agent": {"state": "waiting", "asking": ask}, "next": {"step": "reviewed", "owner": "me", "action": "x"}}
    assert dash.state_of(row) == ("needs", "agent is waiting on your answer: Do you trust the contents of this project?")
    life = lifestage.now(lifestage.place({**row, "goal": "g", "mr": None}), "waiting", asking=ask)
    assert life["pile"] == "yours" and "Do you trust the contents" in life["do"], life
    life_cmd = lifestage.now(lifestage.place({**row, "goal": "g", "mr": None}), "waiting", asking=cmd_ask)
    assert "`make lint`" in life_cmd["do"], life_cmd
    restored = lifestage.now(life_cmd, "done")
    assert "make lint" not in restored["do"], restored
    # lifestage.of() forwards asking instead of wiping scan's prompt text
    with mock.patch.object(lifestage, "blocks", return_value={}):
        of_life = lifestage.of({"goal": "g", "mr": None, "next": row["next"], "agent": {"state": "waiting", "asking": cmd_ask}})
    assert "`make lint`" in of_life["do"], of_life
    agent = {"state": "waiting", "asking": ask}
    dash.live_asking(agent, {"state": "working"})  # answered since the scan: the stale prompt goes
    assert "asking" not in agent
    dash.live_asking(agent, {"state": "waiting", "asking": ask})
    assert agent["asking"] == ask
    with mock.patch.object(dash.ade, "is_native", return_value=True):
        dash.live_asking(agent, {"state": "waiting"})  # native bare waiting clears stale prompt
        assert "asking" not in agent
    agent["asking"] = ask
    with mock.patch.object(dash.ade, "is_native", return_value=False):
        dash.live_asking(agent, {"state": "waiting"})  # non-native preserves scan prompt
        assert agent["asking"] == ask
    dash.live_asking(agent, {})
    assert "asking" not in agent


def test_cleanup_keeps_a_workspace_whose_agent_waits_on_a_prompt():
    rp = cleanup.Repo.__new__(cleanup.Repo)
    rp.path, rp.error, rp.host, rp.project, rp.base, rp.default = "/r", None, "gitlab.com", "acme/widgets", "origin/main", "main"
    rp.mrs = lambda *a: [{"label": "!5", "iid": 5, "state": "merged", "sha": "t"}]
    wt = {"id": "/w/a", "path": "/w/a", "repoId": "r", "branch": "refs/heads/fix",
          "agents": [{"state": "waiting", "agentType": "codex", "asking": ade_common.prompt_ask(CODEX_RUN)}]}
    git = lambda path, *a, check=True: "t" if a[0] in ("rev-parse", "config") else ""
    with (mock.patch.object(cleanup, "git", side_effect=git), mock.patch.object(cleanup.os.path, "isdir", return_value=True),
          mock.patch.object(cleanup, "post_merge_left", return_value=None), mock.patch.object(cleanup, "outside_agent", return_value=None),
          mock.patch.object(cleanup.ade, "is_native", return_value=True)):
        verdict, reason = cleanup.evaluate(wt, rp, set())
        assert verdict == "skip" and "prompt" in reason, reason
        assert cleanup.evaluate({**wt, "agents": [{"state": "done", "agentType": "codex"}]}, rp, set())[0] == "candidate"
        assert cleanup.evaluate({**wt, "agents": [{"state": "done", "agentType": "codex", "asking": ade_common.prompt_ask(CODEX_RUN)}]}, rp, set())[0] == "candidate"
        # a bare "waiting" (Orca's stale alert, a question at the end of a reply) is no proof of a prompt
        assert cleanup.evaluate({**wt, "agents": [{"state": "waiting", "agentType": "codex"}]}, rp, set())[0] == "candidate"
        # multi-agent list where a later agent holds the prompt
        assert cleanup.evaluate({**wt, "agents": [{"state": "done", "agentType": "claude"}, wt["agents"][0]]}, rp, set())[0] == "skip"
        with mock.patch.object(cleanup.ade, "is_native", return_value=False):  # another ADE's own prompt signal: kept
            v2, r2 = cleanup.evaluate({**wt, "agents": [{"state": "waiting", "agentType": "codex"}]}, rp, set())
            assert v2 == "skip" and "prompt" in r2, (v2, r2)
            assert cleanup.evaluate({**wt, "agents": [{"state": "done", "agentType": "codex"}]}, rp, set())[0] == "candidate"
    # twin entries for one path: the prompted agent on either one counts (including missing/None agents)
    twins = cleanup.dedupe([{**wt, "id": "a", "agents": None}, {**wt, "id": "b"}])
    assert len(twins) == 1 and twins[0]["agents"][0]["asking"]["cmd"] == "make lint"


def test_swarm_review_parser_and_security_regressions():
    # post-option output, answered markers, rate limits, and auth errors reject stale prompts
    for tail in (CODEX_RUN + ["• Ran make lint", "  └ ok"],
                 CODEX_RUN + ["• Running make lint"],
                 CLAUDE_BASH + ["⏺ Bash(make test-py)", "  └ ok"],
                 CLAUDE_BASH + ["✔ You approved", "Running tests..."],
                 CLAUDE_BASH + ["User selected Yes.", "Done."]):
        assert ade_common.prompt_ask(tail) is None, tail[-2:]
    assert ade_common.prompt_ask(CLAUDE_BASH + ["You've hit your usage limit"]) is None
    assert ade_common.screen_state(CLAUDE_BASH + ["You've hit your usage limit"])["state"] == "rate_limited"
    assert ade_common.prompt_ask(CLAUDE_BASH + ["API Error: 401 OAuth access token has been revoked"]) is None
    assert ade_common.screen_state(CLAUDE_BASH + ["API Error: 401 OAuth access token has been revoked"])["state"] == "logged_out"

    # questions AFTER an unrelated numbered list must not match
    assert ade_common.prompt_ask(["Next steps:", "1. Refactor foo", "2. Update tests", "Do you want to proceed?"]) is None
    assert ade_common.prompt_ask(["Changelog:", "1. Fix bug", "2. Add test", "Update available packages"]) is None
    assert ade_common.prompt_ask(["1. alpha", "2. beta", "Requesting permission for:"]) is None

    # rejected earlier command prompt never leaks its command into a later edit prompt
    rejected = CODEX_RUN + ["■ Execution rejected by user.", "Would you like to make the following edits?", "  foo.py (+2 -1)",
                            "› 1. Yes, proceed (y)", "  2. No (esc)", "  Press enter to confirm or esc to cancel"]
    assert ade_common.prompt_ask(rejected) == {"agent": "codex", "kind": "edit", "text": "Would you like to make the following edits?", "cmd": ""}

    # non-bare opencode composer (placeholder or draft) terminates prompt detection
    assert ade_common.prompt_ask(["Do you want to proceed with the refactor?", "1. Yes", "2. No", "┃ Ask anything..."]) is None
    oc_draft = ["Do you want to proceed with the refactor?", "1. Yes", "2. No", "┃ fix the tests", "┃ Build  claude-haiku"]
    assert ade_common.prompt_ask(oc_draft) is None
    assert ade_common.screen_state(oc_draft)["state"] == "finished"
    assert ade_common.screen_state(["Do you want to proceed?", "1. Yes", "2. No", "│ > fix the tests │"])["state"] == "unsent_draft"

    # broad prose false positives and restarted numbered lists after an answered prompt
    assert ade_common.prompt_ask(["• Update available packages:", "  1. ruff 0.9.0", "  2. pytest 8.3.0"]) is None
    assert ade_common.prompt_ask(["Please review the", "following edits?", "1. Fix typo in README", "2. Remove unused import"]) is None
    assert ade_common.prompt_ask(CLAUDE_BASH + ["⏺ I fixed two issues:", "1. Parser bug", "2. Missing test"]) is None
    assert ade_common.prompt_ask(CODEX_RUN + ["• Edited 2 files:", "1. foo.py", "2. bar.py"]) is None

    # _prompt_cmd precision: wrapped option Bash(...), Codex Reason: Command, edit diffs, older Claude Bash command, Bash(...) group, trailing |
    wrapped_opt = ["Bash command", "  git status --short", "Do you want to proceed?", "❯ 1. Yes",
                   "  2. Yes, and don't ask again for", "     Bash(git status:*)", "  3. No"]
    assert ade_common.prompt_ask(wrapped_opt)["cmd"] == "git status --short"
    reason_cmd = ["Would you like to run the following command?", "Reason: run this", "Command", "to verify the fix",
                  "$ make test-py", "› 1. Yes, proceed (y)", "  2. No (esc)"]
    assert ade_common.prompt_ask(reason_cmd)["cmd"] == "make test-py"
    edit_diff = ["Would you like to make the following edits?", "  $ npm run build", "› 1. Yes", "  2. No"]
    assert ade_common.prompt_ask(edit_diff)["cmd"] == ""
    older_claude = ["Bash command", "  make test", "Do you want to proceed?", "❯ 1. Yes", "⏺ Bash(make test)",
                    "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    assert ade_common.prompt_ask(older_claude)["cmd"] == ""
    assert ade_common.prompt_ask(["Do you want to proceed?", "Bash(pytest -q)", "❯ 1. Yes", "  2. No"])["cmd"] == "pytest -q"
    piped = ["Would you like to run the following command?", "  $ git log --oneline |", "    head -n 5", "› 1. Yes", "  2. No"]
    assert ade_common.prompt_ask(piped)["cmd"] == "git log --oneline |"

    # long Codex command, wrapped Agy footer, alternate wraps, Claude trust, Codex approval fallback, Gemini prompts
    long_codex = ["Would you like to run the following command?", "Environment: local", "$ python3 -c 'print(1)'"] + [f"  line_{i}" for i in range(13)] + ["› 1. Yes", "  2. No"]
    assert ade_common.prompt_ask(long_codex) == {"agent": "codex", "kind": "command", "text": "Would you like to run the following command?", "cmd": "python3 -c 'print(1)'"}
    wrapped_agy = AGY_RUN[:-2] + ["↑/↓ Navigate · tab Amend", "ctrl+g edit/expand command", "[Gemini 4] 2% | widgets", "0.0 tok/s"]
    assert ade_common.prompt_ask(wrapped_agy)["cmd"] == "git status --short --branch && git rev-parse HEAD"
    assert ade_common.prompt_ask(wrapped_agy + ["extra1", "extra2"]) is None  # 6 tail rows > PROMPT_TAIL
    alt_wrap = ["Would you like to", "run the following command?", "$ make lint", "› 1. Yes", "  2. No"]
    assert ade_common.prompt_ask(alt_wrap)["text"] == "Would you like to run the following command?"
    edit_wrap = ["Would you like to make the", "following edits?", "foo.py +1", "› 1. Yes", "  2. No"]
    assert ade_common.prompt_ask(edit_wrap)["kind"] == "edit"
    claude_trust = ["Do you trust the files in this folder?", "❯ 1. Yes, proceed", "  2. No, exit"]
    assert ade_common.prompt_ask(claude_trust) == {"agent": "claude", "kind": "trust", "text": "Do you trust the files in this folder?", "cmd": ""}
    footer_only = ["...long command output..."] * 5 + ["› 1. Yes", "  2. No", "  Press enter to confirm or esc to cancel"]
    assert ade_common.prompt_ask(footer_only)["kind"] == "approval"
    gemini_cmd = ["Allow execution of: rm -rf build?", "● 1. Yes, allow once", "  2. No"]
    assert ade_common.prompt_ask(gemini_cmd) == {"agent": "gemini", "kind": "command", "text": "Allow execution of: rm -rf build?", "cmd": "rm -rf build"}
    assert ade_common.prompt_ask(["Apply this change?", "● 1. Yes", "  2. No"])["kind"] == "edit"

    # trailing blanks parity between prompt_ask, screen_state, and agent_state (and no LLM call on known prompt)
    padded = CLAUDE_BASH + [""] * 57
    assert ade_common.screen_state(padded)["state"] == "waiting_for_user"
    with mock.patch("decide.choice", side_effect=AssertionError("known prompt must not call decide.choice")):
        st = ade_common.agent_state(CODEX_RUN + ["", ""], "codex")
    assert st["state"] == "waiting_for_user" and st["ask"]["cmd"] == "make lint"

    # quadratic ReDoS guard on boxed whitespace lines
    t0 = time.monotonic()
    assert ade_common.prompt_ask(["│" + " " * 10000 + "x", "1. Yes"]) is None
    assert time.monotonic() - t0 < 0.5

    # secret redaction, CommonMark backtick fencing, control-char stripping, and malformed shapes
    secret_cmd = ["Would you like to run the following command?",
                  "$ curl -H 'Authorization: Bearer supersecret_token_value_1234567890' https://api.example",
                  "› 1. Yes", "  2. No"]
    got_secret = ade_common.prompt_ask(secret_cmd)
    assert "supersecret_token_value_1234567890" not in got_secret["cmd"] and "…" in got_secret["cmd"]
    injected = {"text": "Run\nthis\x07?", "cmd": "echo `id` [Approve](https://evil.example)"}
    fmt = greenlight.prompt_text({"asking": injected})
    assert "\n" not in fmt and "\x07" not in fmt and "``echo `id` [Approve](https://evil.example)``" in fmt, fmt
    assert greenlight.prompt_text({"asking": "not-a-dict"}) == ""
    assert greenlight.prompt_text({"asking": {"text": 123, "cmd": 456}}) == "123 `456`"
    life_num = lifestage.now(lifestage.place({"goal": "g", "mr": None, "next": {"step": "freeform", "owner": "me", "action": "x"}}),
                             "waiting", asking={"text": 123, "cmd": "echo `x`"})
    assert "123 `` echo `x` ``" in life_num["do"], life_num


def test_basement_worktrees_and_dash_end_to_end(gl_home):
    # ade_basement.worktrees() attaches asking, pane_states() prioritizes waiting, and prompt beats fresh_rec
    pane = {"handle": "%7", "session": "mc-a", "cwd": "/w/a", "title": "widgets", "at": "10", "cmd": "codex"}
    rec = {"/w/a": {"harness": "codex", "session": "mc-a", "used": 1000}}
    with mock.patch.object(ade_basement, "screen", return_value=CODEX_RUN):
        assert ade_basement._pane_agent(pane, "/w/a", rec, now_ms=2000)[:2] == ("codex", "waiting")
    with (mock.patch.object(ade_basement, "panes", return_value=[pane]),
          mock.patch.object(ade_basement, "saved", return_value=["/r"]),
          mock.patch.object(ade_basement, "goals", return_value={}),
          mock.patch.object(ade_basement, "git", return_value="worktree /w/a\n"),
          mock.patch.object(ade_basement, "git_info", return_value={"path": "/w/a", "branch": "refs/heads/f", "isMainWorktree": False, "repoId": "/r", "repo": "r", "projectId": "git:g/r"}),
          mock.patch.object(ade_basement.external, "by_worktree_safe", return_value={}),
          mock.patch.object(ade_basement, "_load_recs", return_value={}),
          mock.patch.object(ade_basement, "screen", return_value=CODEX_RUN),
          mock.patch("importers.first_run")):
        wts = ade_basement.worktrees()
        assert wts[0]["agents"][0]["state"] == "waiting" and wts[0]["agents"][0]["asking"]["cmd"] == "make lint"
        ps = ade_basement.pane_states()
        assert ps["mc-a"]["state"] == "waiting"

    # dash.menu() and dash.ask_user() support agy (> 1.) and unboxed native prompts
    assert [k for k, _ in dash.menu(AGY_TRUST)] == ["1", "2"]
    assert [k for k, _ in dash.menu(AGY_RUN)] == ["1", "2", "3", "4"]
    with mock.patch.object(dash, "waiting_term", return_value=({"path": ""}, {"state": "waiting", "asking": ade_common.prompt_ask(AGY_RUN)}, "%7", {"tail": AGY_RUN})):
        asked = dash.ask_user("ws")
    assert asked["kind"] == "menu" and [o["key"] for o in asked["options"]] == ["1", "2", "3", "4"]
    assert "git status --short --branch" in asked["question"], asked

    # dash.board(), dash.snapshot(), and dash.trees() reflect live prompts and clear exited agents
    ask = ade_common.prompt_ask(CODEX_RUN)
    row = {"id": "/w/a", "name": "a", "path": "/w/a", "project": "o/r", "goal": "g", "mr": None,
           "next": {"step": "reviewed", "owner": "agent", "action": "run the reviews"},
           "agent": {"state": "done", "type": "codex", "last": ""},
           "life": lifestage.place({"goal": "g", "mr": None, "next": {"step": "reviewed", "owner": "agent", "action": "run the reviews"}})}
    (gl_home / "last.json").write_text(json.dumps({"ts": 1, "rows": [row]}))
    live_wt = {"worktreeId": "/w/a", "id": "/w/a", "displayName": "a", "path": "/w/a", "repo": "r", "branch": "refs/heads/f",
               "agents": [{"state": "waiting", "agentType": "codex", "asking": ask}]}
    with (mock.patch.object(dash, "live_agents", return_value={"/w/a": (live_wt, live_wt["agents"][0])}),
          mock.patch.object(dash.blocking, "cached", return_value={}),
          mock.patch.object(dash.placement, "machines", return_value={})):
        brd = dash.board()
        assert "make lint" in brd and "run the reviews" not in brd, brd
        snap = dash.snapshot()
        assert "make lint" in snap["pucks"][0]["why"] and "`make lint`" in snap["pucks"][0]["life"]["do"], snap["pucks"][0]
    # exited agent (agents: []) clears waiting + asking in board() and snapshot()
    row_waiting = {**row, "agent": {"state": "waiting", "type": "codex", "last": "", "asking": ask}}
    (gl_home / "last.json").write_text(json.dumps({"ts": 1, "rows": [row_waiting]}))
    empty_wt = {**live_wt, "agents": []}
    with (mock.patch.object(dash, "live_agents", return_value={"/w/a": (empty_wt, {})}),
          mock.patch.object(dash.blocking, "cached", return_value={}),
          mock.patch.object(dash.placement, "machines", return_value={})):
        snap2 = dash.snapshot()
        assert snap2["pucks"][0]["agent"]["state"] == "none" and "asking" not in snap2["pucks"][0]["agent"]
    # dash.trees() applies live_asking and refreshes full life
    (gl_home / "last.json").write_text(json.dumps({"ts": 1, "rows": [row]}))
    with (mock.patch.object(dash, "ades", return_value=["native"]),
          mock.patch.object(dash, "ade_call", return_value=lambda *a: {"worktrees": [live_wt]}),
          mock.patch.object(dash.blocking, "cached", return_value={})):
        tr = dash.trees()
        assert "make lint" in tr[0]["why"] and "`make lint`" in tr[0]["life"]["do"], tr[0]
