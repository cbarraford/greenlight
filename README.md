# Greenlight

An agent manager for coding-agent workspaces. Every workspace gets a **goal** ("Merge !42", "Review #17", or free-form). Greenlight walks each one through its repo's MR/PR lifecycle, works out the next step and who owns it, does the mechanical steps itself (mark ready, set a milestone, assign a review bot, merge), dispatches an agent to anything idle, cleans up finished workspaces, and only asks you what only you can answer.

It ships as a Claude Code skill (`SKILL.md` + stdlib Python), and **Greenlight**, a macOS menubar app with a dashboard, a chat with Greenlight, and replyable notifications. Greenlight opens in **the Pipe**: the terminal alone, dark. Its level pill, which names where it goes (the Pipe in dark copper, the Basement in amber), or ⌘B switches to **the Basement**, the same window with its sidebar, where the green light at the bottom right rides up to **Greenlight Center** (the dashboard and chat, light), whose terminal button rides back down. ⌘⇧G goes up, ⌘⇧T down.

**Verbose activity** (Settings › Chat & voice, off by default) adds a live log above the chat's message box, and its newest lines under the live line in voice mode: Chief's every command in full with its output and thinking, each workspace agent's state, tool and last words, and every move Greenlight makes (a workspace's lifecycle step moving, the dashboard's Merge, Park, Run and Answer buttons). Off, the chat is the concise view and `chat.py` streams what it always has.

## Requirements

- Python 3.10+ (stdlib only).
- `glab` (GitLab) and/or `gh` (GitHub), authenticated.
- At least one coding agent CLI: `claude`, `codex`, `opencode` or `pi`.
- One workspace manager (ADE): Orca (default), cmux, herdr, or T3 Code. Pick it with `GREENLIGHT_ADE=orca|cmux|herdr|t3`.
- The menubar app: macOS with the Command Line Tools (`swiftc`).

## Install

```bash
git clone https://github.com/cbarraford/greenlight ~/.claude/skills/greenlight   # Claude Code's personal-skill path
cd ~/.claude/skills/greenlight
python3 scripts/greenlight.py report   # first scan: writes ~/.greenlight/report.html
python3 scripts/auto.py install        # the scheduler (systemd user timer, or launchd on macOS)
app/build.sh                           # optional: ~/Applications/Greenlight.app
```

Then run `/greenlight` in Claude Code, or add a scheduled run with `auto.py add` (see `SKILL.md`).

Greenlight can run on another machine (a dev box that's always on): put the checkout there, and pick it in the app's **Settings › Hosts** (any `Host` in `~/.ssh/config`; **Test** checks it, then relaunch), or set `GREENLIGHT_HOST`. `scripts/sync.sh` copies a local checkout to that host.

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
| `require_human_approval` | `true` | `false`: a mergeable MR with no other human's approval merges anyway |
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
| opencode | permission asks are rejected | `--auto` |
| pi | **refuses to run** | no flags (pi has no permission gate or sandbox) |

Unattended work (automations, the menubar chat) runs shell commands, so in safe mode some tasks stop at a refused permission. Only set `bypass` on a machine you're comfortable letting an agent drive.

Support differs by harness:
- All four: dispatch, automations, the menubar chat, limit fallback, cmux launch.
- Claude and codex only: the dashboard's transcript mirror and question picker, herdr's `--kind`, T3 providers.
- Claude only: Greenlight tells an idle agent from one with an unsent draft by reading Claude Code's TUI (`ade_common.py`), and only Claude conversations can be compacted from the chat.

## Layout

| path | what |
|---|---|
| `SKILL.md` | the skill: how Claude Code runs Greenlight |
| `scripts/greenlight.py` | scan, next step per workspace, report, pings |
| `scripts/adopt.py` | a workspace for each open MR/PR of yours or awaiting your review |
| `scripts/lifestage.py` | where each change is in its lifecycle (stage and sub-stage), who holds it, the suggested move |
| `scripts/cleanup.py` | deletes finished workspaces when nothing can be lost |
| `scripts/decisions.py` | the decision record: every question Greenlight asked you, the situation, and your answer (`~/.greenlight/decisions.jsonl`) |
| `scripts/auto.py` | Greenlight's own cron-style automations |
| `scripts/blocking.py` | opt-in: what you're blocking under a team's GitLab review rules |
| `scripts/harness.py` | the agent CLIs |
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

```bash
for t in tests/test_*.py; do python3 "$t" || echo "FAIL $t"; done   # test_chat.py needs pytest: uv run --with pytest python -m pytest tests/test_chat.py
node tests/test_dashboard.cjs; node tests/test_terminal.cjs; node tests/test_stage.cjs       # need playwright-core and Chrome
```
