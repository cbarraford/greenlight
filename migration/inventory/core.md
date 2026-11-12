# Core inventory: greenlight.py + harness + ade_* + blocking

**Found:** the CLI surface is small, but byte-exact parity depends on Python details that Rust doesn't match by default: `json.dumps` formatting/`ensure_ascii`, `round()`/float `str()`, Unicode `splitlines`/`strip`, lookaround regexes, set-iteration order, and import-time side effects (ME lookup, config crash).
**Needs decision:** (a) whether the Rust port reproduces the suspected bugs (§7) or fixes them, since fixing breaks py-vs-rs parity scenarios; (b) the hardcoded `python3 greenlight.py signoff …` command emitted in `next.cmd` (L510); (c) whether `gh api graphql` argv, which embeds `GH_PR_QUERY` verbatim, stays byte-identical. The harness keys fakes on the exact argv, so it has to.
**Unsure:** whether dict-insertion order of `mr`/row keys matters to consumers (dash.py and Swift read them by key; the harness diffs text). This inventory assumes it does. Ordering that depends on hash seed (sets) is listed under Nondeterminism.

Line refs are `scripts/<file>:L`. "py" means current behavior and is the spec unless §7 says otherwise.

---

## 0. Cross-cutting (applies to every script that imports greenlight)

### Import-time side effects (greenlight.py)
| L | What | Port note |
|---|---|---|
| 38-40 | `HERE`, `ROOT=HERE.parent`, `STATE = $GREENLIGHT_HOME or ~/.greenlight` (`or`: empty env → default) | `ROOT` locates `lifecycles/`. The Rust binary needs an equivalent anchor (exe dir? compile-time? env?) |
| 42-64 | `CONFIG = {**DEFAULTS, **json.load(STATE/config.json)}` shallow merge; missing file → DEFAULTS; **invalid JSON or JSON `null`/non-object → traceback in every command**, blocking/harness/ade included | |
| 65 | `BOT = CONFIG["review_bot"]` | |
| 66 | `NOW_MS = time.time()*1000` frozen at import (float) | |
| 122 | `ME = $GREENLIGHT_ME or CONFIG["me"] or whoami()`. **Network call at import** when neither is set: reads `STATE/me.json`, else `glab api user` → `.username`, else `gh api user` → `.login`, timeout 20; writes me.json compact `{"me": …}`; returns `""` if both fail (ME may be empty, no crash) | Every harness scenario needs GREENLIGHT_ME, config `me`, or a fake glab `api user`. Importing `harness`, `blocking`, or `ade_common.default_agent` triggers this too |

### run() — the only subprocess helper in greenlight.py (L86-101)
- `subprocess.run(cmd, capture_output, text=True, timeout)`. Strict UTF-8 decode (invalid bytes → UnicodeDecodeError traceback). Inherits env and stdin. No cwd.
- rc 0 → stdout string. rc≠0 → `RuntimeError(f"{' '.join(cmd)}: {err[:300]}")` with `err = (stderr or stdout).strip()`. The join is a plain space join with no quoting, and the message ends up in `next.action` text.
- Retries only on `TimeoutExpired` or `TRANSIENT_RE` (L80, re.I: `\b50[0234]\b|bad gateway|service unavailable|gateway time-?out|timed? ?out|connection (?:reset|refused)|unexpected EOF|TLS handshake`). Sleeps `2*attempt` seconds (2, 4). The last TimeoutExpired is re-raised as-is: `str()` = "Command '[...]' timed out after N seconds".
- `FileNotFoundError` (missing binary) is not caught and propagates as a traceback, or as an `error` row inside scan's per-row try.

### JSON serialization (port hazard: serde_json is not byte-compatible)
| Style | Separators | Sites |
|---|---|---|
| compact `json.dumps(x)` | `", "` / `": "` (spaces!) | me.json L117, last.json L715, outbox.jsonl L722, pinged.json L730, cmux paste payload |
| `indent=1` | `","` + newline, `": "` | `ade` stdout L872, signoffs.json L878, `scan --json` L902, blocks.json blocking L183, `blocking --json` L208, goals.json ade_common L91 |
- Always `ensure_ascii=True`: `→` → `→`, `…` → `…`, astral chars → surrogate pairs `😀`.
- Floats: `2.0` stays `2.0`, `-0.0` stays `-0.0`, `1e16` → `1e+16`. Python `repr` float rules apply.
- Empty containers with indent print as `[]`/`{}` on one line.
- Key order is insertion order (Rust: `serde_json` with `preserve_order`, or a custom writer).
- A custom Python-compatible serializer is needed (two styles × ensure_ascii).

