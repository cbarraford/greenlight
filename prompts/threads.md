Resolve the open review threads on {mr} ({url}), "{title}", {branch} → {target}. Follow this repo's rules for agents. Don't push, post, resolve or retry anything until I say so.

Open threads:
{threads}

1. git fetch; make sure {branch} is checked out with a clean tree and its HEAD contains {sha} (`git merge-base --is-ancestor {sha} HEAD`). Commits on top of {sha} are earlier unpushed work: keep them, and carry them through the steps below. Stop and tell me only if the tree isn't clean or HEAD doesn't contain {sha}.

2. For each thread, read the whole discussion (glab or gh, whichever hosts this repo) and the code it points at, as it is now.

3. Sort each one:
   - fix it: the reviewer is right;
   - answer it: a question, or a misreading the code can clear up;
   - disagree: you think it's wrong; say why, with the line or test that shows it;
   - already handled: a later commit fixed it; name the commit.

4. Fix the ones to fix as small commits, one per thread when that's natural. Don't touch code no thread asked about. Run the tests for what you touched.

5. Show me, per thread: the verdict, the diff, and the reply you'd post. If a real bug slipped past earlier verification, suggest one diff-triggered line for the repo's `## Checklist`.

When I say so: run `python3 ~/.claude/skills/greenlight/scripts/greenlight.py scan`, then `python3 ~/.claude/skills/greenlight/scripts/audit.py gate push --execute` from the clean worktree; it pushes the audited commit. Write each reply as JSON with its live forge `thread_id` and body; run `python3 ~/.claude/skills/greenlight/scripts/audit.py gate post <file> --kind reply --execute`. The gate posts and resolves only the approved thread IDs; do not post or resolve them separately. Leave the rest open for the reviewer.
