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
| ade-cmux-short | `ade worktree` (no verb) crashed with "not enough values to unpack (expected 3, got 2)" | `Error: ade: need a noun and a verb (e.g. worktree list), got ['worktree']` (all three adapters) | core.md ade_cmux §1 |
| ade-t3-create-badout | unexpected `t3 project add` output crashed with IndexError "list index out of range" | `Error: t3 project add: can't find the project id in its output '<output>'` | core.md ade_t3 §7 |
| ade-t3-launch-nomodel | an agent with no T3 model crashed with KeyError `'pi'` | `Error: <T3 home>/userdata/model-manifest.json has no current model for provider 'pi'` | core.md ade_t3 §2 |
| ade-t3-nodb | a missing T3 state db crashed with sqlite's "unable to open database file" | `Error: no T3 Code state db at <path>/statev2.sqlite (is T3 Code installed? T3CODE_HOME overrides ~/.t3)` | core.md ade_t3 §2 |
