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
- **Clicked through live, but not by a real hand:** ports/tunnel, screenshot paste, file drop, ⌘⇧P Commands, Open in editor and the Diff dialog with notes worked in the real app, driven through the page's JS. Not proven: a real ⌘V of a screenshot (tried three times through Edit › Paste, and no paste event reached the page; the cause wasn't found, possibly the background test app never took focus), a real Finder drag, and Open in editor past VS Code's confirm dialog (Remote - SSH isn't installed here). Only Diff (a page test) and the ports parsers (`test_dash.py`) have tests; paste, drop, Commands and Open in editor have none.

## Ideas from the 2026-11-19 session

Every prompt from tonight, in order. ✅ shipped on `lifecycle-advice` ([#18](https://github.com/cbarraford/greenlight/pull/18)), 🚧 in progress, ⬜ open or worth revisiting.

### Lifecycle and the hex popover
- ✅ Each workspace tracks where it is in the dev lifecycle; when we're the blocker, recommend what to do next, shown in the hex popover
- ✅ The popover shows progress through the lifecycle to completion
- ✅ The new element in the other popover too (screenshot: it was missing)
- ✅ A Merge button when the MR is mergeable
- ✅ Same for Undraft
- ✅ A Deploy button when deploying is an option
- ⬜ Check the Deploy button against a real deployable workspace

### Chief, aware and proactive
- ✅ Chief knows each workspace's lifecycle stage, so you can have a full conversation and it can say which workspace to work on next
- ✅ Chief checks in when you've done nothing for 30 minutes and suggests work (Settings → Idle check-in)
- ✅ Chief can refresh the workspaces when it wants to
- ✅ Chief watches workspace status and can ask you a question
- ✅ Chief can fetch GitHub/GitLab issues and MRs as a tool, and suggest what to address from their status
- ⬜ Watch it for a few days: are the check-ins and questions useful or noisy?

### Voice (live chat)
- ✅ Free, continuous two-way speech with Chief, like Claude's and ChatGPT's voice modes (researched open-source and paid options)
- ⬜ Revisit that research: is there an open-source or realtime speech-to-speech option worth switching to?
- ✅ A live, accurate speech waveform
- ✅ Chief can draw and visualize anything it's saying, and something is always on screen ("one moment please" or loading)
- ✅ Drawing is fast and cheap: an HTML stage, plus charts from a few lines of JSON instead of hand-drawn SVG (your graph screenshot)
- ⬜ More cheap "draw" tools in the same vein: tables, timelines, diffs
- ✅ Captions on/off
- ✅ Chief can end voice mode itself; "goodbye" and similar close it and go back to the text chat
- ✅ The live chat shows the context, with a Clear button, and you can ask Chief to reset
- ~~Full-size stage, "show me" / "step aside", Esc to go back~~: built, then removed at your request
- ✅ 7 live chat styles (Conversation, Orb, On Air, Phosphor, Editorial, HUD, Zen); everything but the old Greenlight style ([designs](https://claude.ai/artifact/GiSsZiMe6B6WfPFUedVVNX))
- ✅ A small button in the live chat header that cycles through the styles (not in Settings)

### Hexes (highlighting)
- ✅ Chief can light one or more hexes
- ✅ A local decision model picks the hexes when Chief doesn't name any
- ✅ Chief lights hexes at the end of every reply, on request ("hexes", "spaces")
- ✅ "Highlight…" / "show me…" is a yes: no confirmation question first
- ✅ Lighting a single hex also opens its popover
- ✅ One Chief across every window: the same lit hexes in each, no duplicates
- ✅ Chief knows which window is focused, and redraws the stage for the workspace you switch to
- ✅ Chief's context holds only the focused window's workspaces
