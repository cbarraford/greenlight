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
