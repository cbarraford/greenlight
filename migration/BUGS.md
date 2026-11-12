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
| gl-scan-gitlab, gl-scan-text, gl-report | a `closed_by` reply that isn't JSON made the row read `greenlight couldn't evaluate: Expecting value: line 1 column 1 (char 0)` | `greenlight couldn't evaluate: glab api <path>: not JSON (<parse error>)` names the call (same for the other single `glab api` reads in mr_details/by_branch) | core.md §7 |
| gl-last-corrupt | a corrupt `last.json` crashed the scan with `Expecting value: line 1 column 1 (char 0)` | `Error: <path>/last.json: not JSON (<parse error>)` names the file (still fatal, as in Python) | core.md §7 |
