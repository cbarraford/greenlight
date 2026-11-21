# greenlight PR lifecycle

This repo on GitHub. Same steps as `default.md`, run on the PR with `gh`. The maintainer is the only
reviewer, and GitHub won't let them approve their own PR, so the agent's three independent reviews and its try-out
stand in for an approval: once they pass, greenlight merges by itself (`automerge`). There's no deploy step: a merged PR is
done, and cleanup removes its workspace. Updating the host's skill checkout to `main` (and `app/build.sh`
when `app/` changed) is the maintainer's, outside the lifecycle.

Options: `automerge`

1. `mr_open` — A PR exists for the branch (Orca's linked PR, else the head branch, else the open PR that closes the goal's issue). Owner: agent. Implement the goal, run `python3 ~/.claude/skills/greenlight/scripts/greenlight.py scan`, then `python3 ~/.claude/skills/greenlight/scripts/audit.py gate push --execute` from the clean worktree to audit and push the exact commit (add `--target <base branch>` when the PR will target a nondefault branch). Put the draft PR's exact title and body, including `Closes #N`, in a JSON file outside the worktree as `{ "title": "...", "body": "...", "target": "<base branch>" }`, then run `python3 ~/.claude/skills/greenlight/scripts/audit.py gate pr <metadata.json> --execute` to audit and create it. Use the push gate again for every later agent push. A change under `app/` isn't done until `python3 scripts/dev.py log --wait`, in the worktree, prints `ok` (the Mac built it; a Linux host can't compile the Swift), and the PR says it did; if it prints "No answer", say that instead.
2. `ci_green` — Every check on the head commit passed (or there are none). Owner: CI while running; agent when one failed.
3. `no_conflicts` — GitHub reports no conflicts. Owner: agent (rebase).
4. `reviewed` — Three independent reviews of the PR's whole diff found nothing left to fix. Owner: agent. Run the three in parallel, each in a fresh context given only the goal and the diff: one per agent where installed (a Claude subagent, `codex exec`, `opencode run`), else three Claude subagents (correctness; edge cases and lost data; simplicity). Verify each finding with a subagent told to refute it, fix the real ones (with a test when it's logic), push, and list in the PR what each reviewer found and what changed. Commits pushed after the label (a fix from the try-out or a thread) get the same three reviews on their own diff before the PR merges. Done: `label=reviewed`
5. `tried` — The change was tried out for real and works. Owner: agent. Use it the way the user would: a script against a scratch `GREENLIGHT_HOME`, the dashboard in a headless browser against `scripts/dash.py`, the app through its dev copy (`python3 scripts/dev.py log --wait`). Fix what breaks and try again. Say in the PR what was tried and what it showed; when part of it can't be tried here (the Mac's UI, voice, the user's accounts), say which part and add the label anyway. Done: `label=tried`
6. `ready` — Not a draft. Owner: greenlight (`gh pr ready`), unless the goal says to keep it as a draft. Then ask the user.
7. `threads` — No unresolved review threads. Owner: agent (verify, then fix or reply, and resolve).
8. `merge` — GitHub reports it mergeable. A PR into `main` is merged by greenlight (`gh pr merge --merge`) once every step above passes; a goal that says "hold" still comes to the user. A PR stacked on another branch waits on that branch's PR to merge first; greenlight never merges it into the side branch.
