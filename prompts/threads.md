Resolve the open review threads on {mr} ({url}), "{title}", {branch} → {target}. Follow this repo's rules for agents. Don't push, post, resolve or retry anything until I say so.

Open threads:
{threads}

1. git fetch; make sure {branch} is checked out at {sha} with a clean tree. Stop and tell me if it isn't.

2. For each thread, read the whole discussion (glab or gh, whichever hosts this repo) and the code it points at, as it is now.

3. Sort each one:
   - fix it: the reviewer is right;
   - answer it: a question, or a misreading the code can clear up;
   - disagree: you think it's wrong; say why, with the line or test that shows it;
   - already handled: a later commit fixed it; name the commit.

4. Fix the ones to fix as small commits, one per thread when that's natural. Don't touch code no thread asked about. Run the tests for what you touched.

5. Show me, per thread: the verdict, the diff, and the reply you'd post.

When I say so: push, post the replies, and resolve only the threads you fixed. Leave the rest open for the reviewer.
