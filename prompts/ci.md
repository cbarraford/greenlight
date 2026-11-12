Fix the failed pipeline on {mr} ({url}), {branch} at {sha}: {pipeline_url}. Follow this repo's rules for agents. Don't push, post, resolve or retry anything until I say so.

Failed jobs:
{failed_jobs}

1. git fetch; make sure {branch} is checked out at {sha} with a clean tree. Stop and tell me if it isn't.

2. Read each failed job's log (glab ci trace, or gh run view --log-failed). Find the first real error, not the last line.

3. Say which it is:
   - real: this change broke it;
   - flaky or infra: a timeout, a runner, a network pull; say what in the log shows it;
   - already broken on {target}: check the latest {target} pipeline.

4. Real: reproduce it locally with the job's own command, then fix the cause. Don't skip or loosen tests, widen timeouts, or hide it behind a retry.

5. Show me the cause in one line, the diff, and what you ran to prove it passes.

When I say so: push. Retry a flaky job only when I say so. If {target} is broken too, say so and stop.
