# Lifecycle scripts inventory: adopt.py, cleanup.py, auto.py, sync.sh

Scope: `scripts/adopt.py` (458 lines), `scripts/cleanup.py` (322), `scripts/auto.py` (226), `scripts/sync.sh` (11), and
`tests/test_adopt.py`, `tests/test_cleanup.py`, `tests/test_auto.py`. Helpers in `greenlight.py`, `harness.py` and
`blocking.py` are described only where these scripts call them (that agent's inventory covers them in full).
Refs are `file:line`. "Py" = observed CPython 3.15 behavior, probed where it says "probed".

## 0. Cross-cutting facts (apply to all three Python scripts)

- **Import side effects** (all three import `greenlight`): `STATE = $GREENLIGHT_HOME or ~/.greenlight` (greenlight.py:40);
  `CONFIG = {**DEFAULTS, **json(STATE/config.json)}`. The merge is shallow: a user `adopt` key replaces the default dict
  whole. Only FileNotFoundError is caught, so a malformed config.json gives a traceback and exit 1 from every
  subcommand (greenlight.py:57-62). `ME = $GREENLIGHT_ME or config.me or whoami()`. When me.json is missing,
  `whoami()` runs `glab api user` and then `gh api user` (timeout 20) at import time and writes `STATE/me.json`
  (greenlight.py:104-122). This happens even for `auto.py list`.
- **JSON encoding is Python's `json.dumps`** everywhere:
  - `ensure_ascii=True`: every non-ASCII char is written as `\uXXXX`, including MR titles, prompts and summaries.
  - Without indent the separators are `", "` and `": "`. With `indent=N` they are `","` + newline and `": "`.
    serde_json compact has no spaces (`PrettyFormatter::with_indent(b" ")` covers indent=1, but not the
    compact separators or `ensure_ascii`). Byte-exact fixtures need a custom formatter.
  - Indent per writer: adopt `save()` `indent=1` (adopt.py:66) and adopt `--json` `indent=1` (adopt.py:442).
    cleanup `--json` `indent=1` (cleanup.py:307). auto `save()` and add/edit print `indent=2` (auto.py:43,186).
    auto `show` has no indent (auto.py:188). log.jsonl, runs.jsonl and outbox.jsonl lines have no indent.
  - `Path.write_text(json.dumps(..))` writes no trailing newline. `print()` adds `\n`.
  - Key order is dict insertion order and must be reproduced (see each output).
  - `json.dumps({None: x})` gives `{"null": x}` (probed).
- **Exit codes, Python style**:
  - `sys.exit("msg")` prints msg plus `\n` to stderr and exits 1.
  - An uncaught exception prints a traceback to stderr and exits 1.
  - argparse errors print `usage: ...` plus `error: ...` to stderr and exit 2. `-h/--help` prints help to stdout and
    exits 0.
  - argparse `allow_abbrev=True` accepts unique prefixes: `--j` means `--json`, `--ca 5` means `--cap 5` (probed).
- **Subprocess text decoding**: every `subprocess.run(..., text=True)` decodes with the locale encoding (UTF-8 in
  practice) in strict mode. Invalid bytes raise `UnicodeDecodeError`, which surfaces as a generic exception.
- **Three different subprocess wrappers**, with different retry and timeout rules (details in §3):
  - `greenlight.run`, used by `gl`, `gl_all`, `gh` and `orca`: retries.
  - `cleanup.run`: no retries.
  - `adopt.git`: no retries, `check=False` returns `""`.
- **`greenlight.orca(*args)`** (greenlight.py:125-141):
  - If `GREENLIGHT_ADE` is not `orca`, it calls `ade_<ADE>.call(*args)` in-process (cmux/herdr/t3 shims, no
    subprocess timeout).
  - Otherwise it runs `[exe, *args, (--environment $ORCA_ENVIRONMENT), "--json"]`, where
    `exe = $ORCA_CLI_COMMAND or which("orca-ide") or ~/.config/orca/linux-orca-cli-shim/orca (if exists) or "orca"`.
    stdout must be `{"ok": true, "result": ...}`. With `ok` false it raises `RuntimeError("orca <args>: <error>")`.
    It returns `result`. Default timeout 120. cleanup passes 300 (cleanup.py:64).

---

## 1. CLI surface

### adopt.py

| Invocation | Behavior | stdout | stderr | Exit |
|---|---|---|---|---|
| `adopt.py dismiss K1 K2 ...` (adopt.py:423-425) | Handled before argparse via `sys.argv[1:2]==["dismiss"]`. Merges `{K: {"ts": int(now), "why": "ignored"}}` into dismissed.json. Keys are not validated: any string, including `--json`. With zero keys it still rewrites the file. Re-dismissing overwrites `ts`. | nothing | nothing | 0 |
| `adopt.py scan [--json] [--cap N]` | `plan(cap)` only, then prints. Despite "changes nothing" it can write blocks.json (blocking cache), me.json and `mkdir STATE` (via `greenlight.scan(save=False)` when last.json is stale). | text or JSON | plan errors | 0, or 2 on plan failure |
| `adopt.py precheck [--json] [--cap N]` | `plan`, prints `f"{len(create)} workspace(s) to adopt"`. `--json` is accepted and ignored. | that line | `adopt plan failed: {e}` | 0 if `create` is non-empty, else 1. Plan failure is also 1 (adopt.py:434). |
| `adopt.py apply [--json] [--cap N]` | `plan`, then `apply(p)`, then prints like scan with apply titles and marks. An exception inside `apply()` (e.g. a save) is uncaught: traceback, exit 1. | text or JSON | plan errors, ping "skipped" notes (redirected), git merge-base stderr | 0, or 2 on plan failure |
| no args / bad cmd | argparse | | usage | 2 |

- `cmd` is a positional with choices `scan|precheck|apply` (adopt.py:426).
- `--json` is `store_true`.
- `--cap` is `int`, default `CAP = CONFIG["adopt"].get("cap", 10)` (adopt.py:43). Negative or zero is accepted
  (`--cap -1` parses, probed) and makes every creatable item "deferred".
- **JSON output** (adopt.py:442): `json.dumps([{k: v for k, v in item.items() if k != "repo"} ...], indent=1)`. Item
  keys in insertion order:
  - From `cand()` (adopt.py:119-122): `key, project, project_id, iid, forge, title, source_branch, draft, role, author,
    updated_at, sha, web_url`. `gh_cands` uses the same order (adopt.py:135-138).
  - Then `block` (blocked MRs, added in `candidates()`).
  - Then `cleared` (tombstone re-requested).
  - Then `name` (only for the create verdict). `repo` is also set at that point but stripped from the output.
  - Then `verdict, reason`.
  - Then, under apply only: `created, path, branch` on success, or `created, error` on failure.
  - `role` is `"author"` or `"review"` (not "reviewer").
  - `project_id` is `null` for GitHub.
  - `updated_at` is `""` when missing.
  - A GitHub rename rewrites `project`/`key` values in place, so order is unchanged.
- **Text output** (adopt.py:444-454): groups in this order: `create`, `deferred`, `busy`, `no-repo`, `skip`. Titles:
  `CREATED` (apply) or `WOULD CREATE`, `DEFERRED (cap)`, `BRANCH BUSY (pinged)`, `NO ADE REPO (pinged)`, `SKIPPED`.
  Empty groups are omitted. Each group:
  ```
  \n## {title} ({n})
  - {mark}{key} [{role}{"/"+block if block}] {title[:60]} — {reason}[ — {error}]
  ```
  - `mark` is `✓ ` or `✗ ` only for apply plus the create group; otherwise `""`.
  - The separator is an em-dash U+2014 with spaces.
  - `title[:60]` counts code points.
  - Items inside a group keep candidate order (updated_at desc).
- **Exact reason strings** (adopt.py:288-316):
  - `covered: a workspace already has it`
  - `covered: created by an earlier adopt run, not scanned yet`
  - `tombstoned ({why}): its workspace was removed`, where `why` is `manual|cleanup|ignored`
  - `approved by me`
  - `I can't approve it (my commits are in it)`
  - `someone else's draft`
  - `no repo in the ADE for {project}` (verdict `no-repo`)
  - `{hit} is checked out in {path}` (verdict `busy`)
  - `over the {cap}-per-run cap: next run` (verdict `deferred`)
  - `{role} MR with no workspace`, with the suffix ` (I'm blocking it: {block})` when blocked (verdict `create`)

### cleanup.py

| Invocation | Behavior | stdout | stderr | Exit |
|---|---|---|---|---|
| `cleanup.py scan [--json]` | `scan()`, prints | text or JSON | `scan failed: {e}` | 0, or 2 on scan failure |
| `cleanup.py precheck [--json]` | prints `f"{len(cands)} workspace(s) to delete"` | line | `scan failed: {e}` | 0 if candidates exist, else 1. Scan failure is 1. |
| `cleanup.py apply [--json]` | `orca worktree rm --worktree id:<id>` for each candidate, in row order, then prints. Removal failures don't change the exit code. | text or JSON | | 0, or 2 on scan failure |

- argparse: `cmd` with choices `scan|precheck|apply`, `--json` (cleanup.py:281-284).
- **JSON** (cleanup.py:307): `json.dumps(rows, indent=1)`. Row keys: `id, name, path, branch, verdict, reason`, plus
  under apply for candidates only: `removed` (bool), and `error` when removal failed.
  - `name` is `displayName` or the basename of `path`.
  - `branch` has `refs/heads/` stripped; `""` when detached.
  - Rows follow `dedupe` order: first appearance of each normalized path in `orca worktree list`.
- **Text** (cleanup.py:309-318): groups `candidate` (`DELETED` under apply, else `WOULD DELETE`), `skip`
  (`SKIPPED (needs a human)`), `keep` (`KEPT`). Header `\n## {title} ({n})`. Rows sorted by `path` (code-point order).
  Line: `- {mark}{name} [{branch or '-'}] — {reason}[ — {error}]`. `mark` is `✓ ` or `✗ ` only for apply candidates.
- **Exact reason strings** (cleanup.py:169-223):
  - `main checkout`
  - `detached HEAD`
  - `pinned`
  - `a planner: the Plan view owns it`
  - `path missing on disk`
  - `repo: {repo.error}`
  - `default branch`
  - `MR lookup failed: {e}`
  - `MR {label} is open`
  - `MR {label} {state}[ (older {labels, comma+space} open, stale)]`
  - `{reason}, but local commits aren't in the MR head or any remote`
  - `detached HEAD, its MR wasn't found`
  - `no MR/PR, branch not merged`
  - `no MR/PR, tip in {base} but never pushed and idle only {idle_h:.0f}h (fresh branch?)`
  - `no MR/PR, branch merged into {base}`
  - `{reason}, but has uncommitted/untracked changes`
  - `{reason}, but a terminal was active in the last 30m`
  - From `scan.one`: `unknown repo`, `error: {e}`
  - Possible `{state}` values: `merged | closed | approved by me | not mine to approve`. Labels are `!N` (GitLab) or
    `#N` (GitHub).

### auto.py

The argv parser is hand-rolled, not argparse (auto.py:166-222).

- `args = sys.argv[1:]`. `cmd = args[0]`, defaulting to `list` when there are no args. `name = args[1]` or `None`.
- `opt(k)` returns the token after the *first* occurrence of `--k` anywhere in args, including the name position.
  A trailing flag gives IndexError, a traceback and exit 1. Unknown flags are ignored. A value can itself look like a
  flag.
- Option values are stored as **strings** (`"timeout": "30"`).

| Cmd | Behavior | stdout | stderr / exit |
|---|---|---|---|
| `list` (default) | For each automation in file order (auto.py:172-174), print `f"{n:24} {'on ' if enabled else 'off'}  {schedule:15} {provider:8} {'running' if alive(n) else ''}\n    {prompt[:100]}"`. Widths are minimum and left-padded, never truncated. There is a trailing space when not running. `provider(a)` is `a.provider or CONFIG.agents[0]`. | lines | 0 |
| `add <name> --schedule S --prompt P [--cwd D] [--precheck C] [--provider X] [--model M] [--effort E] [--timeout MIN] [--enabled\|--disabled]` | Fails if the name exists: `"{name}: already exists"`, exit 1. Builds `{"enabled": True} \| {k: opt(k) for k in OPTS if opt(k) is not None}` in OPTS order `schedule, prompt, cwd, precheck, provider, model, effort, timeout`. Requires non-empty schedule and prompt, else `"--schedule and --prompt are required"`, exit 1. Validates the cron via `due(schedule, localtime())`. A bad cron raises an uncaught ValueError (traceback, exit 1) and nothing is saved. Then save and print. Provider, timeout and cwd are not validated. | `json.dumps({name: a}, indent=2)` | |
| `edit <name> [opts] [--enabled\|--disabled]` | Fails if missing: `"{name}: no such automation"`. Merges into the existing dict: existing keys keep their position and new keys append in OPTS order. `enabled = "--enabled" in args` when either flag is present, so both flags means True. Same validation, save and print as add. **The Swift app calls `edit greenlight --disabled` and `edit greenlight --enabled --schedule '*/N * * * *'` over ssh (app/Greenlight.swift:833).** | same | |
| `show <name>` | `print(json.dumps(autos[name]))`: compact, one line, `", "`/`": "` separators. **The Swift app parses this for `schedule` (`*/N` prefix) and `enabled` (app/Greenlight.swift:816-826).** | JSON line | missing name: `"{name}: no such automation"`, exit 1 |
| `rm <name>` | pop, save, print `removed {name}` | line | missing name: exit 1 |
| `run <name>` | `spawn(name)` prints `{name}: started` or `{name}: still running, skipped` | line | missing name: exit 1 |
| `exec <name>` | **Hidden.** The child process that `spawn` launches runs `execute(name)`. A missing name raises KeyError (traceback into errors.log). | nothing | |
| `runs [name]` | Reads runs.jsonl (every line `json.loads`; a blank line gives a traceback), filters by name, keeps the last 20, prints `f"{strftime('%m-%d %H:%M', localtime(ts))} {name:24} {status:8} {engine or '':6} {secs}s  {summary[-80:]!r}"`. **`!r` is Python `repr()`** (see §6). Two spaces precede the repr. | lines | 0 |
| `tick` | `tick()`: for each automation where `enabled` (default True) and `due(schedule, localtime())`, call `spawn`. | spawn lines (discarded by launchd/systemd) | |
| `install` | See §2. | one line | `CalledProcessError` traceback, exit 1, if load/enable fails |
| anything else | `sys.exit(__doc__)`: the whole module docstring (auto.py:1-20) goes to stderr | | exit 1 |

- `name=None` cases:
  - `add --schedule .. --prompt ..` with no name: `None in autos` is False, so it saves under the JSON key
    **`"null"`** (bug, §7).
  - `show`, `rm` and `run` without a name print `None: no such automation` and exit 1.

### sync.sh

- `GREENLIGHT_HOST=<ssh host> scripts/sync.sh`. No args.
- `set -euo pipefail`; `cd "$(dirname "$0")/.."` (sync.sh:5-6).
- Host: `$GREENLIGHT_HOST`, else `defaults read dev.greenlight.app host` (macOS only; errors ignored) (sync.sh:7).
- An empty host or the literal `local` prints `usage: GREENLIGHT_HOST=<ssh host> scripts/sync.sh` to stderr and
  exits 1 (sync.sh:8).
- If `./memory` exists, pull: `rsync -au "$H":.claude/skills/greenlight/memory/ memory/`. Failures are ignored
  (sync.sh:9).
- Push: `rsync -a --exclude .git --exclude .gitignore --exclude memory --exclude app ./ "$H":.claude/skills/greenlight/`
  (sync.sh:10).
  - No `--delete`.
  - It copies everything else: scripts/, tests/, lifecycles/, prompts/, migration/, etc.
  - The remote path is relative to the ssh home.
  - rsync creates only the last directory level, so it fails if `~/.claude/skills` is missing.
- Push memory: `rsync -au memory/ "$H":.claude/skills/greenlight/memory/ || true` (sync.sh:11).
- "Newest wins" comes from `-u`. Deletions don't propagate (documented ponytail).
- **Porting impact**: it ships source, not a binary. A Rust build for the host's OS/arch must be built there or
  cross-compiled and synced. The `--exclude app` list would also need `target/`.

---

## 2. Files

All under `STATE = $GREENLIGHT_HOME or ~/.greenlight` unless noted.

| File | Who | Read / write | Format | Mode |
|---|---|---|---|---|
| `dismissed.json` | adopt (adopt.py:45) | read in plan (`load`, missing or invalid → `{}`); written by `apply` (adopt.py:401) and `dismiss` (adopt.py:424) | `{"<project>!<iid>": {"ts": int, "why": "manual\|cleanup\|ignored"}}`, indent=1 | rewrite |
| `adopt.json` | adopt (adopt.py:46) | read in plan and apply; written by apply (adopt.py:415) | `{"seen": {key: {"id", "name"}}, "created": {key: int ts}}`, indent=1 | rewrite |
| `log.jsonl` | adopt (adopt.py:393-396), cleanup (cleanup.py:274-277); adopt reads it in `removed_by_cleanup` (adopt.py:242-250) | append | one JSON object per line: `{"ts": int, **entry}`. adopt entries: `{"ts","ws","mr","did":"adopt","project","role"}` or `{"ts","ws","mr","did":"adopt failed","error"}`. cleanup entries: `{"ts","name","path","branch","reason","removed"[,"error"]}`. adopt matches cleanup lines on `name` plus truthy `removed`. Unparseable lines are skipped. | append, `mkdir -p` parent |
| `last.json` | adopt reads (adopt.py:225) | read | `{"ts", "rows": [...]}` (greenlight.scan). It is used when `rows` is not null and `now - ts <= interval_s()`; otherwise adopt does a fresh `greenlight.scan(save=False)`. | read |
| `automations.json` | adopt reads `greenlight.schedule` (adopt.py:218); auto reads and writes (auto.py:32,37-43) | auto `load` has **no error handling** (a malformed file gives a traceback for every cmd, including tick). auto `save`: `STATE.mkdir(exist_ok=True)`, no `parents=True`, indent=2. | `{name: {"enabled": bool, "schedule", "prompt", ["cwd","precheck","provider","model","effort","timeout"]: str}}` | rewrite |
| `auto/<name>.pid` | auto (auto.py:64,95,117) | written by `execute` *before* the precheck; unlinked at the end (missing_ok). Read by `alive`. Left behind on a crash. | decimal pid, no newline | rewrite |
| `auto/<name>/<YYYYmmdd-HHMMSS>.log` | auto (auto.py:97-113) | `open("w")` | text (§3 auto). The name uses local time with second resolution. `logf.parent.mkdir(exist_ok=True)` has no parents, so a name containing `/` crashes. | create/truncate |
| `auto/runs.jsonl` | auto (auto.py:115-116, 198-199) | append, read by `runs` | `{"name","ts":int,"log":str,"status":"skipped\|ok\|failed",["engine"],"summary","secs":int}`. Key order for skipped: name, ts, log, status, summary, secs. For ok/failed: name, ts, log, status, engine, summary, secs. | append |
| `auto/errors.log` | auto spawn (auto.py:126) | the child's stderr (append) | tracebacks from `exec` | append |
| `outbox.jsonl`, `pinged.json` | via `greenlight.ping` (adopt apply pings, auto failed-run ping) | append / rewrite | `{"ts", "text", ["key"], ["ws"]}`. FLAP dedupe: a key starting with `@` that was pinged in the last 24h is skipped. | |
| `history/<name with / → __>.md` | via `greenlight.note` (adopt.py:411) | append `- {%Y-%m-%d %H:%M local} adopted {ref} ({role}): no workspace existed\n` | | append |
| `blocks.json` | via blocking: adopt calls `blocking.blocks()` (cache 300s; may rescan and write); cleanup calls `blocking.cached()` (read only, max age 6h) | | `{"ts", "blocks": {key: {"reason","why","n","url","title"}}}` | |
| `me.json` | via `greenlight.whoami` at import | | `{"me": ...}` | |
| `config.json` | read at import | | keys used here: `adopt.cap`, `adopt.roles` (an unknown role gives KeyError in `ROLES[r]`), `agents`, `harnesses`, `blocking` | |

### auto.py install (auto.py:140-163, 205-220)

**macOS** (`sys.platform == "darwin"`):

- File: `~/Library/LaunchAgents/greenlight-auto.plist` (`Path.home()`, mkdir -p of the parent). Content:
  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>greenlight-auto</string>
    <key>ProgramArguments</key><array><string>{exe}</string><string>{path}</string><string>tick</string></array>
    <key>EnvironmentVariables</key><dict><key>PATH</key><string>{env_path}</string></dict>
    <key>StartInterval</key><integer>60</integer>
  </dict></plist>
  ```
- Placeholders:
  - `exe = sys.executable` (the python3 binary).
  - `path = Path(__file__).resolve()` (the absolute auto.py).
  - `env_path = $PATH` of the installing shell, so glab/gh/orca/agents are found later.
- The values go in through `str.format` with **no XML escaping**.
- The plist has no RunAtLoad, no StandardOut/ErrPath (output is discarded), and no WorkingDirectory.
- Commands, in order:
  1. `launchctl unload <file>`: `capture_output`, result ignored.
  2. `launchctl load -w <file>`: `check=True`, so a failure gives a traceback and exit 1.
  3. Print `greenlight-auto launch agent loaded ({file})`.

**Other OSes** (systemd user units):

- Files under `~/.config/systemd/user/` (`Path.home()`, ignores `$XDG_CONFIG_HOME`; mkdir -p).
- `greenlight-auto.service`:
  ```ini
  [Unit]
  Description=Greenlight automations tick
  [Service]
  Type=oneshot
  KillMode=process
  ExecStart={exe} {path} tick
  ```
  `ExecStart` is unquoted, so a path with spaces breaks. There is no `Environment=PATH`, so the systemd user PATH
  applies. `harness.binary()` falls back to `~/.local/bin/<n>` and `~/.<n>/bin/<n>` for that reason (harness.py:29-32),
  but glab/gh/orca used by prechecks get no such fallback.
- `greenlight-auto.timer`:
  ```ini
  [Unit]
  Description=Run greenlight automations every minute
  [Timer]
  OnCalendar=*-*-* *:*:00
  AccuracySec=1s
  [Install]
  WantedBy=timers.target
  ```
- Commands: `systemctl --user daemon-reload` (check), then `systemctl --user enable --now greenlight-auto.timer`
  (check). Prints `greenlight-auto.timer enabled`.

**Existing install detection**: there is none. Each install blindly overwrites the same filenames and Label.
launchd gets unload then load; systemd gets daemon-reload then enable --now (idempotent).

**What the Rust port must change**:
- `{exe} {path} tick` becomes `<rust binary> auto tick` (or whatever the subcommand is). The plist and unit must
  stop naming python3 and auto.py.
- Reinstalling over an old Python install works because the names are identical.
- Escaping should be added, or the current lack of it documented.

**Load-bearing**:
- `KillMode=process` together with `spawn`'s `start_new_session=True`: the oneshot `tick` exits immediately while
  the setsid'd `exec` child keeps running. Without `KillMode=process`, systemd would kill the child when the service
  ends.
- launchd `StartInterval=60` is not minute-aligned (see §5).

**Stored commands that reference python3**:
- Existing `automations.json` entries contain user-written `precheck` strings and prompts.
- SKILL.md tells agents to run `python3 ~/.claude/skills/greenlight/scripts/{adopt,cleanup,greenlight}.py ...`.
  Examples: SKILL.md:19 and :44, and `greenlight.py precheck` as a precheck.
- These keep working only if the Python entry points remain, or are replaced by shims at the same paths, or the
  stored strings are migrated.
- The Swift app runs `"\(py)/auto.py show|edit greenlight"` over ssh (app/Greenlight.swift:816,833).

---

## 3. External invocations

### adopt.py

All `git` calls go through `adopt.git` (adopt.py:50-54): `["git","-C",path,*args]`, `capture_output`, `text`,
`timeout=180`, cwd None, inherited env. When `check` is set and rc≠0 it raises
`RuntimeError(f"git {' '.join(args)}: {(stderr or stdout).strip()[:300]}")`. It returns `stdout.strip()`; with
`check=False` it returns stdout even on failure, usually `""`. No retry.

| Where | Tool / argv | Parsed | Failure |
|---|---|---|---|
| repo_map adopt.py:86 | `orca repo list` → `["repos"]` (`id`, `path`, `gitRemoteIdentity.canonicalKey`) | JSON (envelope) | propagates: plan fails, exit 2/1 |
| repo_map adopt.py:89 | `git -C <repo.path> remote get-url origin` (check=False), only when canonicalKey is missing | text | `""`: repo ignored |
| checked_out adopt.py:99 | `git -C <path> worktree list --porcelain` (check=False); parses `worktree <p>` and `branch refs/heads/<b>` lines | text | `""`: nothing busy |
| candidates adopt.py:164 | `glab api merge_requests?scope=all&state=opened&author_username=<ME>&per_page=100&page=N`, and the same with `reviewer_username`. `gl_all` pages manually until a batch has fewer than 100 items (greenlight.py:148-156). ME is **not URL-encoded**. One query per role in `CONFIG.adopt.roles` (default `["author","reviewer"]`; the in-code fallback `ROLES` dict iterates the same keys). | JSON | `greenlight.run(tries=3)`: retries a timeout or a TRANSIENT_RE error after 2s then 4s sleeps, timeout 45. A final failure raises and plan fails. |
| gh_cands adopt.py:134 | `gh api graphql -f "q=is:pr is:open archived:false author:@me" -f "query=<GH_SEARCH>"` (and `review-requested:@me`). `first: 100`, **no pagination**. Fields per node: `number title isDraft headRefName headRefOid updatedAt url author{login} repository{nameWithOwner}`. Empty or null nodes (non-PR results) are filtered by `if p`. | JSON | gh: tries=3, timeout 45; raises and plan fails |
| candidates adopt.py:167 | `blocking.blocks()` (only when gitlab is among the forges). Returns `{}` when config `blocking` is off, the cached file if younger than 300s, else a fresh scan (writes blocks.json), or None on failure after printing `blocking scan failed: {e}` to stderr. | | None means blocks_ok=False |
| candidates adopt.py:172 | `glab api projects/<urlquote(proj)>/merge_requests/<iid>` for each blocked key not already seen, in **sorted key order** | JSON | any exception: skip that key. A state other than `opened` is skipped. |
| gh_renamed adopt.py:150 | `gh api repos/<proj>` → `full_name`. Only runs if some GitHub candidate's project isn't in repos. One call per GitHub repo in the ADE. | JSON | exception: skip |
| gh_login adopt.py:128 | `gh api user` → `login`, cached per process (`functools.cache`) | JSON | raises |
| review_done adopt.py:189 | GitHub: `gh pr view <iid> -R <proj> --json latestReviews` | JSON | raises: plan fails |
| review_done adopt.py:191 | GitLab: `glab api projects/<project_id numeric>/merge_requests/<iid>/approvals` → `approved_by[].user.username`, `user_can_approve` (default True) | JSON | raises: plan fails |
| rerequested adopt.py:208 | GitHub: `gh pr view <iid> -R <proj> --json reviewRequests` → `[].login` (team requests have none) | JSON | raises |
| rerequested adopt.py:210 | GitLab: `glab api projects/<project_id>/merge_requests/<iid>/reviewers` → `[].user.username`, `[].state == "unreviewed"` | JSON | raises |
| current_rows adopt.py:228 | `greenlight.scan(save=False)`: orca `worktree ps`, `worktree list`, `terminal list` (`--limit 10000`), plus per-row glab/gh (other inventory) | | raises: plan fails |
| create adopt.py:376 | author: `git -C <repo> fetch origin <source_branch>` (check) | | raises: item `created=False`, `error=str(e)[-300:]` |
| create adopt.py:382 | review, orca ADE: `git -C <repo> fetch origin +<head>:mr-<iid>`. `head` is `refs/pull/<n>/head` (GitHub) or `refs/merge-requests/<n>/head` (GitLab). | | same |
| create adopt.py:385 | review, other ADE: `git -C <repo> fetch origin <head>`; base = MR `sha` | | same |
| create adopt.py:387 | `orca worktree create --name <name> --repo <id:ID \| ID> --base-branch <base> [--gitlab-mr N \| --pr N] --comment "<Merge\|Review> <!N\|#N>: <title>" --setup skip` (create_args adopt.py:347-352). `--repo id:` and the MR-link flag only when `GREENLIGHT_ADE` is orca or unset. Result: `res.worktree` or `res` itself; `.path`. | JSON | same; the worktree may already exist (§7) |
| ensure_branch adopt.py:357 | `git symbolic-ref --short -q HEAD` (check=False; `""` when detached) | | |
| adopt.py:359 | `git rev-parse --verify -q refs/heads/<want>` (check=False) | | |
| adopt.py:361 | `git switch -c <want>` (check) | | raises |
| adopt.py:362 | **`subprocess.run(["git","-C",path,"merge-base","--is-ancestor",want,"HEAD"])`**: no capture (inherits stdout/stderr), **no timeout**, only the rc is used | rc | |
| adopt.py:363 | `git switch -C <want>` (check) | | raises |
| adopt.py:367 | `git branch -D <cur>` (check=False) | | ignored |
| adopt.py:369 | `git branch --set-upstream-to=origin/<upstream> <want>` (check), authored MRs only | | raises |
| apply adopt.py:418 | `greenlight.ping(text, key)` with stdout redirected to stderr | file writes | |

Call order inside `create()` for an authored MR: fetch, orca create, ensure_branch git calls. The test asserts the
exact git argv sequence through mocks (test_adopt.py:211-217).

### cleanup.py

`cleanup.run` (cleanup.py:56-60): `subprocess.run(cmd, cwd=None, capture_output=True, text=True, timeout=60)`. With
`check` and rc≠0 it raises `RuntimeError(f"{' '.join(cmd)}: {(stderr or stdout).strip()[:300]}")`. **No retries.**
TimeoutExpired propagates (it is not a RuntimeError).

| Where | argv | Parsed | Failure |
|---|---|---|---|
| scan cleanup.py:250 | `orca worktree list --limit 10000` (timeout 300) → `["worktrees"]` | JSON | scan failed, exit 2/1 |
| cleanup.py:252 | `orca terminal list` → `["terminals"]` (`worktreeId`, `lastOutputAt` ms). **No `--limit`**, unlike greenlight.scan. | JSON | scan failed |
| cleanup.py:256 | `orca repo list` → `["repos"]` (`id`, `path`) | JSON | scan failed |
| Repo.__init__ cleanup.py:82 | `git -C <root> remote get-url origin` (check) | text | `self.error = str(e)` |
| cleanup.py:83 | `git -C <root> fetch --quiet --prune origin` (timeout 180; **mutates the repo**: prunes remote refs) | | error |
| cleanup.py:84 | `git -C <root> symbolic-ref --short refs/remotes/origin/HEAD` (check=False) | `origin/<default>` | |
| cleanup.py:86 | `git -C <root> remote set-head origin --auto` (check=False; **mutates**), then symbolic-ref again (check) | | error |
| Repos | `Repo(path)` runs in a `ThreadPoolExecutor(8)`, so fetches run concurrently | | |
| mrs GitLab cleanup.py:110 | `glab api projects/<quote(project,safe="")>/merge_requests/<iid>` for each named iid | JSON | an error message containing `404` is skipped; anything else raises → `MR lookup failed: ...` |
| cleanup.py:116 | `glab api projects/<pid>/merge_requests?source_branch=<b>&state=all&order_by=created_at` (urlencode; **one page**, GitLab default 20) | JSON list | raises |
| cleanup.py:125 | `glab api projects/<pid>/merge_requests/<iid>/approvals` for each open MR not authored by ME, fetched even when blocked | JSON | raises |
| cleanup.py:126 | `blocking.cached()` (reads blocks.json, max age 6h; returns `{}` when blocking is off) | | |
| mrs GitHub cleanup.py:135 | `gh pr view <n> -R <project> --json number,state,headRefOid,headRefName,author,latestReviews` for each named n (**no 404 tolerance**) | JSON | raises |
| cleanup.py:137 | `gh pr list -R <project> --head <branch> --state all --json <same fields>` (default limit 30) | JSON | raises |
| cleanup.py:139 | `gh api user` → `login`, only if some PR is OPEN; called once per worktree, not cached | JSON | raises |
| pushed_heads cleanup.py:154 | `glab api projects/<pid>/merge_requests/<iid>/versions?per_page=100` → `[].head_commit_sha`, called lazily per MR | JSON | raises → evaluate raises → `error: ...` |
| evaluate cleanup.py:184 | `git -C <wt> config branch.<branch>.merge` (check=False) | text | |
| cleanup.py:187 | `git -C <wt> rev-parse HEAD` (check) | text | raises → `error: ...` |
| is_ancestor cleanup.py:72 | `git -C <wt> merge-base --is-ancestor <a> <b>` (check=False, rc only) | rc | |
| cleanup.py:205 | `git -C <wt> rev-list -1 <tip> --not --remotes` (check) | non-empty = unpushed | raises |
| cleanup.py:219 | `git -C <wt> status --porcelain --untracked-files=normal` (check) | non-empty = dirty | raises |
| apply cleanup.py:300 | `orca worktree rm --worktree id:<id>` (timeout 300) | JSON | `removed=False`, `error=str(e)[-300:]` |

Worktrees are evaluated in a `ThreadPoolExecutor(8)`; `map` keeps input order.

### auto.py

| Where | argv | stdin | cwd | env | timeout | Failure |
|---|---|---|---|---|---|---|
| execute precheck auto.py:103 | `/bin/sh -c "<precheck>"` (`shell=True`), `capture_output`, `text` | inherited (the spawn child's stdin) | `expanduser(cwd or "~")` | inherited | 300s | rc≠0 → skipped. **TimeoutExpired, or a missing cwd (FileNotFoundError), is uncaught**: the exec child dies with a traceback in errors.log, the pid file is left, nothing goes to runs.jsonl, no ping. |
| agent auto.py:79-81 | `harness.argv(engine, prompt, model=, effort=)` (harness.py:35-58): |  `text` = prompt | `expanduser(cwd or "~")` | inherited | `int(timeout or 120)*60` s | see below |

`harness.argv` per engine. `perms` is the bypass list when `harnesses.<e>.bypass` is set, otherwise the safe list.

| Engine | argv | Safe perms | Bypass perms |
|---|---|---|---|
| claude | `[bin, "-p", *perms, ("--model", m), ("--effort", e)]` | `--permission-mode acceptEdits` | `--permission-mode bypassPermissions` |
| codex | `[bin, "exec", "--skip-git-repo-check", *perms, ("-m", m), ("-c", f"model_reasoning_effort={eff}"), "-"]` | `-c sandbox_mode=workspace-write` | `--dangerously-bypass-approvals-and-sandbox` |
| opencode | `[bin, "run", *perms, ("-m", m), ("--variant", e)]` | (none) | `--auto` |
| pi | `[bin, "-p", *perms, ("--model", m), ("--thinking", ...)]` | none: raises ValueError → exit 127 | (empty list) |

- codex maps effort `xhigh` and `max` to `high`. pi maps `max` to `xhigh`.
- Optional flag pairs are omitted when the value is falsy. `model` and `effort` are only passed when
  `engine == provider(a)`; otherwise the value is `False` and the flag is omitted.
- `bin` is `harnesses.<e>.bin`, else `which(e)`, else `~/.local/bin/e`, else `~/.<e>/bin/e`, else the bare name.
- An unknown engine raises ValueError (exit 127).
- The agent's stdout and stderr are merged (`stderr=STDOUT`).
- Results:
  - TimeoutExpired: `out = (e.stdout or b"").decode(errors="replace") + "\n[timed out]"`, `code = 124`. The partial
    output arrives as bytes on POSIX. Only the direct child is killed; grandchildren survive.
  - `ValueError` or `OSError` (unknown harness, pi without bypass, missing binary, missing cwd, non-integer
    `timeout`): `out = str(e)`, `code = 127`.
  - A child killed by a signal leaves a negative returncode (e.g. `[exit -9]`).
- Engine order and fallback:
  - `dict.fromkeys([provider(a), *CONFIG.agents])`: ordered and deduplicated.
  - Stop at the first engine where `not (code and LIMIT_RE.search(out))`.
  - `LIMIT_RE` (greenlight.py:68, case-insensitive search): `usage limit | limit reached | 5-hour limit | weekly limit |
    out of (?:extra )?usage | rate limit exceeded | exceeded your current quota | insufficient_quota |
    429 Too Many Requests`.
- Status: `ok` if the final code is 0, else `failed`.

Log file content, exactly (auto.py:87,105):
```
$ precheck: <cmd>\n<stdout><stderr>[exit <rc>]\n          (only when a precheck is set)
$ <engine>\n<out>\n[exit <code>]\n                          (once per engine tried)
```

- Run summaries:
  - Skipped: `(stdout or stderr).strip()[-200:]`.
  - Ok or failed: `out.strip()[-300:]`.
- Failed run: `greenlight.ping(f"Automation {name} failed: {summary[-150:]}\nLog: {logf}\nOptions: look into it | ignore")`,
  with no key (so no FLAP dedupe).
- **spawn** (auto.py:122-129): `Popen([sys.executable, __file__, "exec", name], start_new_session=True,
  stdout=DEVNULL, stderr=<STATE/auto/errors.log opened "a">)`. stdin is inherited and cwd is inherited.
  `__file__` is as invoked and may be relative; it works because cwd is inherited. In Rust this becomes
  `current_exe() auto exec <name>` or similar, detached with setsid.
- install: `launchctl` or `systemctl` (see §2).

### sync.sh

- `defaults read dev.greenlight.app host` (stderr discarded).
- rsync three times, over ssh to `$H` (§1).

---

## 4. Env vars read

| Var | Where | Effect |
|---|---|---|
| `GREENLIGHT_HOME` | greenlight.py:40 | STATE dir (all files in §2) |
| `GREENLIGHT_ME` | greenlight.py:122 | username; when unset, config.me, then whoami (shells out) |
| `GREENLIGHT_ADE` | adopt.py:70 (`orca_ade`), greenlight.py:127 (orca dispatch) | `orca` (default) or `cmux`/`herdr`/`t3`. Affects `create_args` (`--repo id:X` vs `X`, MR-link flags) and the review fetch strategy (adopt.py:381-386). |
| `ORCA_CLI_COMMAND`, `ORCA_ENVIRONMENT` | greenlight.py:136-137 | orca exe; `--environment` arg |
| `HOME` | `Path.home()`, `os.path.expanduser` | install paths, cwd `~`, orca linux shim, harness fallbacks |
| `PATH` | auto.py:209 (captured into the plist); subprocess lookup everywhere; `shutil.which` | |
| `TZ` / system zone | `time.localtime`, `time.strftime` | cron matching, log names, runs listing, history note timestamps |
| `LANG`/`LC_*` | subprocess `text=True` decoding | |
| `GREENLIGHT_HOST` | sync.sh:7 | ssh host |
| (none) | cleanup.py | reads no env directly |

Config keys consumed: `adopt.cap`, `adopt.roles`, `agents` (auto default provider and fallback order),
`harnesses.<n>.{bypass,bin}`, `blocking` (whether blocking runs at all; read by blocking.py), `me`.

---

## 5. Nondeterminism

### Clocks

- adopt:
  - `now = time.time()` (float) in plan. Tombstone `ts = int(now)` (adopt.py:282).
  - `apply` uses a fresh `int(time.time())` for `created` (adopt.py:400) and for `log` entries.
  - `dismiss` uses `int(time.time())`.
  - `current_rows` freshness check: `time.time() - last.ts <= interval_s()`. `interval_s` is `*/N` minutes from
    `automations.json["greenlight"]["schedule"]` (regex `^\*/(\d+) `, needs a trailing space), else 1800s.
  - Guard: `created[k] > rows_ts`, where `rows_ts` is the last.json ts or now (adopt.py:289).
  - `iso_ts(updated_at)` = `calendar.timegm(strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))`, which **ignores the tz suffix and
    fractions** (`2033-05-18T03:00:00.123+02:00` → treated as UTC 03:00, probed). Empty string gives 0.
  - blocking cache freshness is 300s.
- cleanup:
  - `NOW_MS = time.time()*1000` captured **at import** (cleanup.py:51).
  - Terminal busy cutoff is `NOW_MS - 30*60_000`, compared strictly with `>` (cleanup.py:251-253).
  - `idle_h = (NOW_MS - (lastActivityAt or NOW_MS)) / 3.6e6`. A missing or 0 lastActivityAt means idle 0. The
    threshold is `< 48`. Formatted with `:.0f`.
  - `blocking.cached()` max age is 6h.
- auto:
  - `tick` uses `time.localtime()` at minute granularity; seconds are ignored.
  - The log filename uses local `%Y%m%d-%H%M%S`.
  - `runs` uses local `%m-%d %H:%M`.
  - `ts` and `secs` use `int` truncation.
  - A missed minute is never caught up (auto.py:134).
  - launchd `StartInterval=60` is relative to load time, not minute-aligned, and coalesces after sleep. A wall-clock
    minute can be skipped (e.g. ticks at 12:00:59.9 and 12:02:00.1) or seen twice. Python dedupes nothing except
    `alive()` (a still-running pid).
  - systemd: `OnCalendar *:*:00`, `AccuracySec=1s`.
  - DST: local-time matching means a skipped hour never fires and a repeated hour fires twice.

### Cron parser (auto.py:46-60): exact semantics

`field(spec, lo, hi)`:

1. Split `spec` on `,`.
2. For each part, `rng, _, step = part.partition("/")` (first `/`).
3. If `rng == "*"`, then `(a, b) = (lo, hi)`. Otherwise `a, b = map(int, (rng.split("-") * 2)[:2])`:
   - `"5"` gives 5..5.
   - `"1-5"` gives 1..5.
   - `"1-5-9"` gives 1..5; the extra part is silently ignored (probed).
   - `"-5"` and `"5-"` give `int("")`, a ValueError.
4. `range(a, b+1, int(step or 1))` is added to the set.
   - `a > b` gives an empty set (`5-1` → ∅).
   - A negative step gives ∅. Step 0 is a ValueError.
   - `a/n` with no range means the **single value a** (`5/10` → {5}; Vixie would give 5,15,25,...).
   - Values are **not clamped or validated** against lo..hi (`70` is accepted and never fires).
5. `int()` is Python's: it accepts leading `+`, underscores (`1_0` → 10) and Unicode digits (`٣` → 3) (probed).
   Names (`MON`, `JAN`), empty parts (`1,,2`), `?`, `L`, `W` and `@hourly` are all ValueErrors.

`due(cron, t)`:

- `cron.split()` on any whitespace must give exactly 5 fields; otherwise ValueError (unpack).
- Fields: minute ∈ field(m, 0, 59), hour ∈ field(h, 0, 23), month `tm_mon` ∈ field(mon, 1, 12).
- DOM: `tm_mday ∈ field(dom, 1, 31)`.
- DOW: `(tm_wday + 1) % 7` (Mon=1 ... Sun=0) ∈ `field(dow, 0, 7) | ({0} if "7" in dow else set())`.
  - The 0..7 universe means `*/2` is {0,2,4,6} and `*/7` is {0,7}.
  - Sunday matches if `0` is in the set **or the character `7` appears anywhere in the dow string**.
- Day rule: `(ok_dom and ok_dow) if "*" in (dom, dow) else (ok_dom or ok_dow)`.
  - The test is **exact string equality with `"*"`**. `*/1`, `*/2` and `1-31` count as restricted, which gives OR
    semantics.
  - Vixie cron treats any field *starting* with `*` as unrestricted (AND). Probed on Monday 2026-10-26:
    `0 0 */1 * 2` → True (Python), while Vixie would say False.
- add/edit validation calls `due(schedule, localtime())`, so only parse errors are caught, not range errors.
- Errors in `tick` propagate: one bad schedule aborts the loop for the remaining automations (a traceback, exit 1,
  output discarded by launchd).

### Ordering

- adopt candidate order: `seen` dict insertion order, then `sorted(..., key=updated_at (string), reverse=True)`.
  - Python's `reverse=True` is **stable**: equal keys keep their original order. Use
    `sort_by(|a, b| b.cmp(a))`, never sort-then-reverse.
  - Insertion order: for each role in config roles (author, reviewer): GitLab results (API order across pages), then
    GitHub search results. Then blocked-only MRs in **sorted key string order** (`"x!20" < "x!3"`).
  - The first role wins dedupe (`setdefault`), so authored beats review.
  - `updated_at` compares as a raw string. GitLab `...T03:00:00.123Z` and GitHub `...T03:00:00Z` mix, and string
    order differs from time order around fractional seconds.
- Pings: no-repo pings sorted by project, then one `@adopt-busy` ping. Items within each keep candidate order.
- cleanup:
  - Rows follow first-seen normalized path from `orca worktree list` (`dedupe`, cleanup.py:229-246).
  - Twins are ordered by a stable sort so linked entries come first; `ids` follows that sorted order.
  - Text output sorts by path. JSON keeps scan order.
  - MR lists are sorted by `-iid`.
  - In GitHub `mrs`, `reversed(ps)` means the *first* occurrence of a PR number wins (cleanup.py:142-145). In GitLab
    `mrs`, the explicit iid lookups come before the branch list and the first wins.
- auto: `list`, `tick` and `runs` follow file order of `automations.json` / `runs.jsonl`.

### Concurrency, pids and temp paths

- cleanup uses two thread pools of 8. Concurrent `git fetch` runs per repo root; two repo ids with the same path would
  race on locks.
- auto: pid files hold `os.getpid()` of the exec child.
  - `alive()` is `os.kill(pid, 0)`. PermissionError counts as an OSError, so a process owned by another user reads as
    dead.
  - **pid `0` or a negative pid reads as alive** (signals the process group).
  - A reused pid reads as alive (stale pid file after a crash).
- No randomness and no temp files in these scripts. Tests use `tempfile.mkdtemp()`.

---

## 6. Python-isms a Rust port could get wrong

- **`repr()`** in `auto.py runs` (`{summary[-80:]!r}`):
  - Single quotes, unless the string contains `'` and no `"`, in which case double quotes (`"it's"`).
  - Escapes `\\`, `\n`, `\r`, `\t`, the active quote char, other non-printables as `\xNN`/`\uNNNN`. Printable Unicode
    is kept raw (`'é\x01'`) (probed).
  - Rust's `{:?}` differs (it always uses `"` and `\u{..}`).
