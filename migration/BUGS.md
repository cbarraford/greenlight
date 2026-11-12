# Behavior changes ledger

Goldens in `scenarios/*/expected/result` are recorded from the Python scripts. The Rust port must
match them, with one exception: a bug fixed on purpose. Each fix updates that scenario's golden in
the same commit and adds a line here, so every diff from Python is visible and reviewable.

Candidates are listed under "Suspected bugs" in `inventory/core.md` §7, `inventory/backends.md` §8
and `inventory/lifecycle.md` §7. Goldens that pin Python-only crash text (KeyError, NoneType,
JSONDecodeError, sqlite errors, `sleep length must be non-negative`) are listed there too. Rust
replaces those crashes with a clean error.

| scenario(s) | was (Python) | now (Rust) | ref |
|---|---|---|---|
| gl-config-corrupt | a corrupt `config.json` crashed with JSONDecodeError text only | `Error: <path>/config.json: <parse error>` names the file | core.md §7 |
| auto-add-bad-cron-fields, auto-add-bad-cron-word, auto-edit-bad-cron | a cron without 5 fields crashed with `not enough values to unpack (expected 5, got N)` | `Error: bad schedule '<cron>': needs 5 fields (minute hour day month weekday), got N` | lifecycle.md §5 cron |
| auto-add-bad-cron-step0 | step 0 crashed with `range() arg 3 must not be zero` | `Error: bad schedule '<cron>': step 0 in '<part>'` | lifecycle.md §5 cron |
| auto-tick-bad-schedule | a non-numeric cron token crashed with `invalid literal for int() with base 10: 'x'` | `Error: bad schedule '<cron>': 'x' is not a number` (tick still aborts there, as in Python) | lifecycle.md §7.7 |
| auto-exec-missing-name | `auto exec <unknown>` crashed with KeyError `'nope'` | `Error: nope: no such automation` | lifecycle.md §7.5 |
| auto-malformed-json | a corrupt automations.json crashed with JSONDecodeError text only | `Error: <path>/automations.json: <parse error>` names the file | lifecycle.md §3 |
| auto-runs-blank-line | a blank/corrupt runs.jsonl line crashed with JSONDecodeError text only | `Error: <path>/auto/runs.jsonl line N: <parse error>` | lifecycle.md §7.25 |
| (none; output unchanged) | chat send/compact read the agent's stderr only after exit: >64 KB of stderr deadlocked the turn | stderr drained in a thread | backends.md §8.1 |
| ade-cmux-short | `ade worktree` (no verb) crashed with "not enough values to unpack (expected 3, got 2)" | `Error: ade: need a noun and a verb (e.g. worktree list), got ['worktree']` (all three adapters) | core.md ade_cmux §1 |
| ade-t3-create-badout | unexpected `t3 project add` output crashed with IndexError "list index out of range" | `Error: t3 project add: can't find the project id in its output '<output>'` | core.md ade_t3 §7 |
| ade-t3-launch-nomodel | an agent with no T3 model crashed with KeyError `'pi'` | `Error: <T3 home>/userdata/model-manifest.json has no current model for provider 'pi'` | core.md ade_t3 §2 |
| ade-t3-nodb | a missing T3 state db crashed with sqlite's "unable to open database file" | `Error: no T3 Code state db at <path>/statev2.sqlite (is T3 Code installed? T3CODE_HOME overrides ~/.t3)` | core.md ade_t3 §2 |
| blocking-bad-reason | a `blocks.json` reason outside the known five crashed with `list.index(x): x not in list` | `Error: <path>/blocks.json: unknown block reason 'mystery' on x!1` | core.md blocking §1 |
| cleanup-bad-url | an scp-style origin with no `:` after the host (`git@gitlab.com/acme/widgets.git`) crashed the whole scan with `not enough values to unpack (expected 2, got 1)` | `scan failed: r1: can't parse origin URL '<url>' (git@ without host:path)` names the repo and URL | lifecycle.md §7 |
| gl-scan-gitlab, gl-scan-text, gl-report | a `closed_by` reply that isn't JSON made the row read `greenlight couldn't evaluate: Expecting value: line 1 column 1 (char 0)` | `greenlight couldn't evaluate: glab api <path>: not JSON (<parse error>)` names the call (same for the other single `glab api` reads in mr_details/by_branch) | core.md §7 |
| gl-last-corrupt | a corrupt `last.json` crashed the scan with `Expecting value: line 1 column 1 (char 0)` | `Error: <path>/last.json: not JSON (<parse error>)` names the file (still fatal, as in Python) | core.md §7 |
| dash-page-corrupt-cache | a corrupt cached page layout crashed `page` with `Expecting property name enclosed in double quotes: ...` | `Error: <path>/dash/pages/pc.json: not JSON (<parse error>)` names the file | backends.md §8 |
| dash-page-designer-fail | a designer reply that isn't JSON read `designer failed: Expecting property name enclosed in double quotes: ...` | `designer failed: the designer's reply isn't JSON (<parse error>): <reply[:80]>` | backends.md §8 |
| dash-snapshot-toolinput-dict | an Orca agent whose `toolInput` is an object crashed the snapshot with `slice(None, 160, None)` | `Error: orca worktree ps: the agent's toolInput is a dict, not text` | backends.md §8 |
| dash-task-badjson | a `task new` body that isn't JSON read `Expecting property name enclosed in double quotes: ...` | `stdin is not JSON (<parse error>)` | backends.md §8 |
| dash-task-noagents | `task new` with an empty `agents` list read `cannot access local variable 'res' where it is not associated with a value` | `no agents configured (config.json agents is empty)` (the task dir is still left behind, as in Python) | backends.md §8 |
| dash-trees-row-without-next | a `last.json` row without `agent`/`next` crashed `trees` with `'agent'` | `Error: <path>/last.json: the row for <path> has no agent` names the file and row | backends.md §8 |
