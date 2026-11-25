# Greenlight: notes & quality standards for coding agents

Read by Codex, Antigravity (`agy`), and other `AGENTS.md` harnesses; Claude Code reads it through `CLAUDE.md`, Gemini CLI through `GEMINI.md`.
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

Use `uv run --no-project python` in place of `python3` when uv is there. On a remote host (the app's `GREENLIGHT_HOST`), run these on
the host: the checkout is `~/.claude/skills/greenlight`.

**Where it lives**
- Backend: `$GREENLIGHT_HOME/diag/events.jsonl` (default `~/.greenlight`), rotated at 5 MB to `.1`–`.3`.
- The Mac app: `~/Library/Logs/Greenlight/app.jsonl`. `diag.py` merges it in when the app runs on the same machine.
- The app writes `ssh_exit`, `action`, `floor`, `voice`, `stage`, `app_start`, `js_error` and `prev_crash` (a launch after a crash or a kill),
  `hang {secs}` (the main thread didn't answer for over 2 s) and, every 5 min, `sample {rss_mb, cpu_pct, up_mins}`.
- `summary.crashes`: the app's macOS crash reports (`~/Library/Logs/DiagnosticReports/Greenlight*.ips`), exception,
  signal and the crashed thread's frames as `image:symbol`. Only when `diag.py` runs on the Mac (not with a remote host).
- `summary.stage`: the voice stage's draws by who asked (`chief`, `fast` for the Haiku card, `dwim`, `app`) and by draw
  (`wait`, `text`, `spec`, `chart`, `html`), and per turn p50/p95 seconds to the first card (not a wait card) and to the last draw.
  Each `stage` event carries `draw`, `by`, `card` (an allowlisted kind or `other`), `turn`, `n` (its place in the turn) and `at`.
- `summary.usage`: chat turns and automation runs per `harness.model`, with tokens in and out and cost. `chat_turn` and
  `automation` events (and `auto/runs.jsonl`) carry `in_tokens`, `out_tokens` and `cost_cents`; only Claude reports a cost.
  `in_tokens` counts cache reads and sums every request of the turn, so it runs far above `context` (one request's size).
  A resumed Codex turn is its thread's total minus the one in its rollout before the turn; none if that's unreadable.

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
  `decisions.jsonl`, `history/`, `pane-logs/`, `auto/*.log`, `report.html`) into a bug report, an issue or a commit.
- `~/.greenlight/leak.re` lists the user's private terms: nothing in the repo may match it (`tests/test_release.py`).

## Required Verification Gates (Before Commit / PR)

Run the local verification suite before considering any change done:

```bash
make lint        # ruff (scripts/, tests/, app/kokoro.py), check_safety.py (incl. JS syntax, token drift), tokens.py check, icons.py check, shellcheck
make test-py     # all Python test suites (auto-routes script tests and pytest tests)
make test-js     # Playwright Chromium UI & security tests (add `make test-webkit` for WKWebView parity)
make typecheck   # macOS: Swift typecheck with -warnings-as-errors -strict-concurrency=targeted
make check-diff  # diff-scoped quality ratchet (stricter ruff, mypy, function size, Swift evalJS)
make coverage    # Python tests with combined coverage.xml (PR CI enforces >= 70% diff coverage)
```

- **Enable local git hooks once per clone:** `make setup-hooks` (activates `.githooks/pre-commit` and `.githooks/pre-push`). Never pass `--no-verify` to bypass hooks.
- **Automated agent hooks:** `.agents/hooks.json` (Antigravity) and `.claude/settings.json` (Claude Code) run `scripts/agent_hook.py` automatically to catch lint/safety regressions and block `--no-verify`.
- **UI / `app/` changes:** Also run `make test-ui` (isolated background `--uitest` instances) and visually inspect screenshots via `python3 scripts/uitest.py`. For OS-level HID checks (`⌘V` paste, Finder drag-drop, notifications), use `scripts/vm_test.sh`.
- **Pre-PR review gate:** Run `python3 scripts/audit.py review --path . --target main` before opening or merging a PR.

## Working on the code & Security Hygiene

- **Python stdlib only (`scripts/`):** New scripts call `diag.install("<name>")` in `__main__` and wrap slow/failing work in `diag.span(...)`. Tests set `GREENLIGHT_HOME` to a tempdir before importing: never touch the real `~/.greenlight`.
- **Swift ↔ JS bridge:** Never use raw string interpolation inside `evaluateJavaScript(...)`. Always use `WKWebView.callJS(_:_:)` (`app/Sources/Config.swift`), or document genuinely raw expressions with `// safety-ok`.
- **Swift preferences & shell paths:** Always use `ud` instead of `UserDefaults.standard` (so `--uitest` stays isolated), `shellPath(_:)` for paths that may start with `~`, and `shellQuote(_:)` for arbitrary arguments.
- **Python subprocesses:** Use argv lists; never add `shell=True` without owner-config justification and `# noqa: S602`.
- **HTML/CSP:** Keep `<meta http-equiv="Content-Security-Policy">` intact on `app/dashboard.html`, `app/terminal.html`, and `app/stage.html`. Never add inline event handlers (`onclick=`, etc.), and keep `stageWeb` free of `WKScriptMessageHandler`.
- **Modularity & tests:** Keep newly added Python functions in `scripts/` at or under **100 lines** and low complexity (`C901 <= 15`, enforced by `make check-diff`). Use `with open(...)` (`SIM115`), never leave `breakpoint()`/`pdb` (`T10`), and add tests in `tests/test_<module>.py` (script or `pytest` style — `scripts/run_py_tests.py` runs both and fails on empty files). Never weaken or skip existing assertions.
- **Shell scripts:** Use `set -euo pipefail`, quote all expansions, and pass `shellcheck -S warning`.
- **Visual design:** Always design in Greenlight's own visual language, defined in [`brand/DESIGN.md`](brand/DESIGN.md): its tokens (`--ds-*` in the pages, `DS` in Swift), its component recipes (§10) and its do/don't list (§12). A UI change is done only when it passes the acceptance gate in §13: screenshots in light and dark, every state, and a fresh-eyes critique. `brand/README.md` is the short version.
- **Design tokens:** `brand/DESIGN.md`'s front matter is the token source. Never edit the generated `<style id="ds-tokens">` blocks or `app/Sources/Tokens.swift` by hand; change the spec and run `python3 scripts/tokens.py generate`. New UI code uses `--ds-*` / `DS` tokens, and `check_safety.py`'s drift baseline (`scripts/drift_baseline.json`) only goes down.
- **Icons:** one set, `brand/icons/ui/*.svg` (16 grid, 1.5 stroke, round joins, `currentColor`; `-fill` names are the filled variants) plus the agents' marks in `brand/icons/marks/`. Use `dsIcon(name, px)` / `dsIconHTML` in the pages and `Image(ds: name, size:)` in Swift (`iconSize(pt)` for a size beside text); never inline an `<svg>` path or an SF Symbol in a custom view. Add or change an SVG, then run `python3 scripts/icons.py generate`; `icons.py sheet out.html` draws a contact sheet.
