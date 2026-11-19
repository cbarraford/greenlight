---
name: greenlight
description: Run greenlight, the user's agent manager across every coding-agent workspace (Orca, cmux, herdr or T3). It checks each workspace's goal and its repo's MR lifecycle, works out the next step and who it's waiting on, does mechanical steps itself (assign the review bot, milestone, mark ready, merge), dispatches agents to idle work, deletes finished workspaces, asks the user what only they can answer, and publishes an HTML report. Use for /greenlight, "run greenlight", "what needs me", "triage workspaces", "clean up workspaces", or when the Greenlight automation fires.
---

# Greenlight

Every workspace has a **goal**: the workspace comment (Orca), description (cmux) or `~/.greenlight/goals.json` entry (herdr, T3), always set through `greenlight.py ade worktree set --comment`, and nothing else goes there. An MR workspace's goal is "Merge !N" (the user's MR) or "Review !N" (someone else's); "Merge MR for #N" means the MR/PR that closes issue N or comes from its branch. GitHub repos are tracked like GitLab ones: greenlight.py maps each PR onto the MR shape with `gh`, and their refs read `#N`. A workspace without an MR has a free-form goal. The repo's lifecycle (`~/.greenlight/lifecycles/<repo>.md`, then the bundled `lifecycles/<repo>.md`, falling back to `default.md`) defines the steps toward merging. A step that isn't built in is the user's own: it passes on a label (`Done: `label=x``: the agent adds the label once the requirement is met) or on the user's sign-off (`Done: `signoff``: greenlight.py asks once per MR and records the answer). Steps listed after `merge` (deploy, verify) are the user's own, checked once their MR has merged; cleanup keeps the workspace until they pass. A lifecycle's `## Rules` section is the user's standing requirements for that repo: every agent brief for it carries those rules, and you hold agents to them. The user edits all of this in the app (Settings → Repositories). `greenlight.py` does the deterministic part; you act on its output.

If `~/.greenlight/instructions.md` exists, read it first: the user's standing instructions to you (Settings → Model in the app), and they override the defaults here. If `~/.claude/skills/greenlight/memory/MEMORY.md` exists, read it and the notes it links to first (it's the owner's own, never committed; create it on the first decision worth keeping). They hold the decisions the user has already made; don't re-ask them. When the user makes a new decision or corrects you, update the matching note there (one fact per file, frontmatter as in the existing notes, `[[links]]` between notes).

**You orchestrate; agents do the work.** Never do the work in your own session or context: no code changes, debugging, investigating a failure, reviewing a diff, rebasing, or writing docs/replies. Every such task goes to an agent in the workspace it belongs to (`greenlight.py ade agent launch --agent <claude|codex|opencode|pi> --worktree id:<id> --prompt "..."`, or `greenlight.py ade terminal send` to its idle agent, as in §2). If no workspace fits, create one with a goal: `greenlight.py ade worktree create --name <name> --repo <repo> --comment "<goal>" --agent <first of config agents> --prompt "..."`. You only read state (`greenlight.py`, `greenlight.py ade worktree ps`, `greenlight.py ade terminal read`, `glab mr view` / `gh pr view`, `blocking.py`), run the lifecycle's one-line `next.cmd` steps, decide, dispatch, and relay what agents report. Keep agent output out of your context: read the last screen or summary, not whole logs. A small question from the user ("what's !101's status?") is answered from that state, not by reading code.

**Workspace calls go through `python3 ~/.claude/skills/greenlight/scripts/greenlight.py ade <orca args>`** (`greenlight.py ade` below): it takes Orca's CLI arguments and runs them on the ADE in use (`GREENLIGHT_ADE=orca`, the default, `cmux`, `herdr` or `t3`), printing Orca-shaped JSON. Anything marked Orca-only below is skipped on the others. With Orca, load `orca-cli` first (`orca skills get orca-cli`). On Linux, outside an Orca terminal, the binary is `orca-ide`; bare `orca` there is the screen reader.

## 1. Scan and report

```bash
python3 ~/.claude/skills/greenlight/scripts/adopt.py apply         # workspaces for open MRs that have none (once per run, first)
python3 ~/.claude/skills/greenlight/scripts/greenlight.py report        # writes ~/.greenlight/report.html and ~/.greenlight/last.json
```

`adopt.py apply` gives every open MR/PR the user wrote or is a requested reviewer on, or (GitLab, when config `blocking` is set) is blocking by the team's rules (`blocking.py`: approval, discussion, conflict, pipeline or orphan, even when not a requested reviewer), a workspace when none has it: authored MRs on their source branch ("Merge !N: <title>"), reviews on `mr-<N>` ("Review !N: <title>"), linked MR, no agent, setup hooks skipped. It runs before the report so they're hexes this run. It skips review MRs the user has approved and isn't blocking, someone else's drafts, and MRs whose workspace was deleted while the MR was open (tombstoned in `~/.greenlight/dismissed.json` until the user is re-requested as reviewer, or the MR changes while still blocked on them). If the blocking scan fails, it runs on the author/reviewer queries alone. It creates at most 10 per run, and pings by itself for projects with no repo in the ADE (`@no-repo:<project>`) and branches checked out elsewhere (`@adopt-busy`). "ignore its MRs" on a `@no-repo` ping: `adopt.py dismiss <project>!<iid>...` for each MR it names, then `greenlight.py resolve @no-repo:<project>`. Report its CREATED entries; `adopt.py scan` shows why each MR was skipped.

Orca-only: open it in Orca's built-in browser: `orca tab create --url file://$HOME/.greenlight/report.html` (or `orca reload` if a tab is already open). **Never** run `orca artifacts share` on it. It can name private (e.g. security) MRs.

Then, if config `blocking` is set, list what the user is blocking: `python3 ~/.claude/skills/greenlight/scripts/blocking.py` (the team's rules computed from GitLab; it reuses the scan adopt.py just cached). It exits non-zero when GitLab couldn't answer: say so, don't report "nothing blocked". Put the blocks at the top of the summary, with `approval`/`discussion` first. Their hexes already show "Needs you" with the reason. A block whose MR has no workspace is adopt.py's job, whether or not the user is a requested reviewer: it already created the workspace this run (or the next, past the cap), or `adopt.py scan` (tagged `[review/<reason>]`) names why it won't. **Never** ask or ping "create a Review !N workspace?" for one, and resolve any old `@review-*` ping for it with `greenlight.py resolve`. Only a block adopt.py skips for a reason the user can change (no repo in the ADE) is worth a `me` item.

Read `~/.greenlight/last.json`. Each row has `next = {step, owner, action, cmd?, agent?}`. Don't re-derive what it decided.

## 2. Act by owner

- **greenlight**: run `next.cmd` exactly (glab assign / milestone / ready / merge, or `gh pr ready` / `gh pr merge` for a GitHub repo). Merges are automatic; `greenlight.py` already holds back any MR/PR whose goal says "hold" or "don't merge" (words the user added, not the MR title the goal copies in; "security-hold" is a name, not a hold), and never auto-merges a GitHub PR into a branch other than the repo's default (unless its lifecycle has `merge_any_base`).
- **agent** (no agent working on it): dispatch one task.
  - Agent: the first of config `agents` (default claude, then codex), unless the goal names another, or `next.agent` names the next one (the first hit its usage limit).
  - Prompt: (Orca-only) reuse the repo's Orca quick command when one matches: `jq '.settings.terminalQuickCommands' ~/.config/orca/profiles/*/orca-data.json`, matched by repoId (conflict → "Rebase Branch", CI → "Fix CI", threads → "Resolve MR Threads" / "Address Feedback"). Otherwise write a `/goal` brief: the workspace goal, the lifecycle file path, `next.action`, the MR URL, the failing job or thread IDs, and the lifecycle's `## Rules` section verbatim when it has one. Brief rules:
    - verify each review claim with a refuting subagent before acting on it;
    - fix it with a test, or reply inline with evidence, then resolve the thread (always leave a comment);
    - get CI green;
    - keep drafts as drafts;
  - Send: if the row has `agent.terminal` and that terminal is idle with an empty input box, run `greenlight.py ade terminal send --terminal <h> --text "<prompt>" --enter --wait-submit 10`. **Never** type into an unsent draft. Otherwise run `greenlight.py ade agent launch --agent <agent> --worktree id:<id> --prompt "<prompt>" --json`.
  - If the launch or terminal reports a usage/rate limit, re-dispatch that one task with the next of config `agents`.
- **no workspace yet**: `adopt.py apply` (§1) already created one per open MR of mine or review request, so it's a row above.
- **done**: `python3 ~/.claude/skills/greenlight/scripts/cleanup.py apply` once per run. It deletes merged/closed MRs and review MRs the user has approved, but only when nothing can be lost. Report its SKIPPED entries; don't resolve them yourself.
- **me**: see section 3. A `me` row with a `next.cmd` (a solo merge, a custom stage's sign-off) runs that cmd exactly once the user says yes.
- **parked**: the goal says `(revisit YYYY-MM-DD)` and that date is still in the future, or it says `(parked)` (no end date; also hidden from the report and dashboard). Do nothing. When the user says "revisit in N days", write the date out. When the user says "park X", append ` (parked)` to its goal; "unpark X" removes it.
- **plan**: the goal starts with `Plan:` (a leftover planner workspace). Never drive it.
- **working / ci / reviewers / author / dependency**: nothing to do (`dependency`: a stacked MR/PR waiting on the one its target branch comes from). Never ping reviewers. A free-form goal tagged `(automation)` is driven by an Orca automation, so greenlight leaves it alone.

Goal backfill (no question needed): a row with an MR and an empty `goal` gets `greenlight.py ade worktree set --worktree id:<id> --comment "Merge !N: <title>"` (or "Review !N: <title>"), plus `--gitlab-mr N` if `linked_mr` is null (Orca-only). Carry any "stay draft" / "keep draft" / "hold" from the old comment into the new goal, e.g. `Merge !101: <title> (stay draft)`. The old status line in `comment` is then overwritten. It's preserved in the log.

Board status (Orca-only): `greenlight.py ade worktree set --worktree id:<id> --workspace-status <s>`. me → `in-review --unread`. agent / working / ci → `in-progress`. reviewers / author → `in-review`. Only change it when it's different.

**Workspace history.** Each workspace has its own history in `~/.greenlight/history/<name>.md` (a `/` in the name becomes `__`). Before you ask about a workspace, dispatch work to it or delete it, read that history with `python3 ~/.claude/skills/greenlight/scripts/greenlight.py history <name>`. After every decision the user makes, every action you take (dispatch, merge, goal change, delete) and anything notable an agent reports, add a dated line with `greenlight.py note <name> "<status → decision/action → why>"`. Name the MR and the agent involved. The file is kept after the workspace is deleted.

Log every mutation as one line: `echo '{"ts":<epoch>,"ws":"<name>","mr":"<!N>","did":"<what>","old_comment":"<if overwritten>"}' >> ~/.greenlight/log.jsonl`.

## 3. Things only the user can answer

**On-demand run** (the user is in the session): batch the `me` rows into AskUserQuestion. Up to 4 questions per call; group similar rows ("which of these automation runs can I delete?" multiSelect; "goal for X?" offering 2–3 guesses inferred from the branch, recent commits and the agent's last message). Apply the answers: goals go to `--comment`, and approved deletes go to `greenlight.py ade worktree rm --worktree id:<id>`, only after `git status --porcelain` is clean and `git log @{u}..` is empty (otherwise ask again).

**Automation run** (nobody there): don't ask. The `me` rows are already marked unread and sit at the top of the report. Also ping the user once per new `me` item: `python3 ~/.claude/skills/greenlight/scripts/greenlight.py ping --key "<workspace name>" $'<workspace>: <question> <MR URL>\nOptions: <answer> | <answer>'`. The `--key` lets a later scan close the question in the app by itself once that workspace is gone or no longer `me`. Group look-alike rows into one ping (all held merges, all keep-draft MRs) instead of one per workspace. A grouped ping, or one with no workspace (a blocking-scan block, say), takes a key starting with `@` (`@held-merges`, `@acme/widgets!147`); scans skip those, so run `greenlight.py resolve <key> "<why>"` yourself once it no longer applies. A grouped ping that is about particular workspaces names each with `--ws <workspace>` (repeatable; name or id from the scan, an unknown one sends nothing), e.g. `greenlight.py ping --key @3.22-ship --ws fix-1 --ws fix-2 --ws feat-3 "..."`: the dashboard lifts those workspaces to their own "Needs you" hex (off the shelf, out of any stack) until the user answers. A workspace `--key` counts as its own `--ws`. Always end the text with a final line `Options: first | second` (in the same quoted argument, after a newline): they become one-tap buttons, and a ping without one is refused. It pops a notification on the user's Mac (the Greenlight menubar app) and waits in its bell inbox until the user answers; the reply comes back to the menubar chat session, not to you. Don't re-ping an item you pinged in an earlier run (check its history).

**Menubar chat** (`chat.py`, no terminal): AskUserQuestion isn't available. Ask in plain text, one question at a time. Use `greenlight.py ping` only for things the user should see while away from the chat.

## 4. Finish

Reply in 5–10 lines: the report link, counts per owner, what you did (adopted / merged / dispatched / deleted / backfilled), and what needs the user, as clickable MR links.

## Automations

There are two kinds, and "my automations" means both. Managing them is orchestration, so you do it yourself (no agent needed).
- **greenlight's own**: `python3 ~/.claude/skills/greenlight/scripts/auto.py` on the host greenlight runs on. It's Orca-style: a 5-field cron schedule in that host's local time, a prompt, an optional precheck (a non-zero exit skips the run), and the agents of config `agents` in order, the next taking over when one hits its limit (permissions per harness.py: safe unless `harnesses.<name>.bypass`). A failed run pings the user.
  - `auto.py add <name> --schedule "0 9 * * 1-5" --prompt "<text>" [--cwd DIR] [--precheck CMD] [--provider claude|codex|opencode|pi] [--model M] [--effort E] [--timeout MIN]`
  - `auto.py list`, `auto.py edit <name> --disabled|--enabled|<option>`, `auto.py rm <name>`, `auto.py run <name>` (now), `auto.py runs [name]` (each run's log path is in `~/.greenlight/auto/runs.jsonl`).
- **Orca** (Orca-only): `orca automations list|show|create|edit|remove|run|runs` (`--json`; see each command's `--help`). Edit takes `--id`, plus `--enabled`/`--disabled` and any create option. Triggers are a preset, cron or RRULE.

How to handle requests:
- **List**: show both in one list, tagged `orca` or `greenlight`. For each, give the name, the schedule in plain words ("weekdays 9:10"), whether it's on or off, and the last run's result. Keep it short.
- **Create**: make it one of greenlight's own unless the user says Orca, or it needs to run inside an Orca workspace. Before creating, say back the name, the schedule in plain words with the timezone, the prompt, and any precheck, in one short message ending `Options: create | change something`. If the prompt does work in a repo, set `--cwd` (greenlight) or `--workspace`/`--repo` (Orca) to that repo.
- **Edit**: find it by name across both kinds. If the name matches more than one, ask which. Show what changes (before → after), then apply it.
- **Remove**: always confirm first (`Options: remove | keep`). Orca's `remove` also deletes the run history. Suggest disabling it as the alternative.
- **Run now / runs**: `auto.py run|runs`, or `orca automations run|runs --id`.
- Never move an automation between the two kinds unless the user asks.
