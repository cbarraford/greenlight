# TODO

Basement parity gaps vs Orca, T3 Code and herdr that come after items 1–9 (new workspace from the sidebar, agent alerts, drag-drop, setup hooks, diff, ports, CLI adapter, saved commands, open in editor).

- **Phone/web access.** You can already `ssh` in and run `tmux -L greenlight attach`. A real version would serve terminal.html read-only over Tailscale. Orca and T3 both ship mobile apps.
- **Usage and rate-limit view.** Show per-agent token spend and how much of the 5-hour window is left, next to the floor pill.
- **Transcript search.** Full-text search over the agent transcripts (`~/.claude/projects/*.jsonl`) from ⌘J.
- **Larger features, deferred until someone asks:**
  - plugins
  - orchestration and fan-out (one goal split into N worktrees)
  - Linear/Jira intake
  - embedded browser
  - emulator panel
  - computer use
- **Snooze/archive button (optional).** The `(parked)`/`(revisit)` goal tags already cover this. Add a button only if typing the tag turns out to be friction.
