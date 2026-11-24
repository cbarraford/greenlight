# Greenlight

**An agent manager for coding-agent workspaces.** Run a dozen Claude Code, Codex, Gemini, opencode or pi agents in
parallel worktrees, and let Greenlight keep every one of them moving until its change merges.

Each workspace gets a **goal** ("Merge !42", "Review #17", or anything free-form). Greenlight walks it through its
repo's MR/PR lifecycle: it works out the next step and who owns it, does the mechanical steps itself (mark ready, set a
milestone, assign a review bot, merge), dispatches an agent to anything idle, cleans up finished workspaces, and only
asks you what only you can answer.

> **Status: alpha.** Greenlight is built and used daily by one person, against GitLab and GitHub. Expect rough edges
> and fast change. Issues and PRs are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).

## What you get

- **A Claude Code skill** (`SKILL.md` + stdlib Python): run `/greenlight` and it scans every workspace, reports what
  each needs and does the safe steps.
- **A scheduler** (`auto.py`): the same work on a timer, with or without an LLM session, so nothing sits idle overnight.
- **A macOS menubar app** with three floors:
  - **the Pipe**: just the terminal, dark;
  - **the Basement** (⌘B): the terminal with a sidebar of hosts and workspaces;
  - **Greenlight Center** (⌘⇧G): the dashboard of every workspace and its lifecycle stage, plus a chat (text or voice)
    with **Chief**, Greenlight's own agent, which can answer questions about the board and act on it.
- **Replyable notifications**: when Greenlight needs you, answer from the banner.
- **Pluggable everything**: forges (`glab`, `gh`), agent CLIs, and workspace managers (Orca, cmux, herdr, T3 Code).

## How it works

```
 your MRs/PRs ──adopt.py──▶ workspaces (git worktrees + an agent each)
                                   │
              scan (greenlight.py) │  lifecycle step, owner, next move
                                   ▼
        mechanical step? ──▶ Greenlight does it (ready, milestone, review bot, merge)
        agent's turn?    ──▶ dispatch the next agent with the workspace's state card
        your turn?       ──▶ one question, in the app, a notification or the chat
        merged / closed? ──▶ cleanup.py removes the workspace once nothing can be lost
```

A repo's lifecycle is a short Markdown file (`lifecycles/default.md`), so a team's own process (review bots, release
milestones, required approvals) is a config change, not a code change.

## Requirements

- Python 3.10+ (stdlib only).
- `glab` (GitLab) and/or `gh` (GitHub), authenticated.
- At least one coding agent CLI: `claude`, `codex`, `agy` (Antigravity), `opencode`, `pi` or `gemini` (Gemini CLI).
- One workspace backend: Orca (default for now), Greenlight's own `native` engine (git worktrees + `tmux -L greenlight`; register repos with `greenlight.py repos add <path>`), or the legacy cmux, herdr and T3 Code drivers. Pick it with `GREENLIGHT_ADE=orca|native|cmux|herdr|t3`, or `"ade"` in `config.json` (`scripts/ade.py`). #195 makes `native` the default.
- The menubar app: macOS with the Command Line Tools (`swiftc`). The backend also runs on Linux.

## Quick start

```bash
git clone https://github.com/cbarraford/greenlight ~/.claude/skills/greenlight   # Claude Code's personal-skill path
cd ~/.claude/skills/greenlight
python3 scripts/diag.py doctor         # checks glab/gh auth, agent CLIs and the rest of the setup
python3 scripts/greenlight.py report   # first scan: writes ~/.greenlight/report.html
python3 scripts/auto.py install        # the scheduler (systemd user timer, or launchd on macOS)
app/build.sh                           # optional: ~/Applications/Greenlight.app
```

Then run `/greenlight` in Claude Code, or add a scheduled run with `auto.py add` (see `SKILL.md`). For a scheduled run
with no LLM session, `auto.py add greenlight --schedule "*/30 * * * *" --orchestrator` runs `orchestrate.py tick`.

