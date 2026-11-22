# Greenlight: notes for coding agents

Read by Codex, agy and other `AGENTS.md` harnesses; Claude Code reads it through `CLAUDE.md`, Gemini CLI through `GEMINI.md`.
The user-facing docs are `README.md` (setup, layout, tests) and `SKILL.md` (how the skill runs).

## Troubleshooting: read the diagnostic trail first

When something misbehaves (a failed scan, an automation that keeps failing, a slow or crashing app), query the trail
before reading code. It's JSON on stdout, built for you:

```bash
python3 scripts/diag.py summary              # version, env, config shape, error counts, top errors, p50/p95 per span, automations
python3 scripts/diag.py errors --since 24h   # failures grouped by component, kind, exception type, class and code location
python3 scripts/diag.py tail -n 50 [--kind cmd] [--since 1h] [--run ID]   # raw events, oldest first
python3 scripts/diag.py doctor               # setup checks: glab/gh auth, agent CLIs, disk, automations timer, dead pid files
```

Use `uv run python` in place of `python3` when uv is there. On a remote host (the app's `GREENLIGHT_HOST`), run these on
the host: the checkout is `~/.claude/skills/greenlight`.

**Where it lives**
- Backend: `$GREENLIGHT_HOME/diag/events.jsonl` (default `~/.greenlight`), rotated at 5 MB to `.1`–`.3`.
- The Mac app: `~/Library/Logs/Greenlight/app.jsonl`. `diag.py` merges it in when the app runs on the same machine.
- The app writes `ssh_exit`, `action`, `floor`, `voice`, `app_start`, `js_error` and `prev_crash` (a launch after a crash or a kill),
  `hang {secs}` (the main thread didn't answer for over 2 s) and, every 5 min, `sample {rss_mb, cpu_pct, up_mins}`.
- `summary.crashes`: the app's macOS crash reports (`~/Library/Logs/DiagnosticReports/Greenlight*.ips`), exception,
  signal and the crashed thread's frames as `image:symbol`. Only when `diag.py` runs on the Mac (not with a remote host).

**How to read it**
- Every event has `ts`, `v` (git short SHA), `comp` (the script, or `app`), `run`, `pid` and `kind`.
- `run` is a correlation id: one tick's `auto` → `orchestrate` → `adopt` / `greenlight` / `cleanup` share it. `tail --run ID` follows one.
- Spans (`cmd`, `scan`, `audit`, `chat_turn`, `decide`) carry `secs` and either `ok: true` or `err` (exception type),
  `msg_class` (`auth`, `rate_limit`, `timeout`, `network`, `not_found`, …) and `where` (`file.py:line:func`, our frames only).
- Point events: `script` and `phase` (orchestrator steps), `orch` / `adopt` / `cleanup` (what a tick did), `automation`,
  `ping`, `dash_error`, and `uncaught` (a script's crash, with `err` and `where`).
- `cmd` is every external command (glab, gh, orca, …) made through `greenlight.run()`: `tool`, `sub`, `rc`, `tries`.
- Names are salted hashes (`ws_h`, `mr_h`, `name_h`): stable within one install, meaningless outside it. Ask the user
  which workspace or MR they mean; don't try to reverse them.

**Bug reports**: `diag.py bundle` (a file) and `diag.py report --title T --body-file F` (a preview; `--execute` files it on
cbarraford/greenlight). Both re-check every string with `audit.py`'s patterns and `leak.re` and fail closed. Never file
without the user's yes, and never attach anything but a bundle.

## Privacy rules (the trail may be shared publicly)

- Never add free text to an event: no messages, argv, stderr, titles, goals, branch or workspace names, paths or hosts.
  `diag.event()` drops non-token strings and counts them in `dropped`; don't work around it.
- A new string field needs its key in `diag.KEYS` and must hold an enum-like token. Identifiers go in `<x>_h` via `diag.h()`.
- Never copy `~/.greenlight/diag/salt` anywhere, and never put the private journals (`log.jsonl`, `outbox.jsonl`,
  `decisions.jsonl`, `history/`, `auto/*.log`, `report.html`) into a bug report, an issue or a commit.
- `~/.greenlight/leak.re` lists the user's private terms: nothing in the repo may match it (`tests/test_release.py`).

## Working on the code

- Python is stdlib only (`scripts/`); tests are scripts: `make test-py` (what CI runs), `make test` (adds Playwright and the
  Swift typecheck). Tests set `GREENLIGHT_HOME` to a tempdir before importing: never touch the real `~/.greenlight`.
- New scripts call `diag.install("<name>")` in `__main__`, and wrap their slow or failing work in `diag.span(...)`.
