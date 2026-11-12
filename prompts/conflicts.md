Fix the merge conflicts on {mr} ({url}), {branch} → {target}. Follow this repo's rules for agents. Don't push, post, resolve or retry anything until I say so.

1. git fetch; make sure {branch} is checked out at {sha} with a clean tree. Stop and tell me if it isn't.

2. Rebase onto origin/{target}. Merge instead if this repo's rules say so.

3. For each conflict, read both sides and the {target} commits that touched it (git log origin/{target} -- <file>). Keep both intents. If they can't both hold, stop and ask me; don't pick one.

4. Generated files and lockfiles: regenerate them, don't hand-merge.

5. Build, and run the tests for what the conflicts touched.

6. Show me each resolution: the file, what each side wanted, what you kept.

When I say so: git push --force-with-lease. If someone pushed to {branch} since {sha}, stop and tell me instead.