### Regexes needing lookaround/backrefs (Rust `regex` lacks them; use `fancy-regex` or hand-code)
- `KEEP_DRAFT_RE` L71 `(?<![\w-])(stay|keep|remain)s? (?:a |as )?draft(?![\w-])` re.I
- `HOLD_RE` L72 `(?<![\w-])(?:hold|do ?n[o'’]t merge)(?![\w-])` re.I. Confirmed: "dont merge" is **not** matched (`n[o'’]t` needs a char between n and t), but "don't merge", "do not merge", and "donot merge" are.
- `DIM_RE` ade_common:12 `\x1b\[2m.*?(?=\x1b\[(?:0|22)?m|$)`
- Python `\w`, `\d`, `\s`, and `\b` are Unicode-aware on str, which matches Rust `regex` defaults, but `re.I` case folding differs slightly for exotic chars.

---

## greenlight.py

### 1. CLI surface (`main` L860-906, hand-rolled argv; no argparse)
`cmd = argv[1] if len(argv)>1 else "scan"`

| Subcommand | Args | Behavior / output | rc |
|---|---|---|---|
| `note` | `<ws> <text…>` | appends `- %Y-%m-%d %H:%M <' '.join(text)>\n` (**local time**) to `history_file(ws)` (L848: `STATE/history/<name with / → __>.md`, mkdir parents). No stdout. Empty text → `- … HH:MM \n` (trailing space) | 0 |
| `ping` | `[--key K] [--ws W]… <text…>` | `ping_cli` L762: flags parsed only from the front while `len(args)>1`. Joined text must match `\nOptions: *\S.*\|` else `sys.exit("…")` → stderr, rc1. `--ws` must be in `scanned_names()` (last.json) else sys.exit rc1. `@`-prefixed key pinged within FLAP=24h (`recent_keys` scans outbox.jsonl) → prints `skipped: {key} was pinged in the last 24h`, rc0. A non-`@` key is treated as a workspace name and prepended to ws, **unvalidated**. Writes outbox + pinged.json | 0/1 |
| `resolve` | `<key> [why…]` | why default `"no longer needed"`; outbox event; no stdout | 0 |
| `ade` | `<orca args…>` | strips **every** `"--json"` element (even as a flag value), calls `orca(*rest)`, prints `json.dumps(res, indent=1)` | 0 / traceback |
| `signoff` | `<proj> <iid> <stage>` | signoffs.json `{"proj!iid": sorted(set(old+[stage]))}` indent=1; no stdout | 0 |
| `history` | `<ws>` | prints file content **plus print's newline** (double trailing newline), or `(no history)` | 0 |
| `precheck` | – | `scan(save=False)`; `n = #rows owner∈{greenlight,agent}` + `len(adopt.plan(rows, ts)["create"])`; adopt exception → stderr `adopt plan failed: {e}`, counted as 0; `if n == 0: keep(res)` (saves only when idle!); stdout `{n} actionable workspace(s) or adoption(s)` | 0 if n else 1 |
| `report` | – | `scan()` (saves last.json + close_stale), writes `STATE/report.html`, prints its path | 0 |
| anything else | `[--json]` | `scan(save=True)`. `--json` **anywhere** in argv → indent=1 JSON. Else per row `f"{owner:10} {step:12} {mr:6} {name[:40]:40} {action[:90]}"` (mr = `ref()` string or ""). Python width counts code points, not display width | 0 |
- An unknown subcommand (typo, or `--json` as argv[1]) silently runs a **saving** scan.
- Missing positional args (e.g. `signoff a b`) → IndexError traceback, rc1.
- Slicing (`[:40]`, `[:90]`, `[:300]`, `[-400:]`) is by code point. Rust must slice on char boundaries, not bytes.

