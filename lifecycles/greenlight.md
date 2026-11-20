# greenlight PR lifecycle

This repo on GitHub. Same steps as `default.md`, run on the PR with `gh`. The maintainer is the only
reviewer, and GitHub won't let them approve their own PR, so the agent's three independent reviews and its try-out
stand in for an approval: once they pass, greenlight merges by itself (`automerge`). Deploying stays the maintainer's call: after the merge, the card's Deploy button pulls `main` into the host's skill checkout and signs the step off.

Options: `automerge`

1. `mr_open` — A PR exists for the branch (Orca's linked PR, else the head branch, else the open PR that closes the goal's issue). Owner: agent. Implement the goal, push, and open a draft PR that says `Closes #N`. A change under `app/` isn't done until `python3 scripts/dev.py log --wait`, in the worktree, prints `ok` (the Mac built it; a Linux host can't compile the Swift), and the PR says it did; if it prints "No answer", say that instead.
2. `ci_green` — Every check on the head commit passed (or there are none). Owner: CI while running; agent when one failed.
3. `no_conflicts` — GitHub reports no conflicts. Owner: agent (rebase).
4. `reviewed` — Three independent reviews of the PR's whole diff found nothing left to fix. Owner: agent. Run the three in parallel, each in a fresh context given only the goal and the diff: one per agent where installed (a Claude subagent, `codex exec`, `opencode run`), else three Claude subagents (correctness; edge cases and lost data; simplicity). Verify each finding with a subagent told to refute it, fix the real ones (with a test when it's logic), push, and list in the PR what each reviewer found and what changed. Done: `label=reviewed`
5. `tried` — The change was tried out for real and works. Owner: agent. Use it the way the user would: a script against a scratch `GREENLIGHT_HOME`, the dashboard in a headless browser against `scripts/dash.py`, the app through its dev copy (`python3 scripts/dev.py log --wait`). Fix what breaks and try again. Say in the PR what was tried and what it showed; when part of it can't be tried here (the Mac's UI, voice, the user's accounts), say which part and add the label anyway. Done: `label=tried`
6. `ready` — Not a draft. Owner: greenlight (`gh pr ready`), unless the goal says to keep it as a draft. Then ask the user.
7. `threads` — No unresolved review threads. Owner: agent (verify, then fix or reply, and resolve).
8. `merge` — GitHub reports it mergeable. A PR into `main` is merged by greenlight (`gh pr merge --merge`) once every step above passes; a goal that says "hold" still comes to the user. A PR stacked on another branch waits on that branch's PR to merge first; greenlight never merges it into the side branch.
9. `deploy` — The host runs `main` (or this change doesn't need deploying). Rebuild the app (`app/build.sh`) if `app/` changed. Done: `signoff` Run: `cd ~/.claude/skills/greenlight && git checkout -q main && git pull -q --ff-only && pkill -f "[d]ash.py stream"; true`
