Verify {mr} ({url}), "{title}", {branch} → {target} at {sha}. Goal: {goal}.
You are an independent verifier with fresh eyes — do not trust prior claims that this works. Don't push, post, resolve, approve or merge until told.

Repo standing rules:
{rules}

Diff-triggered reliability checklist (run every item whose trigger matches the diff):
{checklist}

1. Scope and triggers.
   - `git fetch`; confirm this checkout is at {sha} with a clean tree (stop and say so if not).
   - Read the goal/description and `git diff origin/{target}...{sha}`.
   - Match the changed files and behaviors against the reliability checklist above; every matched trigger is mandatory.

2. Setup (environment and state).
   - Boot the real app, service, or CLI cleanly using the repo's verification scripts or shortcuts.
   - If a required tool, browser engine, or integration identity is missing, note the exact gap rather than silently downgrading to a unit test.

3. Drive (at the real user surface).
   - Exercise every changed flow and matched checklist trigger at the exact surface a real user interacts with (UI clicks/keyboard/drag in the real browser or app, real CLI invocations, real API calls).
   - Never cheat around the surface (e.g. calling internal JS helpers or mocks instead of driving real controls).
   - Probe 2–3 edge or error paths (empty input, cancel/close, reload/restore, invalid input).

4. Check (audit and side effects).
   - Confirm actual end state: rendered output, persisted files/DB, exit codes, and zero console/page/runtime errors.
   - Audit your own run: verify no step bypassed the real user surface.
   - Flag behavioral or visual side-effect defects even when the happy path passes (e.g. stale dialog state after cancel, broken focus, layout glitches).

5. Report, evidence-first:
   - **Verdict**: `pass` or `fail`, and why in one line.
   - **Flows & triggered checks**: each flow/trigger run at the real surface, with concrete evidence (commands, outputs, screenshot or recording paths).
   - **Defects found**: blocker / major / minor, exact repro steps, observed vs. expected (include side-effect defects).
   - **Could not prove**: anything you could not verify end-to-end and why. Never claim verified for unproven behavior.
   - **Checklist suggestion**: if a bug or near-miss exposed a verification gap, propose one diff-triggered line for the repo's `## Checklist`.

When authorized to record the verdict (or after I confirm), record it against this commit so Greenlight advances the `verify` stage (or loops defects back to the builder):
`python3 ~/.claude/skills/greenlight/scripts/greenlight.py verify {project} {iid} <pass|fail|uncertain> --sha {sha} --summary "<one-line verdict>" [--evidence <path-to-html-or-artifact>] [--defect "<defect>"] [--unproven "<gap>"]`
