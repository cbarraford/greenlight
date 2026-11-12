Review {mr} ({url}), "{title}", {branch} → {target}. I'm the reviewer; you're preparing my review. Don't change code, push, approve or post anything until I say so.

1. Understand the intent. Read the description, linked issues and every existing thread (glab or gh, whichever hosts this repo). Say in one sentence what the change is meant to do. A missing description, or one the diff doesn't match, is a finding.

2. Read the change in context. The head is {sha}; I last reviewed at {last_review_sha} ("none": this is my first review). git fetch, and make sure this checkout is at {sha}; if it isn't, say so and review {sha}. The whole change is git diff origin/{target}...{sha}. If I reviewed before, start with git diff {last_review_sha}..{sha}: what changed since, and whether my earlier threads were addressed. Then open the code around each hunk: callers of what changed, the tests, config and migrations it touches. Follow this repo's rules for agents.

3. Hunt for real problems, most costly first:
   - Correctness: wrong logic, missed edges (empty, zero, max, nil, duplicate, unicode, time zones), off-by-ones, errors swallowed or mishandled, races and ordering, state left half-written on failure.
   - Security: untrusted input reaching a query, shell, path, template or deserializer; missing or bypassable authz; secrets in code or logs; unsafe defaults.
   - Compatibility: public APIs, wire and storage formats, migrations without a way back, behavior callers rely on, determinism where it matters.
   - Tests: would a test fail without this change? Are the risky branches covered? Flaky or tautological tests.
   - Scope: anything the description doesn't mention, debug leftovers, dead code, unrelated churn.
   - Simplicity: code that could be deleted, or that redoes something the repo or stdlib already has.

4. Prove each finding before you keep it: the exact line, and the input or sequence that breaks it. Run a test or a quick repro when you can. Drop what you can't show. No style nits unless the repo's rules ask for them.

5. Run the tests and linters this repo uses for the touched code. Say what you ran and what failed.

Then report, most severe first:
   - Verdict, in one line: approve, approve with nits, or request changes, and why.
   - Findings: blocker / major / minor / nit, file:line, what's wrong, the case that breaks it, the fix you'd suggest.
   - Questions for the author: what the code alone couldn't tell you.
   - What you checked, and what you didn't.

Ask me which findings to post. Post the ones I pick as draft threads on their lines, with a suggestion block when the fix is small. I submit the review myself.