- **json.dumps** separators, `ensure_ascii` and indent (§0).
- **Code-point slicing**:
  - adopt: `title[:60]`, `str(e)[-300:]`.
  - auto: `prompt[:100]`, `summary[-80:]`, `[-150:]`, `[-200:]`, `[-300:]`.
  - Wrappers: `(stderr or stdout).strip()[:300]`.
  - Rust must slice by `char`, not bytes. A byte slice can panic mid-UTF-8.
- **`str.strip()`** treats `\x1c-\x1f` as whitespace; Rust `trim()` does not (both strip `\x85`) (probed: Python strips
  `"\x1c a\x1f"` to `"a"`, Rust keeps it).
- **`splitlines()`** also splits on `\r`, `\x0b`, `\x0c`, `\x1c-\x1e`, `\x85`, ` `, ` `. It is used on
  log.jsonl (adopt.py:243), runs.jsonl (auto.py:199) and `git worktree list --porcelain` (adopt.py:99). Rust
  `lines()` splits only on `\n`.
- **`str.split()`** with no argument (the cron fields) splits on any whitespace run and drops empties.
- `removesuffix` and `removeprefix` remove one occurrence (`parse_remote`: first `/`, then `.git`, in that order).
- `rsplit("!", 1)` for keys; `split("/", 1)[1]` for the default branch.
- **`f"{x:.0f}"`** rounds ties to even. Rust `{:.0}` matched on 0.5, 1.5, 2.5 and -0.3 (→ `0 2 2 -0`, probed both).
  Keep the `-0` output.
