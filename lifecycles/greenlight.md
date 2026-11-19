# greenlight PR lifecycle

This repo on GitHub. Same steps as `default.md`, run on the PR with `gh`. The maintainer is the only
reviewer, and GitHub won't let them approve their own PR, so the last step asks them instead of waiting on an
approval that never comes. Deploying (`sync.sh`) stays the maintainer's call: after the merge, greenlight asks them to sign it off.

Options: `solo`

1. `mr_open` — A PR exists for the branch (Orca's linked PR, else the head branch, else the open PR that closes the goal's issue). Owner: agent. Implement the goal, push, and open a draft PR that says `Closes #N`.
2. `ci_green` — Every check on the head commit passed (or there are none). Owner: CI while running; agent when one failed.
3. `no_conflicts` — GitHub reports no conflicts. Owner: agent (rebase).
4. `ready` — Not a draft. Owner: greenlight (`gh pr ready`), unless the goal says to keep it as a draft. Then ask the user.
5. `threads` — No unresolved review threads. Owner: agent (verify, then fix or reply, and resolve).
6. `merge` — GitHub reports it mergeable. A PR into `main` goes to the user: review it, then greenlight runs `gh pr merge --merge`. A PR stacked on another branch waits on that branch's PR to merge first; greenlight never merges it into the side branch.
7. `deploy` — Deployed with `scripts/sync.sh` (or not needed for this change). Done: `signoff`