Greenlight can run on another machine (a dev box that's always on): put the checkout there, and pick it in the app's **Settings › Hosts** (any `Host` in `~/.ssh/config`; **Test** checks it, then relaunch), or set `GREENLIGHT_HOST`. `scripts/sync.sh` copies a local checkout to that host.

> **Safety.** Agents run with safe permissions by default. Setting `bypass` for a harness (below) lets an agent run any
> command unattended: only do that on a machine you're comfortable letting an agent drive.

**Verbose activity** (Settings › Chat & voice, off by default) adds a live log above the chat's message box: Chief's every command with its output and thinking, each workspace agent's state, and every move Greenlight makes.

## Configuration

State and config live in `$GREENLIGHT_HOME` (default `~/.greenlight`). `config.json` there overrides these defaults; each key replaces the default whole.

| key | default | what |
|---|---|---|
| `me` | asked from `glab`/`gh` once | your forge username |
| `review_bot` | `null` | a bot the `review_bot` lifecycle step assigns; `null` skips the step |
| `agents` | `["claude", "codex"]` | dispatch order; on a usage limit Greenlight moves to the next |
| `harnesses` | `{}` | per agent: `{"bypass": true}` for full access, `{"bin": "/path"}` |
| `llm_harness` | `"claude"` | runs the dashboard's own LLM calls (page design, suggestions) |
| `merge_method` | `"merge"` | `merge`, `squash` or `rebase` |
| `require_human_approval` | `true` | without `automerge`, a human signs off every merge: a reviewer where the forge requires approvals, else you ("merge it?"); `false`: a mergeable MR with no other human's approval merges anyway |
| `adopt` | `{"cap": 10, "roles": ["author", "reviewer"]}` | workspaces `adopt.py` creates per run, and for which of your MRs |
| `blocking` | `null` | GitLab only: a team roster for `blocking.py` (see its docstring); `null` turns it off |
| `suggest_rules` | `[]` | extra rules for the dashboard's suggested prompts |
| `name` | `"greenlight"` | what the dashboard's LLM prompts call it |

**Lifecycles.** A repo's steps come from `~/.greenlight/lifecycles/<repo>.md`, else the bundled `lifecycles/<repo>.md`, else `lifecycles/default.md`. `examples/lifecycles/release-train.md` adds a review bot and release milestones.

**Goal tags.** Words in a goal that change what Greenlight does: `(hold)` / `don't merge` (never auto-merge), `(stay draft)`, `(parked)` (ignored and hidden), `(revisit YYYY-MM-DD)` (ignored until then), `(automation)` (driven by an automation, Greenlight leaves it alone).

**`.greenlight.json`.** A repo can name its terminal project in its main checkout, so a teammate who clones it gets the same one. A window showing just that project asks, top right, whether to wear its look (its **Local Lights**; No keeps the default colors, and the look dialog switches between Default and Local Lights any time), opens its first shell on `host` (when it's one of yours) and lists only `hosts` (plus Local and any with shells open). The look dialog's **Save to <repo>** writes it; commit it to share. Folders stay per machine, in the app's own `projects.json`; a repo you put in a project yourself stays there.

```json
{"name": "Platform", "host": "devbox", "hosts": ["devbox", "gpu"],
 "look": {"dark":  {"strip": "#1d2330", "fg": "#d8dee9", "accent": "#88c0d0"},
          "light": {"strip": "#eef1f5", "fg": "#2e3440", "accent": "#5e81ac"}}}
```

`look` is a pair: `dark` for the Basement (the terminal, always dark), `light` kept for Greenlight Center (the dashboard, always light; nothing wears it yet). A flat `look` (older files) is the dark half. Keys: `strip fg dim hair accent focus o r g b p y t` (CSS colors, the chrome only). Repos in no project are **No Man's Land**. In a window, the look dialog (brush icon) also has eight **premade looks** under the prompt (a click wears one; Undo walks back) and **Advanced** (a color picker for every key, live).

## Agent harnesses

`harness.py` builds every headless agent command. Each harness runs with safe permissions unless you opt into bypass:

| harness | default | `harnesses.<name>.bypass: true` |
|---|---|---|
| claude | `--permission-mode acceptEdits` | `--permission-mode bypassPermissions` |
| codex | workspace-write sandbox | `--dangerously-bypass-approvals-and-sandbox` |
| agy | `--mode accept-edits` | `--dangerously-skip-permissions` |
| opencode | permission asks are rejected | `--auto` |
| pi | **refuses to run** | no flags (pi has no permission gate or sandbox) |
| gemini | `--approval-mode auto_edit --skip-trust` (headless Gemini CLI won't start in an untrusted folder; trusting it loads that folder's `.gemini` settings) | `--approval-mode yolo --skip-trust` |

Unattended work (automations, the menubar chat) runs shell commands, so in safe mode some tasks stop at a refused permission. Only set `bypass` on a machine you're comfortable letting an agent drive.

Support differs by harness:
- Claude, Codex and agy (Antigravity): first-class everywhere — dispatch, automations, menubar chat, background agents (workspace pages, suggestions, tab naming), System One (`decide.py`), Basement pane detection/launch/alerts, dashboard avatars and transcript mirror (`All output`), limit fallback, and dynamic model discovery (`harness.py models`; `<harness>:<model>` pins any model to a specific harness).
- opencode, pi and Gemini CLI: dispatch, automations, the menubar chat, limit fallback, cmux launch. The plain **Gemini CLI** entry runs that CLI's default model; `gemini-*` model IDs run through `agy`.
- All six: Greenlight reads an agent's terminal (its last ~60 lines, `ade_common.agent_state`) as one of working, waiting for you, finished, stuck in a loop, rate limited, logged out or an unsent draft. Claude, Codex, agy and Gemini drafts are recognised (`ade_common.draft`); opencode and pi drafts read as empty.
- Claude only: conversations can be compacted from the chat.

## System One

`scripts/decide.py` makes Greenlight's quick, calibrated calls: `bool`, `choice`, `score` and `batch` over one piece of text, each answer with its probability and the provider that gave it. It reads agents' terminals, the instructions in your goals (hold, stay draft, park, revisit on a date) and which hexes Chief's reply is about (P > 0.70 lights one; a lone hex at P > 0.85 opens its card). Set it in config.json, or in Settings › Automation › System One:

```json
"system_one": {"provider": "jev", "fallback": "haiku"}
```

- `jev`: TypeSafe's System One API. Key: `TYPESAFE_API_KEY`, then `decide.py key` (stdin; Settings' Set key… uses it).
- `haiku`: Claude Haiku 5.5 with structured output, over the Messages API when `ANTHROPIC_API_KEY` is set, else through `claude -p`.
- `local` (the default): no model. Every answer comes back uninformative (provider `local`), so Greenlight's own patterns decide, as before. Any failure falls back to this.

A model can only add to what the patterns find: it can add a hold or a draft but never remove one, and it parks a workspace only at P ≥ 0.85. `decide.py test` asks each configured model a question with a known answer.

## Diagnostics & privacy

Greenlight keeps a local diagnostic trail so an AI can troubleshoot it: `scripts/diag.py summary | errors | tail | doctor` (JSON).
It records structured events only (which command ran, how long it took, its exit code, the exception type and a class
such as `auth` or `timeout`, the code location, the app's menu and button commands) in `~/.greenlight/diag/events.jsonl` and
`~/Library/Logs/Greenlight/app.jsonl`, each capped at 5 MB × 4. It never records message text, titles, goals, argv,
stderr, paths, hosts, or workspace, branch and MR names: those appear only as salted hashes, and the salt never leaves
`~/.greenlight/diag/salt`. Nothing is uploaded. Delete it any time with `rm -r ~/.greenlight/diag ~/Library/Logs/Greenlight`.

To report a bug, use the app's **Help › Report a Bug…**, ask Chief to file one, or run
`diag.py report --title "…" --body-file -` (a preview) and then the same command with `--execute`, which files it on
cbarraford/greenlight with `gh` (or opens GitHub's form without it). The report carries the trail's summary, its
errors and its newest events. Every string is checked again against the secret and personal-data patterns `audit.py`
uses and your `leak.re`: an event that matches is left out, your own text must pass the same check, and if the check
can't run, nothing is exported. `diag.py bundle` writes the same data to a file (mode 0600) for you to read or attach.

## Layout

| path | what |
|---|---|
| `SKILL.md` | the skill: how Claude Code runs Greenlight |
| `AGENTS.md` | notes for coding agents (Codex, agy; `CLAUDE.md` and `GEMINI.md` point at it): the diagnostic trail and its privacy rules |
| `scripts/greenlight.py` | scan, next step per workspace, report, pings |
| `scripts/adopt.py` | a workspace for each open MR/PR of yours or awaiting your review |
| `scripts/placement.py` | where each workspace's agent runs; flags and recreates ones not on the host greenlight runs on |
| `scripts/lifestage.py` | where each change is in its lifecycle (stage and sub-stage), who holds it, the suggested move |
| `scripts/dwim.py` | the fast path for board commands said, typed in the chat or in ⌘J (“park the two auth ones”, “hold #42”, “fix CI on the red ones”): done in under a second with an Undo toast, or handed to Chief when it’s ambiguous or destructive; and the voice stage’s instant table / timeline / diff / lifecycle card |
| `scripts/cleanup.py` | deletes finished workspaces when nothing can be lost |
| `scripts/decisions.py` | the decision record: every question Greenlight asked you, the situation, and your answer (`~/.greenlight/decisions.jsonl`) |
| `scripts/auto.py` | Greenlight's own cron-style automations (`--orchestrator`: the deterministic tick instead of an agent) |
| `scripts/orchestrate.py` | a greenlight run with no LLM session: adopt, scan, `next.cmd`, goal backfill, templated dispatch with state cards, cleanup; `decide.py` only at ambiguous forks (`plan` shows what a tick would do) |
| `scripts/ws_state.py` | per-workspace state cards (`~/.greenlight/state/<ws>.json`) carried into every dispatch and failover, and the top-K retrieval the chat uses instead of the whole board |
| `scripts/blocking.py` | opt-in: what you're blocking under a team's GitLab review rules |
| `scripts/harness.py` | the agent CLIs |
| `scripts/diag.py` | the diagnostic trail: structured, hashed events and the `summary`/`errors`/`tail` queries (see `AGENTS.md`) |
| `scripts/update.py` | versioned backend installs for over-the-air updates: `install`, a locked `swap` with a smoke test, `rollback`, `adopt` (see `docs/UPDATES.md`) |
| `scripts/release.py` | builds and signs releases for the `stable` and `main` channels: version, backend tarball, notes, the signed feed (see `docs/RELEASING.md`) |
| `scripts/decide.py` | System One: quick calibrated decisions (Jev, Haiku, or none) |
| `scripts/ade_*.py` | the cmux, herdr and T3 adapters (Orca is native) |
| `scripts/dash.py`, `scripts/chat.py` | the menubar app's backends |
| `scripts/sync.sh` | copies the checkout to a remote host |
| `scripts/dev.py` | dev copies: the app per worktree, live on your Mac |
| `app/` | the macOS menubar app (Swift + `dashboard.html`, `terminal.html`) |
| `lifecycles/`, `prompts/` | bundled defaults; yours in `~/.greenlight/` override them |
| `examples/` | a sample lifecycle |
| `brand/` | names, colors, icons |
| `tests/` | Python and Playwright tests |

## Dev copies

Working on the app itself: every worktree of this repo gets its own `~/Applications/greenlight-<workspace>.app`, kept live as the worktree changes. Nothing to run or relaunch after the one install:

```bash
# on the Mac, once. <host>: where the worktrees are (an ssh host, or "local"); <repo>: a checkout of this repo there
ssh <host> cat <repo>/scripts/dev.py | python3 - install <host> <repo>
```

- **Pages** (`dashboard.html`, `terminal.html`, their scripts) reload in the running copy about half a second after a save.
- **Swift** and `build.sh` changes rebuild the copy (unoptimized, ~15s) and relaunch it if it was open. A build that fails leaves the copy running as it was.
- A copy **opens by itself once**, the first time its branch changes the app. After that it's yours to open and quit. Delete the worktree and its copy goes too.
- A copy is its own app (bundle id `dev.greenlight.app.<workspace>`): its settings start as a copy of Greenlight's, it runs the **worktree's Python** against the real `~/.greenlight`, it has its own terminal tabs, and it posts no banners (Greenlight already does).
- `python3 scripts/dev.py log --wait`, in a worktree: what the Mac made of the sources as they are now, compiler errors included. That's how an agent on a Linux host checks its Swift.
- **Firewalls** (Little Snitch): a copy's ssh rides one tunnel's socket, so the only process that connects is launchd's `/usr/bin/ssh`, once, whatever the workspace and however often it's rebuilt. Not covered: a copy's shell on a host other than the one Greenlight runs on, and what you run in a Local pane.
- A branch from before dev copies gets none until it merges `main` (its `build.sh` would build over Greenlight.app).

`python3 ~/.greenlight-dev/dev.py status` shows the agents and each copy, `… uninstall` removes all of it, and `~/.greenlight-dev/log` is the log.

## Tests

`make test` runs all of these plus a Swift typecheck; `make help` lists the other targets (build, report, sync, dev-status).

```bash
for t in tests/test_*.py; do python3 "$t" || echo "FAIL $t"; done   # test_chat.py needs pytest: uv run --with pytest python -m pytest tests/test_chat.py
node tests/test_dashboard.cjs; node tests/test_terminal.cjs; node tests/test_stage.cjs       # need playwright-core and Chrome
```

## Contributing & license

Greenlight is early and moves fast. See [CONTRIBUTING.md](CONTRIBUTING.md), and report security problems privately per [SECURITY.md](SECURITY.md). MIT licensed, see [LICENSE](LICENSE).
