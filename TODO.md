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

Found while running items 1–9 live (2026-11-13):

- **Alerts in restored tabs.** A background tab restored at launch doesn't attach until you open it, so its "Claude finished" ping can't fire until then. Fix: attach every agent tab at launch, or have the backend's `state()` drive the ping instead of the page's screen scan.
- **Dock badge.** The bell and the macOS notification both fired, but no badge showed on the Dock icon. Check the Notifications → Greenlight → Badges setting before treating it as a bug.
- **Basement worktree noise.** The Basement lists every `git worktree` of every repo, including Claude Code's own `.claude/worktrees/agent-…`. Hide paths under `.claude/worktrees/`.
- **Not yet clicked through live:** ports/tunnel, pasting a screenshot, file drop, ⌘⇧P Commands, Open in editor, and the Diff dialog with notes. They have unit tests only.