### 2. Files (all under STATE unless noted)
| Path | R/W | Format | Lines |
|---|---|---|---|
| `config.json` | R | JSON object | 57-64 |
| `me.json` | R/W | compact `{"me": …}` | 104-121 |
| `last.json` | R/W | compact `{"ts", "prev_ts", "skipped", "gone", "rows"}`; rows carry `changed` `"step/owner → step/owner"`; corrupt → scan crashes (prev diff) | 699-716 |
| `outbox.jsonl` | A | one compact JSON per line (ping/resolve events, ts epoch) | 719-723 |
| `pinged.json` | R/W | compact | 725-731 |
| `signoffs.json` | R/W | indent=1 | 241-245, 875-879 |
| `history/<ws __>.md` | R/A | markdown lines | 848-858 |
| `report.html` | W | HTML | 797-846 |
| `lifecycles/<repo>.md` (STATE) then `ROOT/lifecycles/` | R | markdown, see lifecycle_file | 178-240 |
| `<worktree>/.git` | R via `git config` | – | find_mr |
- `mkdir(exist_ok=True)` **without parents** at L699 and L720: STATE must already exist, or FileNotFoundError.
- Writes are non-atomic (`write_text`), so a concurrent dash.py reader can see a truncated file.

### 3. External invocations
| Fn (L) | argv | timeout / tries |
|---|---|---|
| whoami 104 | `glab api user`, then `gh api user` | 20 / 1 |
| orca_cli 132 | `[exe, *args, ("--environment", $ORCA_ENVIRONMENT)?, "--json"]`; exe = `$ORCA_CLI_COMMAND` or `which("orca-ide")` or `~/.config/orca/linux-orca-cli-shim/orca` **if exists** or `"orca"`. Requires stdout JSON `{"ok": true, "result": …}` else `RuntimeError("orca <args>: <error>")` | 120 / 1 |
| orca 125 | `$GREENLIGHT_ADE` (default `orca`) ≠ orca → `importlib.import_module("ade_"+name).call(*args)`; `GREENLIGHT_ADE=""` → `ade_` ModuleNotFoundError | – |
| gl 144 | `glab api <path>` | 45 / 3 |
| gl_all 148 | `glab api <path>{?|&}per_page=100&page=N`, loops until a page returns <100 | 45 / 3 |
| gh 309 | `gh <args>` | 45 / 3 |
| gh_open_prs 313 | `gh pr list -R <o/r> --state open --limit 500 --json number,headRefName,closingIssuesReferences` | |
| gh_pr 332 | `gh api graphql -F owner=<o> -F name=<r> -F n=<num> -f query=<GH_PR_QUERY L284-304 verbatim>` | |
| release_milestone 246 | glab gl_all `projects/<pid>/milestones?state=active`; gh `gh api repos/<o/r>/milestones?state=open&per_page=100` (due_on[:10] → due_date). Called only if lifecycle mentions milestone; wrapped in try → None (L620-622) | |
| mr_details 260 | glab `projects/<pid>/merge_requests/<iid>`, `/discussions` (gl_all), `/approvals`, `pipelines/<id>/jobs?scope[]=failed` (gl_all), `/blocks` when `merge_request_blocked` | |
| find_mr | `git -C <path> config branch.<b>.merge` (**no timeout**, only if `isdir(path)`); glab `projects/<pid>/issues/<n>/closed_by`; glab `projects/<pid>/merge_requests?<urlencode(source_branch, state=all, order_by=created_at)>`; gh `pr list -R p --head <b> --state all --limit 1 --json number` | |
| scan 581 | `orca worktree ps`, `orca worktree list`, `orca terminal list --limit 10000` (via orca()) | |
| agent_overlay 553 | `orca terminal read --terminal <h> --limit 200` | |
| evaluate→next.cmd (data, not run) | glab `mr merge N -R p --yes [--squash\|--rebase]`; gh `pr merge N -R p --<how>`; review bot: glab `mr update N -R p --assignee +BOT --reviewer +BOT`, gh `pr edit N -R p --add-assignee BOT --add-reviewer BOT`; signoff `["python3", str(HERE/"greenlight.py"), "signoff", proj, iid, step]` | |
- `pid()` L159 = `urllib.parse.quote(project, safe="")` (`/`→`%2F`, space→`%20`, `~` kept).
- `urlencode` uses quote_plus (space→`+`, `/`→`%2F`).

### 4. Env vars
`GREENLIGHT_HOME` (STATE), `GREENLIGHT_ME`, `GREENLIGHT_ADE`, `ORCA_CLI_COMMAND`, `ORCA_ENVIRONMENT`, `HOME` (Path.home), `PATH` (which/subprocess), `TZ` (local-time strftime in note/report/revisit). Everything else is inherited by children (glab/gh token env).

