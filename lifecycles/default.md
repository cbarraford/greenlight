# Default MR lifecycle

Used for any repo without its own `lifecycles/<repo>.md`. Same as `examples/lifecycles/release-train.md`, minus the review bot and the milestone. Put your own in `~/.greenlight/lifecycles/<repo>.md`.

1. `mr_open` — An MR exists for the branch. Owner: agent. Implement the goal, push, and open a draft MR.
2. `ci_green` — The head pipeline succeeded (or there's no CI). Owner: CI while it's running; agent when it failed.
3. `no_conflicts` — No merge conflicts. Owner: agent (rebase).
4. `ready` — Not a draft. Owner: greenlight, unless the goal says to keep it as a draft. Then ask the user.
5. `threads` — No unresolved threads. Owner: agent (verify, then fix or reply, and resolve).
6. `approved` — The required approvals are in. Owner: reviewers.
7. `merge` — The forge reports it mergeable. Owner: greenlight merges automatically.

GitHub PRs follow the same steps: greenlight maps the PR onto the MR shape (checks → pipeline, review threads → threads, `mergeStateStatus` → merge status) and runs `gh pr ready` / `gh pr merge` itself (`merge_method` in config.json picks merge, squash or rebase).

A lifecycle can add an `Options:` line of backticked flags:
- `solo`: nobody else reviews this repo. A mergeable MR/PR with no other human's approval goes to the user ("review it, then merge?") with the merge command attached, instead of waiting on reviewers.
- `automerge`: the lifecycle's own steps stand in for a human approval. A mergeable MR/PR whose every earlier step passed is merged by greenlight without waiting on reviewers or asking the user (a goal that says "hold" still asks). It needs at least one of your own steps before `merge` that checks the work (an agent's independent reviews, a try-out, each `Done: `label=…``); a lifecycle with none never automerges.
- `merge_any_base`: greenlight may merge an MR/PR whose target isn't the default branch. Without it a stacked GitHub PR waits on the PR its base branch comes from (`dependency`), or asks the user when there is none.

A step that isn't one of the built-ins above is your own: ``N. `qa` — QA passed. Done: `label=qa-ok` `` passes once the MR has that label (the agent adds it when the requirement is met), and ``Done: `signoff` `` asks you once per MR. Steps after `merge` are what's left once it lands (deploy, verify): ``8. `deploy` — Deployed to prod. Done: `signoff` ``. They can only be your own steps (signoff or label), they're checked only on your own MRs after they merge, and cleanup keeps the workspace until they're done. `mr_open` is the implement phase: the goal is being built until the MR exists.

When a step is on you (or it's an agent's step and no agent is on it), the workspace's card recommends what to do next. The built-in steps have written-out advice; for your own steps the model writes one line alongside the next-step prompt. Click the card's steps bar to see the whole lifecycle as a checklist. A `## Rules` section at the end holds standing requirements every agent on the repo is told to follow. The app's Settings → Repositories edits all of this.