- **`{s:24}`** pads to a minimum width in code points, left-aligned. Rust `{:24}` also counts chars, so it matches.
- **`int()`** accepts `+`, `_` and Unicode digits (cron fields, `--timeout`, pid file with surrounding whitespace).
  Rust `parse::<i64>` rejects `_`, Unicode digits and whitespace.
- **`\d`** in Python `re` matches Unicode digits:
  - `MR_REF` `^(?:Review|Merge) [!#](\d+)\b` (cleanup.py:52), `MR_BRANCH` `^mr-(\d+)$`.
  - `parse_remote` `(?::\d+)?`.
  - `interval_s` `\*/(\d+) `.
  - Rust `regex` `\d` is also Unicode by default, but `.parse()` of the match then fails where Python's `int()`
    succeeds.
- **`\b`** in Python is Unicode-aware; Rust `regex` `\b` is Unicode by default too, so this matches.
- **`re.match`** anchors at the start only (`interval_s`, `MR_REF`); `MR_BRANCH` also has `$`. In Python `$` matches
  before a trailing `\n`; in Rust `$` does not.
- **`urllib.parse.quote(s, safe="")`** escapes everything except `A-Za-z0-9_.-~`, `/` → `%2F`, using UTF-8 bytes
  (probed `a%20b%2Fc~d.e_f-g%2Bh%40%C3%A9`). **`urlencode`** uses `quote_plus` (space → `+`). adopt's `gl_all` query
  does not encode ME at all.
