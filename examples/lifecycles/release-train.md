# Release-train MR lifecycle (example)

Copy to `~/.greenlight/lifecycles/<repo>.md` for a GitLab repo with a review bot and release milestones.

The first failing step is the next step. Each step names the check `greenlight.py` runs, who owns
fixing it, and how. Steps 1–2 (branch, code change) are done once an MR exists. Before that,
the workspace goal tells the agent what to build; the agent opens a draft MR when it's ready.

1. `mr_open` — An MR exists for the branch. Owner: agent. Implement the goal, push, and open a draft MR.
2. `ci_green` — The head pipeline succeeded. Owner: CI while it's running; agent when it failed ("Fix CI" quick command, plus the failing job IDs).
3. `no_conflicts` — No merge conflicts. Owner: agent ("Rebase Branch").
4. `review_bot` — `review_bot` from config.json is both assignee and reviewer (skipped when unset). Owner: greenlight (`glab mr update`).
5. `milestone` — A milestone is set. Owner: greenlight, using the active <prefix>x.y.z due latest (prefix from `milestone=` on an `Options:` line, default `Release-`). If that's ambiguous, ask. Drop this step for a repo without release milestones.
6. `ready` — Not a draft. Owner: greenlight (`glab mr update --ready`), unless the goal says to keep it as a draft. Then ask the user.
7. `threads` — No unresolved threads. Owner: agent ("Resolve MR Threads"). Verify each claim. If it's already addressed, resolve it with a comment explaining why. Otherwise fix it, then comment and resolve.
8. `approved` — The required approvals are in. Owner: reviewers.
9. `merge` — GitLab reports `mergeable`. Owner: greenlight merges automatically.
