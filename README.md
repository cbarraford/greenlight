# Greenlight

An agent manager for coding-agent workspaces. Every workspace gets a **goal** ("Merge !42", "Review #17", or free-form). Greenlight walks each one through its repo's MR/PR lifecycle, works out the next step and who owns it, does the mechanical steps itself (mark ready, set a milestone, assign a review bot, merge), dispatches an agent to anything idle, cleans up finished workspaces, and only asks you what only you can answer.

It ships as a Claude Code skill (`SKILL.md` + one Rust binary, `bin/greenlight`), and **Greenlight**, a macOS menubar app with a dashboard, a chat with Greenlight, and replyable notifications. Greenlight opens in **the Pipe**: the terminal alone, dark. Its level pill, which names where it goes (the Pipe in dark copper, the Basement in amber), or ⌘B switches to **the Basement**, the same window with its sidebar, where the green light at the bottom right rides up to **Greenlight Center** (the dashboard and chat, light), whose terminal button rides back down. ⌘⇧G goes up, ⌘⇧T down.

## Requirements

- Rust (`cargo`, via [rustup](https://rustup.rs)): `bin/greenlight` builds the binary on first run and after every `git pull`.
- `glab` (GitLab) and/or `gh` (GitHub), authenticated.
- At least one coding agent CLI: `claude`, `codex`, `opencode` or `pi`.
- One workspace manager (ADE): Orca (default), cmux, herdr, or T3 Code. Pick it with `GREENLIGHT_ADE=orca|cmux|herdr|t3`.
- The menubar app: macOS with the Command Line Tools (`swiftc`).

## Install

```bash
git clone https://github.com/cbarraford/greenlight ~/.claude/skills/greenlight   # Claude Code's personal-skill path
cd ~/.claude/skills/greenlight
bin/greenlight report                  # first run builds the binary, then scans: writes ~/.greenlight/report.html
bin/greenlight auto install            # the scheduler (systemd user timer, or launchd on macOS)
app/build.sh                           # optional: ~/Applications/Greenlight.app
```

Then run `/greenlight` in Claude Code, or add a scheduled run with `greenlight auto add` (see `SKILL.md`).

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
| `adopt` | `{"cap": 10, "roles": ["author", "reviewer"]}` | workspaces `greenlight adopt` creates per run, and for which of your MRs |
| `blocking` | `null` | GitLab only: a team roster for `greenlight blocking` (see `src/blocking.rs`); `null` turns it off |
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

`src/harness.rs` builds every headless agent command. Each harness runs with safe permissions unless you opt into bypass:

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
- Claude only: Greenlight tells an idle agent from one with an unsent draft by reading Claude Code's TUI (`src/ade_common.rs`), and only Claude conversations can be compacted from the chat.

## Layout

| path | what |
|---|---|
| `SKILL.md` | the skill: how Claude Code runs Greenlight |
| `bin/greenlight` | the entry point: rebuilds the binary when the source changed, then runs `greenlight <command>` |
| `src/greenlight.rs` | scan, next step per workspace, report, pings (`greenlight scan`, `report`, `ping`, …) |
| `src/adopt.rs` | `greenlight adopt`: a workspace for each open MR/PR of yours or awaiting your review |
| `src/cleanup.rs` | `greenlight cleanup`: deletes finished workspaces when nothing can be lost |
| `src/auto.rs` | `greenlight auto`: Greenlight's own cron-style automations |
| `src/blocking.rs` | `greenlight blocking`, opt-in: what you're blocking under a team's GitLab review rules |
| `src/harness.rs` | the agent CLIs |
| `src/ade_*.rs` | the cmux, herdr and T3 adapters (Orca is native) |
| `src/dash.rs`, `src/chat.rs` | `greenlight dash` / `chat`: the menubar app's backends |
| `src/py.rs`, `src/base.rs` | shared helpers (Python-compatible JSON/text formatting, config, CLI wrappers) |
| `scripts/sync.sh` | copies the checkout to a remote host |
| `app/` | the macOS menubar app (Swift + `dashboard.html`, `terminal.html`) |
| `lifecycles/`, `prompts/` | bundled defaults; yours in `~/.greenlight/` override them |
| `examples/` | a sample lifecycle |
| `brand/` | names, colors, icons |
| `tests/` | Playwright tests for the app's pages |
| `migration/` | the black-box harness: goldens recorded from the old Python, fake CLIs, `BUGS.md` (deliberate fixes) |

## Tests

```bash
cargo test && cargo build && GREENLIGHT_BIN=target/debug/greenlight migration/harness.sh check   # unit tests, then every golden
node tests/test_dashboard.cjs; node tests/test_terminal.cjs          # need playwright-core and Chrome
```
