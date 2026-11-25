Fix the merge conflicts on {mr} ({url}), {branch} → {target}. Follow this repo's rules for agents. Don't push, post, resolve or retry anything until I say so.

1. git fetch; make sure {branch} is checked out with a clean tree and its HEAD contains {sha} (`git merge-base --is-ancestor {sha} HEAD`). Commits on top of {sha} are earlier unpushed work: keep them, and carry them through the steps below. Stop and tell me only if the tree isn't clean or HEAD doesn't contain {sha}.

2. Rebase onto origin/{target}. Merge instead if this repo's rules say so.

3. For each conflict, read both sides and the {target} commits that touched it (git log origin/{target} -- <file>). Keep both intents. If they can't both hold, stop and ask me; don't pick one.

4. Generated files and lockfiles: regenerate them, don't hand-merge.

5. Build, and run the tests for what the conflicts touched.

6. Show me each resolution: the file, what each side wanted, what you kept.

When I say so: run `python3 ~/.claude/skills/greenlight/scripts/greenlight.py scan`, then `python3 ~/.claude/skills/greenlight/scripts/audit.py gate push --execute --force-with-lease` from the clean worktree. The gate pushes the audited commit; do not run a separate git push. If someone pushed to {branch} since {sha}, stop and tell me instead.