### 5. Nondeterminism
- `NOW_MS`/`time.time()` → idle_h, outbox ts, last.json ts, FLAP window, report timestamp.
- Local time: `note` prefix, `report` "generated" line, REVISIT compare (`time.strftime("%Y-%m-%d")` local vs the goal's date string, **lexicographic** compare).
- **Set iteration order** (hash-seed dependent for str): the per-project listing loop in scan (`for p in {…}`) affects only call order (the harness doesn't care; fakes are keyed by argv). Output is re-sorted. blocking `who` set (see blocking §5).
- `ThreadPoolExecutor(8).map` keeps input order, but **call order to fakes is concurrent**. Fine for keyed fakes, but stateful fakes (call counters) would race.
- `whoami` network at import; me.json caches it.
- Retry sleeps 2/4s in run().

### 6. Python-isms to replicate (verified by probe on 3.15.2)
- `round(x, 1)` is banker's-ish on binary floats: `round(0.25,1)=0.2`, `round(0.35,1)=0.3`. idle_h = `lastOutputAt and round((NOW_MS-x)/3.6e6, 1)`, so it can be `0` (falsy input passes through as int `0`), `None`, a float, or `-0.0` (future timestamps).
- `str(float)` in report (idle_h): `"2.0"`, `"0"` (int), `"-0.0"`. Rust `Display` prints `2`.
- `str.splitlines()` also splits on `\x0b \x0c \x1c \x1d \x1e \x85    `. `str.strip()` strips Unicode whitespace incl. `\x1c-\x1f`, `\xa0`. Rust `lines()`/`trim()` differ.
- `html.escape(s)` (quote=True): `'` → `&#x27;`, `"` → `&quot;`.
- `shlex.join`/`quote`: safe set `[\w@%+=:,./-]` ASCII-only, so non-ASCII gets quoted; `'` → `'"'"'`.
- `str(KeyError('x'))` → `"'x'"` (quoted), visible in error rows `greenlight couldn't evaluate: 'x'`.
- `-F` in `gh api graphql` type-coerces (numeric owner → int). This is a gh behavior, but the argv must stay the same.
- f-string `:10`/`:40` pads by code point; `[:40]` slices by code point.
- `json.loads` accepts `NaN`/`Infinity`, and duplicate keys keep the last.
- `dict.get(k) or default` treats `""`, `0`, `[]` as missing (used throughout for linkedPR, comment, labels).
- Truthiness of `mr["pipeline"]` (`{}` falsy) gates CI steps.
- `lifecycle_file` L178: `STATE/lifecycles/<basename>.md` → `ROOT/lifecycles/<basename>.md` → any project whose basename is `greenlight` gets the bundled `greenlight.md`.
- `STAGE_RE` L189 / `DONE_RE` L190 / Options parsing: `` `foo bar` `` parses to `{"foo": " bar"}` (split on first space, value not stripped).
- `said()` L412 removes the MR title case-insensitively from the goal/comment before HOLD/KEEP_DRAFT matching.
- `thread_line` L401: `f"{path}:{line}".rstrip(":")`, so line `None`/`0` → path only, and a path ending in `:` loses it.
- `ade_common.project_id` keeps userinfo and ssh port (see §7).
- `linked_number` L327 accepts int, str, or `{"number": …}` dict.

### 7. Suspected bugs (don't fix; decide parity)
1. **gl_all L148**: when a page returns a dict (error/object endpoint), `out += dict` appends its **keys**; `len(dict)<100` ends the loop. Silent garbage.
2. **Unknown subcommand → saving scan** (L897+): a typo like `greenlight.py scna` hits the network and overwrites last.json and close_stale-resolves pings.
3. **said() title eating** L412: a title like `"hold"` or `"old"` is removed from the goal text before HOLD_RE, so "hold" in the goal can vanish; partial-word removal ("old" inside "hold") turns "hold" into "h".
4. **ade_common.project_id** L29: `ssh://git@gitlab.com:22/acme/x` → `git:git@gitlab.com/22/acme/x`; `https://user@gitlab.com/…` keeps `user@`, so forge_project then fails to recognize it.
5. **HOLD_RE** doesn't match "dont merge" (and the comment implies it should).
6. **ping_cli**: a non-`@` `--key` is pushed into ws without the `scanned_names` validation that `--ws` gets.
7. **precheck saves only when n==0**: intentional per comment L712 (an actionable precheck mustn't overwrite the snapshot the report diffs against). Not a bug; the port must keep it.
8. **mkdir without parents** L699/L720 → crash on a fresh machine where `~/.greenlight` doesn't exist but `GREENLIGHT_HOME` points deeper.
9. **revisit date compare** uses local date vs a string. A non-zero-padded or malformed date can't match REVISIT_RE anyway, but tz shifts around midnight change the outcome.
10. **corrupt last.json** crashes scan (prev diff isn't guarded) and every subsequent scan until it's deleted.
11. **closed_by lookup** catches only RuntimeError, so a JSON decode error or KeyError escapes into the row's error path.
12. **signoff cmd** hardcodes `python3` + script path; after the port it must point at the Rust binary, which breaks parity by necessity.

### 8. Scenario seeds (from tests; → harness scenarios)
| Test (tests/test_greenlight.py:L) | Seed |
|---|---|
| 1-20 setup | `config.json {"review_bot": …, "me": …}`, `lifecycles/widgets.md` ← `release-train.md` |
| 22-80 evaluate pure | build `scan` scenarios from fake orca ps/list + glab MR JSON per case (draft, hold, keep-draft, conflicts, CI failed/running, threads, approvals, merge) |
| 83-96 ping/close_stale/resolve | `ping --key @x …` twice → second prints "skipped…"; `resolve @x why` → outbox line |
| 98-126 ping CLI | already CLI-shaped: missing Options → rc1 stderr; unknown `--ws` → rc1 |
| 128-145 gl retry | fake glab rc1 stderr "502 Bad Gateway" then success. **Needs a stateful fake** (keyed fakes can't express "fail then succeed"); costs ~6s of real sleep |
| 147-160 precheck | mocks scan+adopt.plan; as a scenario: empty orca → `0 actionable…` rc1 |
| 162-166 forge_project | pure; covered by scan rows with `github:`/`git:` projectIds |
| 168-215 gh_raw graphql | canned GraphQL → fake `gh` keyed on the full graphql argv (query verbatim) |
| 218-244 GitHub evaluate | `scan --json` over a github project |
| 248-286 scan w/ fake orca | WTS/FULL L248-263, OPEN L264-266, fake_pr L267-269, fake_gh L270-271; `report` L285-286 → report.html (normalize timestamp) |
| 311-333 config knobs | config.json variants; gh milestones L330-332 |
| 334-355 custom stages + signoff | `signoff acme/widgets 42 qa` (exists: signoff-add); custom lifecycle md in `home/lifecycles/` |
| new | `note ws hello` + `history ws` (double newline); `history foo/bar` (exists); `ade worktree list --json`; `scna` typo → scan; `--json` first arg |
| new | corrupt `config.json` → traceback rc1 in every command; corrupt last.json → scan traceback |

---

## harness.py (library, no CLI)
1. **CLI**: none. `H` table L17 (claude/codex/opencode/pi), `conf(name)` L25 merges `CONFIG["harnesses"][name]`.
2. **Files**: none directly (CONFIG via greenlight import, which also triggers ME at import).
3. **Invocations**: none run here; builds argv. `binary()` L29: `conf.bin` or `which(name)` or `~/.local/bin/<name>` or `~/.<name>/bin/<name>` (if exists) or bare `name`. `interactive()` L61 uses the **bare name** (inconsistent with binary()).
4. **Env**: HOME, PATH (which).
5. **Nondeterminism**: filesystem probing in binary().
6. **Python-isms**: `argv()` L35. codex effort `xhigh`/`max` → `high`; pi `max` → `xhigh`. For non-system-prompt harnesses `system` is prepended to text unless `sid`. pi without bypass → ValueError.
7. **Bugs**: interactive() ignores configured `bin`.
8. **Seeds**: tests/test_harness.py:8-34 (pure argv table; port as Rust unit tests, not harness scenarios). 36-56 chat.Norm is out of scope.

## ade_common.py (library)
1. **CLI**: none.
2. **Files**: `GOALS = $GREENLIGHT_HOME or ~/.greenlight /goals.json` L9 (evaluated at import, separately from greenlight.STATE); `set_goal` L87 writes indent=1.
3. **Invocations**: `git()` L24 `git -C path …` timeout 30, returns `""` on **any** failure. git_info L35: `rev-parse --path-format=absolute --git-common-dir`, `--git-dir`, `symbolic-ref -q HEAD`, `remote get-url origin`. projectId is `""` not None. worktree_add L45: `git -C repo worktree add -b <name> <parent>/<repo>-<name with / → -> [base]`. worktree_remove L54.
4. **Env**: GREENLIGHT_HOME, HOME.
5. **Nondeterminism**: none beyond git state.
6. **Python-isms**: `flags()` L61: `--a=b` → key a; a value starting with `--` becomes the next flag (so `--text --json` → both True); positional args are ignored; last wins. `draft()` L73 finds the `❯` input box; `MENU_ITEM_RE` `❯\s*\d\.`. `ANSI_RE` L11, `DIM_RE` L12 (lookahead). `plain()` L20.
7. **Bugs**: project_id userinfo/port (greenlight §7.4); `flags()` can't pass a text value beginning with `--`.
8. **Seeds**: tests/test_ade.py:11-35 pure (flags, draft, plain) → unit tests; 37-41 dash.repos out of scope.

## ade_cmux.py (selected by GREENLIGHT_ADE=cmux; library)
1. **CLI**: `call(noun, verb, *flags)` L113 implements `worktree ps|list|set|rm|create`, `terminal list|read|send`, `agent launch`, `repo list`; anything else → `RuntimeError("orca X Y has no cmux equivalent")`. `<2 args` → ValueError unpack.
2. **Files**: `~/.cmuxterm/*-hook-sessions.json` L18, glob order, later file wins on key collision.
3. **Invocations** `cmux --id-format uuids [--json] …` env `CMUX_QUIET=1`, timeout 60 (L22): `tree --all`, `list-windows`, `workspace list --window W`, `read-screen`, `rpc terminal.paste <compact JSON>`, `send --surface S -- text`, `new-pane` + `sleep 2` + send of `shlex.join(argv)`, `workspace create`, `workspace-action --action set-description|clear-description`, `workspace close`. Plus ade_common git calls.
4. **Env**: HOME, PATH; sets CMUX_QUIET for the child.
5. **Nondeterminism**: glob order, `time.sleep(2)`, `updatedAt*1000` may be a float.
6. **Python-isms**: STATES map L19; shlex.join quoting.
7. **Bugs**: `ags[0]["updatedAt"]` KeyError when missing; `worktree rm` with an unknown id → StopIteration traceback; `worktree set` ignores `--workspace-status`.
8. **Seeds**: `GREENLIGHT_ADE=cmux greenlight.py ade worktree list` with fake cmux tree/list-windows; `ade terminal send --terminal T --text hi --enter`; unknown verb → error text.

## ade_herdr.py (GREENLIGHT_ADE=herdr)
1. **CLI**: `call()` L110, same verb set as cmux + error text "has no herdr equivalent".
2. **Files**: `BRIEFS = $GREENLIGHT_HOME|~/.greenlight /briefs/<pane>-<epoch>.md` L15, written when the prompt has a newline or `len(shlex.quote(prompt).encode()) > 800`.
3. **Invocations**: `herdr [--session $GREENLIGHT_HERDR_SESSION] …` timeout 120, unwraps `result` (L19): `agent list`, `pane list|read (ansi)|run|send-text|split`, `agent prompt|start` (up to 15 tries; `agent_pane_busy` → sleep 1; `agent_not_ready` swallowed), `worktree create|remove`, `workspace close`.
4. **Env**: GREENLIGHT_HERDR_SESSION, GREENLIGHT_HOME, HOME.
5. **Nondeterminism**: epoch in the brief filename, sleep loop.
6. **Python-isms**: shlex.quote byte-length threshold.
7. **Bugs**: `next()` StopIteration on an unknown id; swallowed agent_not_ready can leave the prompt unsent silently.
8. **Seeds**: fake herdr `agent list`/`pane list`; long-prompt launch → briefs file exists.

## ade_t3.py (GREENLIGHT_ADE=t3)
1. **CLI**: `call()` L105, same pattern, "has no t3 equivalent".
2. **Files**: `HOME = $T3CODE_HOME or ~/.t3` L18; **sqlite read-only** `HOME/userdata/statev2.sqlite` table `projection_projects` (no ORDER BY; the URI path is not escaped, so `?`/`#` in the path break it); `model-manifest.json`.
3. **Invocations**: `T3 = which("t3") or HOME/bin/t3`, timeout 120, error `t3 <args[:2]>: …` (L25): `thread list|show|start|send` (prompt via **stdin**), `project add` (id = `stdout.split()[2]`), `project remove --force`.
4. **Env**: T3CODE_HOME, HOME, PATH.
5. **Nondeterminism**: sqlite row order; `ms()` L32 naive ISO → **local time**.
6. **Python-isms**: `datetime.fromisoformat` (3.11+ accepts most ISO forms); `int(ts*1000)` truncation; `read` returns `lines[-int(limit):]` (limit `"0"` → whole list).
7. **Bugs**: limit 0 returns everything; `split()[2]` IndexError on unexpected output. The harness has no sqlite fake, so it needs a real db file in `userhome/.t3/`.
8. **Seeds**: needs a fixture sqlite + fake t3; lowest priority.

## blocking.py (CLI + imported by greenlight/dash)
### 1. CLI (argparse, `main` L199)
`blocking.py [--json] [--fresh]`. argparse allows prefixes (`--js`, `--f`); unknown arg → usage on stderr, **rc2**.
- `blocks()` None → `sys.exit("blocking: GitLab couldn't answer, so this is no answer (not 'nothing blocked')")` rc1 (plus an earlier stderr `blocking scan failed: {e}`).
- `--json` → `json.dumps(b, indent=1)`.
- text: sorted by `(["approval","discussion","orphan","conflict","pipeline"].index(reason), key)` → `- [{reason}] {key} {title[:70]} — {why}\n  {url}`; empty → `nothing is blocked on me`. A reason outside the list → ValueError.
- No roster (`CONFIG["blocking"]` empty) → `{}`, prints "nothing is blocked on me", rc0, **no network**.
### 2. Files
`STATE/blocks.json` `{"ts": int, "blocks": {...}}` indent=1, mkdir parents (L181-183). CACHE=300s for `blocks()`, `cached()` default 6h (dash). Corrupt file → treated as absent.
### 3. Invocations (all via greenlight.gl/gl_all)
`open_mrs` L128: `projects/<pid>/merge_requests?state=opened…` filtered by milestone title **startswith** prefix (greenlight uses fullmatch) and project_id. `check` L54: MR detail, `pipelines?per_page=1`, `pipelines/<id>/jobs?scope[]=failed` (sorted by -id), `discussions`, `approvals`, reviewers, `commits`, `notes`.
### 4. Env
Via greenlight (GREENLIGHT_HOME, GREENLIGHT_ME, …).
### 5. Nondeterminism
`time.time()` ts; ThreadPoolExecutor(8); `who` **set** in discussion check (string-hash order → which user is listed first in `why` can vary per run with PYTHONHASHSEED); "first reason wins" depends on hits order within an MR (deterministic per check()).
### 6. Python-isms
`ROSTER.get(...) or {}` truthiness; `TEAMS` sets; `ts()` L46 ISO parse; `safe()` swallows every per-MR exception (→ `[]`) while open_mrs listing errors abort the whole scan (→ None).
### 7. Bugs
- Pipeline non-success with no failed jobs → orphan path that **skips** thread/approval checks.
- prefix startswith vs greenlight fullmatch: milestone `v1.2` matches `v1.20`.
- set-order `why` text is non-deterministic (needs PYTHONHASHSEED=0 in the harness, or the Rust side sorts, which breaks parity).
### 8. Seeds
tests/test_blocking.py:28-41 canned API by path suffix → fake glab keyed per path; 44-71 check cases (approval, discussion, orphan, conflict, pipeline) → `blocking.py --json --fresh` scenarios with a config `blocking` roster (the tests monkeypatch module globals REPOS/PEOPLE/TEAMS; scenarios must use config.json instead); 73-82 cache: pre-seeded `home/blocks.json` with a fresh ts → no network (needs a ts relative to now: setup.sh writes it). Plus: no roster → "nothing is blocked on me"; glab failure → rc1 message.

## tests/test_release.py
29 lines: scans the repo with `leak.re` for private strings; reads GREENLIGHT_HOME via `.get(..., default)` (not `or`). Not a behavior to port; keep it pointed at the Rust sources too.