- `urllib.parse.urlparse` netloc keeps the port (cleanup's host parsing).
- **`os.path.normpath`** collapses `//`, strips a trailing `/`, and resolves `.` and `..` lexically (cleanup `dedupe`
  key). Rust `Path::components()` does not resolve `..`.
- **Truthiness**:
  - `iid` 0, `linked_mr` 0, `lastActivityAt` 0 (treated as now), `rows_ts` 0 (replaced by now).
  - `a.get("timeout") or 120`: the *string* `"0"` is truthy, giving a 0s timeout and immediate `[timed out]`, 124.
  - `a.get("cwd") or "~"`.
  - `wt.get("ids") or [wt["id"]]`.
  - `remote_name = branch and git(...)` evaluates to `""` when branch is `""`.
  - `bool(m.get("draft"))`.
  - `(p["author"] or {})`.
  - `link.get("number") if isinstance(link, dict) else link` (Orca's linked MR is an int or a `{"number":..}` dict).
- **`dict.fromkeys(list)`** is an ordered dedupe (cleanup `mr_numbers`, auto engine order).
- **`str(KeyError('x'))`** is `"'x'"` with quotes (probed). It shows up in `MR lookup failed: 'weird_state'` and
  `error: 'repoId'`.
- `str(TimeoutExpired)` is `"Command '[...]' timed out after N seconds"`. `str(RuntimeError(msg))` is msg.
- **`"404" in str(e)`** is a substring test on the whole message, including the argv (an MR path containing 404 would
  match).
- **Negative returncode on signal**, printed as `[exit -N]`.
- `subprocess` `text=True` decoding is strict UTF-8 (§0).
- **`sorted(reverse=True)` is stable** (§5). `list.sort(key=...)` is stable (dedupe twins).
- `max(... or 0 ...) or None`.
- `contextlib.redirect_stdout(sys.stderr)` only covers greenlight.ping's `print("skipped: ...")` during adopt apply,
  so it lands on stderr. Other prints in `apply` (none) would be affected the same way.
- `functools.cache` on gh_login lasts for the process.
- `sys.exit(__doc__)` prints the module docstring, including its leading newline handling (it begins `Greenlight's own
  automations: ...`).
- **Exception → traceback, exit 1** (not 2):
  - adopt `apply()` errors after the plan succeeded.
  - auto `opt()` IndexError, bad cron, `load()` JSON error, `show`/`rm` KeyError paths (guarded), `runs` with a blank
    line.
  - cleanup: argparse is fine; scan errors exit 2/1.
- cleanup `Repo` is built through `Repo.__new__` in tests, bypassing `__init__`. Black-box tests need git fakes for
  init (§8).

---

## 7. Suspected bugs (not fixed; preserve or decide)

1. **adopt `parse_remote` eats an all-digit first namespace as a port** (adopt.py:78). `git@gitlab.com:123/repo.git`
   becomes `("gitlab.com", "repo")` (probed): the greedy `(?::\d+)?` consumes `:123`, then `/` acts as the separator.
   The scheme is lowercase-only (`HTTPS://gitlab.com/a/b` → host `HTTPS`).
2. **cleanup has a different, weaker remote parser** (cleanup.py:93-97), so the two scripts disagree on one repo:
   - `ssh://git@gitlab.com:22/a/b` gives host `gitlab.com:22`, reported as `unsupported forge gitlab.com:22`.
   - `https://gitlab.com/a/b.git/` gives project `a/b.git/` (`.git` isn't stripped behind a `/`), so every glab call
     uses a bad pid.
   - `gitlab.com:a/b` and `user@gitlab.com:a/b` give an empty host.
   - `git@host/path` with no `:` raises ValueError *outside* the try. The exception escapes `Repo()` inside the
     executor, **the whole cleanup scan fails, exit 2**.
3. **Cron `*/n` in DOM or DOW counts as restricted**, giving OR semantics, unlike Vixie (§5). `a/n` means a single
   value. The `"7" in dow` check is a substring test. No range validation. Documented as "like cron", but it isn't
   exactly.
4. **`auto.py add` with no name saves under `"null"`.** `add --schedule .. --prompt ..` (name `None`; or name equal to
   a flag token such as `add --schedule "* * * * *" ...`, which makes the name `--schedule`).
5. **auto `execute` crash paths**: a precheck TimeoutExpired, a missing cwd for the precheck, or `load()[name]`
   KeyError crash the exec child. The pid file is left, no runs.jsonl record is written and there is no ping, so the
   failure is silent apart from errors.log.
6. **auto names are unvalidated path components**: `/` crashes `execute` (mkdir without parents). `..` escapes
   `auto/`.
7. **auto `tick`**: one bad schedule (hand-edited file) raises and skips every later automation in that tick.
8. **install templates are unescaped**: `&` or `<` in PATH makes an invalid plist, and a space in exe or path breaks
   `ExecStart`. The systemd unit has no PATH, so prechecks calling `glab`, `gh` or `orca` may not resolve.
9. **launchd StartInterval drift** can skip or double a cron minute (§5).
10. **adopt partial create**: if `orca worktree create` succeeds but a later step fails (`ensure_branch` switch, or
    `--set-upstream-to` when `origin/<b>` is missing), the item is recorded `created=False` and not stored in
    `created` (adopt.py:404-414). The next run inside the last.json freshness window doesn't see it as covered and
    tries `worktree create` again: a duplicate or a name collision.
11. **adopt.py:362 `merge-base --is-ancestor`** inherits stdio (git's errors land on adopt's stderr) and has no
    timeout.
12. **adopt GitHub search has no pagination** (`first: 100`), while GitLab paginates. cleanup's GitLab branch lookup
    reads one page (20) and `gh pr list` defaults to 30.
13. **adopt case-only GitHub mismatch**: `gh_renamed` ignores a name that differs only in case
    (`name.lower() != proj.lower()`), but the filter `c["project"] in repos` is case-sensitive. If the ADE key differs
    from GitHub's `nameWithOwner` only in case, the PR is silently dropped. Low confidence; it depends on what
    canonicalKey looks like.
14. **adopt `removed_by_cleanup` matches on workspace name across all history** (adopt.py:242-250). A reused name
    (e.g. `mr-20` in two projects, or a recycled branch name) mislabels `why`.
15. **adopt `scan` is not read-only** (writes blocks.json and possibly me.json), contrary to its docstring.
16. **adopt `--cap` accepts ≤0** (everything is deferred). `dismiss` accepts any token (`adopt.py dismiss --json`
    tombstones the key `--json`).
17. **adopt clears a re-requested tombstone even when the item isn't created** (approved, busy, no-repo, deferred)
    (adopt.py:294-295, 336). Probably intended, but note it.
18. **cleanup `orca terminal list` has no `--limit`** (cleanup.py:252), while greenlight.scan passes `--limit 10000`.
    If Orca's default limit truncates, active terminals are missed and a workspace in use could be deleted. Only the
    dirty and unpushed guards remain.
19. **cleanup's GitHub iid lookup has no 404 tolerance** (cleanup.py:135), unlike GitLab. A `Review #N` comment that
    names a PR in another repo gives `MR lookup failed` and a skip (fails safe).
20. **cleanup "approved by me" has no source-branch guard** (cleanup.py:128-129), unlike "not mine to approve". A
    workspace where I code on someone else's branch that I approved becomes a candidate (still subject to the
    nothing-lost guards).
21. **cleanup Repo init mutates repos** (`fetch --prune`, `remote set-head --auto`) even for `scan` and `precheck`.
22. **cleanup is unaware of unknown GitLab states**: an unknown `state` gives KeyError, shown as `MR lookup failed:
    'x'`.
23. **auto `alive()`**: pid reuse gives a false "still running"; pid 0 or negative reads as alive.
24. **auto agent timeout** kills only the direct child (no process-group kill), so agent subprocesses are orphaned.
25. **auto `runs`** crashes on a blank or corrupt line in runs.jsonl.
26. **iso_ts ignores timezone offsets** (adopt.py:182-183). It is fine for GitLab/GitHub `Z` timestamps and wrong
    otherwise.
27. **adopt apply**: an exception in `log()` or `note()` after a successful create marks the item failed, but `created`
    already holds it (adopt.py:408-413). Minor.

---

## 8. Scenario seeds (tests → black-box fixtures)

**None of the three test files invoke a CLI entry point.** Every test patches Python functions at module boundaries:

- `greenlight.gl`, `gl_all`, `gh`, `orca`
- `adopt.repo_map`, `adopt.checked_out`, `adopt.git`, `adopt.create`
- `blocking.blocks`
- `cleanup.run`, `cleanup.orca`, `cleanup.Repo`, `os.path.isdir`
- `auto.agent`, plus module globals (`auto.STATE/FILE/DIR`, `adopt.DISMISSED/STATE_FILE/LOG`, `greenlight.STATE`)

To become black-box fixtures, each mock maps to a fake CLI on PATH:

| Mock | Process-boundary fake |
|---|---|
| `greenlight.gl(path)` / `gl_all` | `glab api <path>`. `gl_all` appends `&per_page=100&page=N`, and the fake must return fewer than 100 items to stop paging. Avoid error text matching `TRANSIENT_RE` (`50[0234]`, `timed out`, etc.), or expect 2s+4s of retries. |
| `greenlight.gh(*a)` | `gh api graphql -f q=... -f query=...`, `gh api repos/<p>`, `gh api user`, `gh pr view N -R p --json latestReviews\|reviewRequests` |
| `greenlight.orca` / `cleanup.orca` | an `orca` exe (or `$ORCA_CLI_COMMAND`) printing `{"ok":true,"result":...}`. argv ends with `--json`. |
| `adopt.repo_map` | `orca repo list` plus `git remote get-url origin` |
| `adopt.checked_out` | `git worktree list --porcelain` |
| `blocking.blocks` | a pre-written `STATE/blocks.json` `{"ts": <now>, "blocks": {...}}` (fresh, within 300s) **plus** config.json `blocking.repos` non-empty (otherwise blocks() returns `{}`). `puck=None` (scan failed) needs a stale/absent blocks.json and a glab fake that fails blocking.py's own `open_mrs` queries (see the blocking inventory). |
| `cleanup.run` | fake `git` and `glab` answering the World tables |
| `cleanup.Repo` (built with `__new__`) | the fake git must also answer `remote get-url origin` (e.g. `git@gitlab.com:acme/widgets.git`), `fetch --quiet --prune origin`, and `symbolic-ref --short refs/remotes/origin/HEAD` → `origin/develop` |
| `os.path.isdir` | real directories for each worktree path |
| `auto.agent` | fake `claude` / `codex` executables on PATH (found via `shutil.which` in harness.binary). The prompt arrives on stdin. |

**Always set `GREENLIGHT_HOME` (temp dir) and `GREENLIGHT_ME=octo`** (otherwise `whoami()` shells out at import).

**Time.** Tests pin `NOW = 2_000_000_000` (2033-05-18) by passing `now=` and `rows_ts=` into `plan()`. The CLI path
uses the real clock, `rows_ts = last.json.ts` and `cleanup.NOW_MS`. Black-box fixtures need either data relative to
the real clock, or a Rust-only time override (e.g. a `GREENLIGHT_NOW` env, a new testing affordance that doesn't
exist in Python). auto cron tests need a local-time override and a fixed `TZ`.

### tests/test_adopt.py (289 lines)

| Lines | Scenario | Canned data | CLI-reachable? |
|---|---|---|---|
| 2-17 | env setup: GREENLIGHT_HOME, GREENLIGHT_ME=octo; redirects STATE, DISMISSED, STATE_FILE, LOG to a temp dir; NOW=2e9 | | |
| 19-26 | `parse_remote`: canonicalKey without a scheme, scp-ssh, https, `oauth2:tok@` with trailing `/`, `ssh://...:22`, `""` → `(None, None)` | inline | **pure function only** |
| 28-38 | `repo_map`: canonicalKey vs missing identity (origin URL via git) vs GitHub key | REPOS 28-32 (`orca repo list`), origin map 33, fake_git 34 | via `orca repo list` + `git remote get-url` fakes |
| 42-45 | `m()` GitLab MR builder: iid, project_id 7, `t{iid}`, `b{iid}`, `sha{iid}`, `references.full`, web_url, `updated_at` default `2033-05-18T03:00:00Z` | | |
| 48-71 | `run_plan` harness. `gl_all` dispatches on `author_username` / `reviewer_username` (53). `gl` serves the single-MR lookup (57-62, 404 RuntimeError when unknown, default state opened), `/approvals` (63-64, `approved` and `cant` sets), `/reviewers` (65, default state "reviewed"). Mocks `repo_map` = `{acme/widgets: r-tn /w/widgets gitlab}` (67), `checked_out` = busy (68), `blocking.blocks` = puck (69-70). Calls `plan(rows, NOW-60, cap, now=NOW)`. | 48-71 | needs last.json (ts) for rows |
| 74-79 | helpers: `verdicts` normalizes reasons (split on `:` and ` (`); `row()` builds a greenlight row | | |
| 81-83 | dedupe: the same MR as author and reviewer appears once, as author | | yes |
| 85-115 | **main verdict matrix**. Dismissed seeds 85-88 (incl. `!99` closed, pruned). Covered by MR (1) or by branch (2); own draft adopted (3); approved (4); other's draft (5); tombstoned and unchanged (6); re-requested and changed (7, cleared); no-repo ×2 grouped (8, 11); busy source branch (9); own MR stays tombstoned (10); busy `mr-12` (12). Expected dismissed {6, 10}; pings `@no-repo:acme/no-repo` then `@adopt-busy`; text contains `!8 !11`; last line `Options: `; `seen == {acme/widgets!1}`. | 89-96 | yes |
| 117-121 | cap: 12 creatable, 10 create + 2 deferred; `cap=1` gives 1 | | yes (`--cap 1`) |
| 123-132 | tombstones from `seen`: gone and open → manual; still there → covered; gone by cleanup (log.jsonl line `{"name":"ws4","removed":true}` at 128) → cleanup; merged → none | adopt.json seed 124-127 | yes |
| 134-138 | `created` newer than `rows_ts` gives skip; older gives create | adopt.json seeds 135, 137 | yes (relative to last.json ts) |
| 140-171 | **blocking**: puck map 148-150, `mrs` single lookups 151-153 (22 merged; 25 missing → 404), approved {3, 23, 28}, covered branch `cov`. Verdicts 155-166. Looked-up set exactly [20..28] (167; 1 and 2 not refetched). `block` key on 1, absent on 3, reason suffix on 20; name `mr-20`; `create_args` comment `Review !20: t20` (170); dismissed {26}; 28 cleared (171). | 143-154 | yes, with blocks.json + config.blocking |
| 173-187 | blocking scan failed (`puck=None`): author/reviewer only, no single lookups; dismissed and seen kept for unseen keys. Then the scan is back: 21 tombstoned, 20 covered, 26 tombstoned; seen = {20}. | adopt.json 174-175 | yes (hard: needs the blocking scan to fail) |
| 189-202 | `create_args` exact argv for author/review, Orca vs `GREENLIGHT_ADE=cmux` (no `id:`, no MR flag, base = sha) | inline | **pure function**; reachable through apply with a fake orca recording argv |
| 205-217 | `create()` git call sequence: author `fetch origin me/x`; review Orca `fetch origin +refs/merge-requests/6/head:mr-6`; `GREENLIGHT_ADE=t3` gives `fetch origin refs/merge-requests/6/head`, base `abc`. orca returns `{"worktree": {}}` (no path, so no ensure_branch). | 206 | via apply with fake git/orca logging argv (t3 also needs the t3 shim's own CLI) |
| 219-235 | **ensure_branch on real git** (bare origin, clone, worktree `x-2`). It ends on `x` tracking `origin/x` and `x-2` is deleted. Review: `mr-6` exists, worktree `mr-6-2` ends on `mr-6`. | real repos under the temp dir | keep as a real-git integration test; no CLI entry |
| 237-245 | `apply()` twice with the same `@` pings: the outbox gets each key once (FLAP); dismissed.json and adopt.json `seen` written | plan dict 238-239 | via `adopt.py apply` twice with fakes |
| 247-253 | apply with a create: log.jsonl last `did == "adopt"`; `history/me__x.md` contains `adopted !5 (author)`; `created` has the key | `create` mocked 249 | yes, with fake git/orca |
| 254-256 | `user_can_approve=false`: skipped `I can't approve it`, unless blocked (discussion) | | yes |
| 260-274 | GitHub-only ADE: glab must never be called (`gl_all` raises AssertionError); fork PR `x/fork` dropped without a ping; create name `b5`, args include `Merge #5: t5` and `--pr 5` | `pr()` 261-263, gh fake 264-265 | yes: fake gh, glab that fails loudly |
| 276-288 | renamed GitHub repo: search reports `new/r`, ADE has `old/r`; `gh api repos/old/r` → `new/r`; PR 7 covered by row `linked_mr: 7`, PR 8 created on `/w/r`, no pings | gh_ren 278-282 | yes |

### tests/test_cleanup.py (217 lines)

| Lines | Scenario | Canned data | CLI-reachable? |
|---|---|---|---|
| 5-18 | env; `blocking.FILE` redirected to an empty temp (so `cached()` gives None → nothing blocked) | | set GREENLIGHT_HOME |
| 21-22 | `mr()` builder (state default merged, sha `head{iid}`, author ME) | | |
| 25-70 | **World fake**. glab: `projects/<pid>/merge_requests?source_branch=` → MRs sorted -iid (46-47); `/<iid>` (52, unknown → `RuntimeError("... 404 Not Found")`); `/approvals` (53-55); `/versions` (56). git by subcommand: `config` (merge, rc 1 when unset), `rev-parse` (tip), `status` (` M x.go` when dirty), `rev-list` (tip when unpushed), `merge-base` (a == b, `ancestors` pairs, or `a=="base" and b=="origin/develop"`). | 25-70 | fake `glab` and `git` on PATH implementing these tables |
| 73-76 | `repo()` bypasses `__init__`: host gitlab.com, base `origin/develop`, default `develop` | | fake git for init (§8 table) |
| 79-96 | `evaluate()` and `scan()` harnesses. Fake orca: `worktree list`, `terminal list` (busy ids with `lastOutputAt = NOW_MS`), `repo list` (`{"id":"r","path":"/r"}`) (89-91) | | yes (`cleanup.py scan --json`) |
| 99-122 | **twin dedupe**: linked entry plus a `path/` twin with null link. MR 5302 merged (tip == sha) vs older 5290 open gives `MR !5302 merged (older !5290 open, stale)`. Stale link to 5290 still a candidate. One row per path, keeping id `a`. Busy on twin `b` → skip. Pin on twin → keep. `dedupe` ids `[a, b]`; a single entry passes through. | 102-106 | yes |
| 124-127 | a newer open MR on the branch: `keep`, `MR !5310 is open` | 125-126 | yes |
| 129-144 | GitLab rebased and deleted the branch: the tip is an earlier pushed version → candidate `MR !78 merged`; tip behind an older version → candidate; never pushed → skip with exact reason; versions **not fetched** when the cheap check passes (asserts no `versions` call) | 131-143 | yes (fake glab call log) |
| 146-183 | **review checkouts**: approved by me via comment-only detached (1766), `mr-N` branch (1768), Orca link (1791); merged on an old push (1785, versions); not yet approved (1792) → keep; other project (151); `cant` (196): review checkout `not mine to approve`, but a workspace on the source branch → keep | 148-154, 172, 177 | yes |
| 185-204 | guards: dirty, unpushed, busy terminal, pinned, main checkout; detached with no MR makes **zero calls** (196-197); comment naming a missing MR → `detached HEAD, its MR wasn't found`; no-MR branch not merged; default branch | 186-204 | yes |
| 207-216 | GitHub `Repo.mrs`: `gh pr view` per number, `gh api user` → `gh-me`; result `[("#8","open"), ("#7","approved by me")]`; `mr_numbers({"comment":"Review #7: x"}, "mr-7")` → `[7]` | prs 209-211, fake 212-213 | `mrs`: yes via the GitHub repo path; `mr_numbers`: **pure** |

### tests/test_auto.py (29 lines)

| Lines | Scenario | CLI-reachable? |
|---|---|---|
| 6-12 | `due()`: `*/30` at :30 vs :31; `30 7 * * 1` on Monday; `1-5` weekday vs Saturday; DOW `7` and `0` both Sunday; `0 0 1 * 5`, OR rule on Friday 23rd; `15,45 8-10 * 10 *` | **pure function only** (`tick` uses the real localtime). Add Rust unit tests; for black-box, `add` exercises parse validity only. Extra probes worth encoding: `0 0 */1 * 2` on a Monday is True; `5/10` → {5}; `1-5-9` → 1..5; `*/0`, `MON`, 4 or 6 fields → error. |
| 15-17 | `execute`: precheck `exit 1` gives a runs.jsonl record with status `skipped` | yes: `auto.py exec x` (hidden) with automations.json seeded |
| 18-24 | `agent` monkeypatched: claude returns `You've hit your usage limit` with exit 1, codex returns `done` exit 0. Expect status `ok`, engine `codex`, calls `[claude, codex]`; pid file removed (`alive` False) | yes: fake `claude` and `codex` on PATH. The real argv: `claude -p --permission-mode acceptEdits` and `codex exec --skip-git-repo-check -c sandbox_mode=workspace-write -`, prompt on stdin, cwd `~`. Default config agents `["claude","codex"]`. |
| 26-28 | PLIST template parses with plistlib; ProgramArguments `[exe, path, "tick"]`; StartInterval 60 | template-only. Black-box `install` would run launchctl/systemctl; fake those on PATH and point HOME at a temp dir. |

**Not covered by any test** (add fixtures):
- adopt: text output, `dismiss`, `precheck` exit codes, `interval_s`/last.json freshness, the cleared-tombstone save
  path.
- cleanup: `apply` (orca rm, log.jsonl), text output, `Repo.__init__` URL parsing, `scan failed` exits.
- auto: `list`, `add`, `edit`, `show`, `rm`, `run`, `runs`, `tick`, `install`, the failed-run ping, timeout 124,
  missing binary 127.
- sync.sh: nothing.

**Cross-module consumers to keep compatible**:
- `greenlight.py precheck` imports `adopt` and calls `adopt.plan(res["rows"], res.get("ts"))["create"]`
  (greenlight.py:887-890).
- The Swift app's `auto.py show|edit greenlight` (app/Greenlight.swift:816,833).
- adopt reads cleanup's log.jsonl `name` and `removed` fields, and auto's `automations.json["greenlight"].schedule`.
