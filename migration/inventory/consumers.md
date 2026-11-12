# Consumers of scripts/*.py: the contract the Rust port must keep

**Found:** The app (app/Greenlight.swift) calls 4 of the 13 scripts (dash.py, chat.py, auto.py, greenlight.py) over a shell or ssh, about 25 call shapes in all. It reads their stdout as raw JS source or as line-delimited JSON, ignores stderr, and checks the exit code only in a few places. Nothing the app parses depends on key order or whitespace. What it does depend on: exact enum strings, `null` versus a missing key in a few places, one JSON object per line on the streams, an append-only `outbox.jsonl`, and stdout that begins with `{`.

**Needs approval or a decision:**
- The binary's install path and how it is invoked. Every Swift call site concatenates `"\(py)/<script>.py"`, so all of them change at once (§4).
- Existing hosts have persisted Python state that cutover must migrate: the launchd plist or systemd unit (`python3 auto.py tick`), `next.cmd` argv in last.json, and user automations' `--precheck` strings.
- The golden harness (migration/harness.sh) diffs stdout byte for byte, which is stricter than any consumer. Rust either matches Python's `json.dumps` format (`", "`/`": "` separators, `\uXXXX` escapes) or the harness normalizes JSON.

**Unsure:** The page field maps in §2c and §2d come from two parallel full reads. I spot-checked only `mr.role`: dashboard.html:1280 tests `"reviewer"`, which dash.py never emits, so that is an existing bug. The chat event schema was taken from the Swift parser and chat.py `emit()`, not from a live run.

The Rust binary is invoked as `greenlight <script-without-.py> <args>` (migration/harness.sh:46). That is assumed below wherever a command shape is given.

---

## 0. The transport (one helper for every call)

`ssh()` lives at app/Greenlight.swift:420-455, with `remoteArgv` at 27-30.

- **Command string.** `py` is `GREENLIGHT_HOME=<greenlightHome> python3 <greenlightCode>/scripts` (23). Callers append `/dash.py ...`. `chatCmd` (25) prefixes `CHAT_DIR=<home>/chat-test` under `--selftest`.
- **Where it runs.**
  - On a local host: `$SHELL -lc <cmd>`. A login shell, so PATH has glab, gh and orca.
  - Otherwise: `/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 <host> <cmd>`.
  - Exit code 255 or -1 means unreachable (764).
- **stdin.** The given string, possibly empty, is written in full and then closed. The binary always sees EOF and must not wait for a TTY.
- **stderr.** Discarded (`FileHandle.nullDevice`, 432). Anything the binary prints there is invisible to the app, so errors the app should show belong in stdout JSON.
- **stdout.**
  - Split on byte `\n`. Each line goes to `onLine` on the main thread. The remainder is flushed at exit, split on `\n` (447).
  - A non-UTF-8 byte is decoded lossily.
- **Two consumption styles:**
  - **Concatenate** (`out += $0`, which drops the newlines). Then `out.hasPrefix("{") ? out : <synthesized {"name"|"op"|"path":..., "error":"<host> didn't answer (exit N)"}>`, which is injected as JS source: `evaluateJavaScript("mc.x(\(js))")`.
    - Output must start with `{` exactly: no BOM, no leading space, no banner line.
    - It must be one JS-parsable expression. JSON qualifies, and pretty-printed JSON is fine too because the lines are concatenated.
    - No other stdout line may appear (no progress text, no log line).
    - Never emit NaN or Infinity. serde_json can't; Python could in theory.
    - Raw UTF-8 is fine: JSON is valid JS since ES2019, including U+2028.
  - **Line stream** (`dash.py stream`, `chat.py send/compact`, the outbox tail). Each line is one complete JSON object, flushed as soon as it is written. Pretty-printing breaks these.

The exit code is consumed only where listed below. Everywhere else it appears only inside the synthesized error text.

## 1. Every invocation

The `Swift` column gives line numbers in app/Greenlight.swift. `dash` means `dash.py`, and so on.

### 1a. Line streams

| Swift | Command | stdin | Output consumed | Exit code |
|---|---|---|---|---|
| 2938-2945 streamDash | `dash.py stream 5` | none | Every line that starts with `{` is stored as `dashSnap`, broadcast as `mc.update(line)` to every dashboard webview, and passed to `queueBuilds` | Ignored. On any exit, restart after 10s. The process must run forever and must not exit on a transient error. dash.py:1036-1043 prints `{"error": "..."}` and keeps looping |
| 1000 chat send | `chat.py send --model M --effort E --name '<name>'` | the message payload | Event lines (§2b). The turn ends at the first `done` | No `done` before exit gives "Lost the connection to <host> (exit N)", or "Stopped." if the user stopped it |
| 1005 compact | `chat.py compact --model M` | `""` | Same events | Same |
| 1093-1100 outbox tail | Swift-built shell: `f=<home>/outbox.jsonl; touch $f; n=<seen or $(wc -l < $f)>; echo "#start $n"; tail -n +$((n+1)) -F $f` | none | `#start N` sets the cursor. Every other line advances the cursor by 1 and is parsed as a ping (§2d) | Reconnects after 15s. **No script is involved, but the file format is the contract** |

### 1b. One-shot JSON (concatenate, then inject)

| Swift | Command | stdin | Injected as | Notes |
|---|---|---|---|---|
| 2985-2995 suggest | `dash.py suggest '<name>' --model M --effort E` | none | `mc.suggest(js)` on all dashboards and `term.suggest(js)` on all terminals | Falls back to `{"name","error"}` |
| 3054-3062 page | `dash.py page '<name>' [--fresh] --model M --effort E` | none | `mc.page(js)` | A facts error in dash.py raises `SystemExit(json)`: the message goes to stderr with exit 1 and stdout is empty, so Swift synthesizes the error. Rust may simply print `{"name","error"}` to stdout instead |
| 3064-3075 ask | `dash.py ask '<name>' --model M --effort E` | none | `mc.asked(js)` | Swift also re-encodes the result with `.sortedKeys` for the bell |
| 3064-3075 answer | `dash.py answer '<name>'` | the answer text (dash.py keeps 4000 chars) | `mc.answered(js)` | |
| 3076-3081 park | `dash.py park '<name>' on\|off` | none | `mc.parked(js)` | |
| 3083-3093 task | `dash.py task repos\|new '<arg>'` | `new`: JSON `{text, files:[{name, data:<dataURL>}]}`, up to 20 MB | `mc.tasked(js)` | |
| 3095-3101 merge | `dash.py tree merge '<path>'` | none | `mc.merged(js)` | |
| 3108-3114 run | `dash.py run '<name>'` | the prompt (dash.py keeps 8000 chars) | `mc.ran(js)` | |
| 3486-3495 pushTrees | `dash.py trees` | none | `term.workspaces(out)` on every terminal webview, only if it starts with `{` | Exit ignored. A failure keeps the old sidebar |
| 3555-3565 tree op | `dash.py tree info\|rm\|goal\|cfg\|merge '<path>' [--force]` | `goal` and `cfg`: text up to 8192 chars | `term.tree(js)`. For any op other than `info`, the trees are pushed again | |

### 1c. JSONSerialization in Swift

| Swift | Command | stdin | Read |
|---|---|---|---|
| 187-191 RulesStore.call | `dash.py task <op> '<arg>'` | optional | Parsed to `[String:Any]`. Falls back to `{"error"}`. If `j["error"]` is a String it is shown as status; otherwise `done(j)` runs. See §2a for each op |
| 813-828 loadGreenlightEvery | `auto.py show greenlight` | none | Requires exit 0, then reads one JSON dict: `schedule` (String; `*/N ...` is parsed for N) and `enabled` (Bool, defaults to true when missing). auto.py:188 prints one line, or `sys.exit(msg)`, which exits 1 when the automation is missing |
| 2951-2965 queueBuilds | No call: parses each stream line | | `pucks[]`: `name`, `sig` (String), `built` (**must be JSON `false`** to trigger a build), `state` (`!= "done"`) |
| 2973-2982 queueSuggests | Same | | `suggestion` **must be JSON `null`** to trigger a suggest (`is NSNull`). A missing key never triggers one. Also reads `state != "done"` and `agent.state` not in `working`/`waiting` |

### 1d. Exit code only (stdout ignored)

| Swift | Command | Meaning |
|---|---|---|
| 830-835 setGreenlightEvery | `auto.py edit greenlight --disabled` or `--enabled --schedule '*/M * * * *'` | 0 means saved |
| 2930-2936 rescan | `greenlight.py scan >/dev/null` | `mc.rescanned(code)`. Nonzero means failure. The new snapshot arrives through the stream: snapshot `scanned` must equal last.json `ts` |
| 2959 build | `dash.py page '<name>' --model --effort >/dev/null` | None. Exit just frees the slot; the result lands in the page cache that `snapshot.built` reflects |
| 1011 clear | `chat.py new` | None. Selftest (4094-4097) runs `ls <home>/chat-test` afterwards and requires no filename containing `session` |
| 1022 stop | `chat.py stop` | None. It must kill the running `send` (chat.py kills the pgid in `DIR/pid`); that `send` then emits `done ok=false "Stopped"` |
| 757-765 testHost | `command -v python3 >/dev/null \|\| { echo nopy; exit 3; }; test -f <code>/scripts/greenlight.py && echo ok` | Last stdout line `ok` or `nopy`. Exit 255 or -1 means unreachable. **Python-specific: rewrite as a test for the binary** |

### 1e. Not called by the app

- `dash.py snapshot` and `dash.py transcript`: no app or page caller (grep shows none). `transcript` has no consumer at all.
- `chat.py bg` and `chat.py bgstop`.
- `greenlight.py` other than `scan`; all of `adopt.py`, `cleanup.py` and `blocking.py`; `auto.py` other than `show`/`edit`.
- These are consumed by the agent through SKILL.md (§4) and by the launchd or systemd timer (`auto.py tick`).
- **Script to script:**
  - auto.py:127 respawns itself (`sys.executable __file__ exec <name>`).
  - dash.py imports greenlight.py.
  - dash.py:168-169 runs `next.cmd` from last.json, and checks `cmd[0] == "gh"`.
  - dash.py:134 looks for `"signoff"` in the `next.cmd` argv.
  - greenlight.py:510 emits `["python3", ".../greenlight.py", "signoff", proj, iid, step]` as `next.cmd`. SKILL.md:33 tells the agent to "run `next.cmd` exactly".

## 2. JSON fields read

### 2a. Swift-decoded

**`dash.py task` ops (RulesStore, 187-240)**
- **Every reply:** `j["error"] as? String` means failure.
- **`repos -`:** `repos[].path`, String. The last path segment is used (197).
- **`instructions-get -`:** `text`, String.
- **`memory-list -`:** `notes as? [[String:String]]`, each with `name` and `text` (238).
  - Every value must be a string, or the whole cast fails and the list is empty.
- **`lifecycle-get <repo>` → `got()` (228-233):**
  - `stages as? [[String:String]]`, each with `key`, `req` and `done`. **Any non-string value, null included, empties the whole list**, so `req` must be `""` rather than null.
  - `options as? [String:String]`, `rules` (String), `intro` (String).
  - `source`: one of `"yours"`, `"default"`, `"bundled"`. The exact strings are shown at 236.
  - `builtins as? [String:String]`.
  - `file`: ignored.
- **`autorun-get` / `autorun-set`** (stdin `{kind:int}`) (206-209):
  - `kinds[]`, each `{kind: String, when: String, gates: [String]}`.
  - `on as? [String:Int]`.
- **`prompt-get` / `prompt-set "<repo>/<kind>"`** (218-223):
  - `kind` must equal the requested kind; otherwise the reply is ignored.
  - `text`, `default` (String); `own` (Bool); `vars` (`[String]`).
- **No fields read:** `lifecycle-set` (stdin `{stages,options,rules,intro}`), `lifecycle-reset`, `instructions-set` (stdin text), `memory-set <note>` (stdin), `memory-delete`. Only `error` and the status matter.
- **Validation:** dash.py checks `<arg>` against `^[\w.@+/-]{1,200}$` (dash.py:42, 1076).

**`auto.py show greenlight`**
- `schedule`: String.
- `enabled`: Bool, may be missing.
- All other keys are ignored.

### 2b. chat.py event lines (1034-1066)

Each line is one JSON object with an `"e"` String. Lines that are not JSON, or lack `e`, are ignored.

| `e` | Fields read | Notes |
|---|---|---|
| `start` | `engine` | Matched against the model list |
| `status` | `text` | |
| `step` | `id`, `title`, `detail` | |
| `step_done` | `id`, `ok` (defaults to true), `out` | |
| `text_reset` | none | |
| `text` | `delta` | |
| `usage` | `context` (Int > 0), `tps` (Double) | |
| `done` | `context` (Int), `window` (Int > 0; null is OK), `text`, `ok` (defaults to **false**) | The first `done` ends the turn. `text` goes through `splitOptions` (507-516): a trailing `Options: a \| b` line becomes buttons |
| (any) | `model`, `ms` | Ignored |

**Selftest asserts:**
- A reply arrives.
- `Options:` buttons parse.
- `context > 0` and `window > 0`.
- `compact` lowers `context`.
- A codex reply works.
- Clear leaves no `session*` file.

**Session state:** session files are `DIR/session` and `DIR/session_<harness>`. `DIR` defaults to `$GREENLIGHT_HOME/chat`, and `CHAT_DIR` overrides it.

**Text the model sees:**
- The system prompt (chat.py:24) embeds the absolute path of `SKILL.md`, computed from `__file__` (two directories up from the script). The binary must point at the same SKILL.md.
- chat.py:32 tells the model to run `greenlight.py note <ws> "..."`. This is Python-specific text (§4).

### 2c. dashboard.html (`mc.*`)

All references are app/dashboard.html line numbers.

**Things that crash the page** (every pane goes blank):
- **`state` outside the 7 keys of `STATE`:** `needs`, `crashed`, `raised`, `working`, `queued`, `quiet`, `done` (601-609). It is read as `STATE[p.state].label` with no guard.
- **`agent` not an object:** `p.agent.state` is read unguarded (973, 1145, ...). Every puck needs an `agent` object.
- **`project` null:** `.split("/")` at 1113.
- **`name` not a string:** `.split` at 841-842.
- **A `moves[].kind` outside `merge|review|threads|ci|conflicts`:** 1538 and 1545.
- **A page reply with neither `facts` nor `error`:** `r.facts.*` at 1228, 1285 and 1312. Also a `facts.puck` that has no `agent` object (1228).

**`mc.update(snap)`** (1945-1960)

Top level:
- `error`: if truthy, the whole update is ignored.
- `pucks`: an array, **in display order**.
- `scanned`: **epoch seconds**. It must be stable between updates: `scanned !== ui.rescan` ends the "Refreshing…" state (1954).
- `ts`: **epoch seconds**. "Live" means it is less than 45s old.
- `trees`: never read by the dashboard.

Each puck:
- **`name`, `path`:** Strings. Byte-exact keys.
- **`goal`, `why`, `action`:** Strings. `why` has `.replace` called on it.
- **`state`:** the enum above.
- **`progress`:** 0..1 or null.
- **`steps`:** `[String]` or null. Known names: `mr_open`, `ci_green`, `no_conflicts`, `review_bot`, `milestone`, `ready`, `threads`, `approved`, `merge`, `review`, `freeform`, `done`, `error`, `parked`. Unknown names are shown raw.
- **`step_index`:** **an integer** or null. Null means all steps are done.
- **`step`:** String, from the same names.
- **`owner`:** only part of a key.
- **`hidden`, `built`:** Bools.
- **`sig`:** String. A `suggest` reply's `sig` must equal it, or the page asks again on every hover.
- **`suggestion`:**
  - **`null` or `{label, prompt}`.** `{}` is truthy and leaves the button stuck on "Please wait…" for good (1951, 1568).
  - It is spread over `{name, sig}`, so it must not carry its own `name` or `sig`.
- **`branch`:** String.
- **`moves`:** read only when `mr.state === "opened"`. `mv[0]` is the main button. Uses `kind`, `prompt`, `repo`, `n`, `jobs`, `how`, `target`. `own`, `sha` and `auto` are not read here.
- **Never read on the snapshot puck:** `changed`, `blocking`, `puck`.

`agent`:
- **`state`:** `working` and `waiting` are special; anything else counts as idle.
- **`type`:** truthy means there is an agent; also tested against `/codex/i`.
- **`last`, `task`, `tool`, `input`:** Strings.
- **`since`:** a number (below 1e12 means seconds, otherwise ms) or an ISO string. **It must be stable**, because a change re-fires `ask` (key at 1336).
- **`idle_h`:** hours, a float or null.

`mr`:
- **`{}`, null and missing are all equivalent**: every read is guarded by `mr && mr.iid`.
- **Fields read:** `iid`, `ref`, `forge` (`"github"` versus anything else), `web_url`, `title`, `draft`, `author`, `source_branch`, `target_branch`, and `approved_by` (`[String]`).
- **`state`:** `opened` and `merged` are special.
- **`role`:** dash.py and adopt.py emit `"author"` or `"review"`. 973 and 1138 test `"review"`; **1280 tests `"reviewer"`, which never matches** (an existing page bug; keep emitting `"review"`).
- **`pipeline`:** `failed`, `success`, `running`, `pending`, `created`, `preparing`, `waiting_for_resource`, `canceled`, `skipped`, `manual`.
- **Numbers, not strings:** `threads` (`=== 1` at 1123), `threads_total`, `approvals`, `approvals_required`.
- **`conflicts`, `mergeable`:** Bools.
- **Never read here:** `labels`, `milestone`, `pipeline_url`.

**`mc.page(r)`** (1972-1978)
- **`name`:** must equal the open page, or the reply is dropped.
- **`error`:**
  - With no `facts`, the page shows a stub.
  - With `facts`, the error is shown as the Insights header.
- **`facts.history[]`:**
  - `ts` is a **String of the form `"YYYY-MM-DD HH:MM"` in local time**. It is parsed with `slice(0,16)` and string-sorted together with the commits.
  - `text` is matched against `/\bunchanged\b|no action|SKIPPED again/i`.
  - `state` is not read.
- **`facts.git.commits[]`:** `ts` (same format) and `subject`. `git.files` is not read.
- **`facts.mr_live`:** `|| {}`, so null, missing and `{}` are all fine.
  - `jobs[].status`: `success`, `failed`, `running`, `pending`.
  - `conflicts` and `unresolved`: `!= null` overrides the snapshot value.
  - `approved_by`: an array or null. A non-array truthy value throws at 1276.
  - `milestone` (String), `pipeline_url`, `labels`, `error`.
  - **Not read:** `pipeline`, `updated`.
- **`designed`:** **epoch seconds**.
- **`layout`:**
  - `tiles` is an array, and the first 3 valid tiles are used, so order matters.
  - `wide` is `{widget, props, title}`.
  - Widget names must be keys of `W` (674-764); unknown names are dropped.
  - Tone values: `ok`, `warn`, `bad`, `info`, `idle`.
- **Not read:** `v`, `sig`, `layout.puck`.

**`mc.suggest(r)`** (1935-1937)
- Reads `name`, `error`, `prompt` (truthy means ready), `label`, and `sig` (must equal the puck's `sig`).

**`mc.asked(r)`** (1961-1964)
- **`question`:** String; falls back to `agent.last`.
- **`kind`:** `"menu"` sends `options[].key`. Otherwise the page sends `options[].label`.
- **`options[]`:** `{key, label, detail?}`, in order.
- **`error`:** read.
- **`questions`:** never read.

**`mc.answered(r)`, `mc.parked(r)`**
- Only `name` and `error` are read. A falsy `error` means success.
- `ok`, `goal` and `agent` are never read.

**`mc.ran(r)`** (1939)
- **`ok` must be truthy on success.** Without it the page logs a failure and keeps the old suggestion.

**`mc.tasked(r)`** (1855-1869)
- **`op`:** `=== "repos"` is exact; any other value is treated as `new`.
- **For `repos`:** `repos[].name` (the option value) and `repos[].path`. `host` is never read.
- **For `new`:**
  - `error` is matched against `/workspace (\S+) already exists/` (1831, 1865). **Keep that wording.**
  - Also reads `name` and `repo || arg`. `ok` is not read.

**`mc.merged(o)`** (1558-1563)
- `path` must match the puck's path exactly. Also reads `error`, `ref`, `target`, `how`.
- `op` and `ok` are not read.

**Swift-built arguments, not script output:** `mc.bell(n, open, asked[])` (`asked[].id` must be a **string**), `mc.pickProjects`, `mc.builds`, `mc.rescanned(code)`. They are listed only because `bell.asked[].ts` and `ws` come from outbox pings (§2e).

### 2d. terminal.html (`term.*`)

All references are app/terminal.html line numbers.

**Injection:** `term.tree()` and `term.suggest()` with no argument throw (`o.op`, `o.name`). Only `term.workspaces` tolerates an empty reply (`o?.trees || []`, 1516).

**`dash.py trees` → `{trees:[...]}`**
- **Array order is the contract:**
  - Stable sort, so the order within a group is emission order (905).
  - Project group order is first occurrence (908).
  - `p.home` is the first `main` tree with truthy `cfg` (850).
  - `wsInfo` is the first matching path (818).
  - autoRun walks trees in order (1530).
- **`path`:**
  - The `===` key everywhere, so tree-op replies must echo the request path byte for byte.
  - No trailing slash: `under()` uses `startsWith(d + "/")` (855).
- **`name`:** String. When falsy, the page falls back to the last segment of `path`.
- **`project`:** String; `""` is allowed. **It must never be null on a `main` tree**, because 1073 and 1082 call `home?.project.split("/")`.
- **`ade`:** truthy. `orca`, `cmux`, `herdr` and `t3` get pretty labels.
- **`branch`:** String or null.
- **`main`:** Bool.
- **`agent`:** an exact string from `working`, `waiting`, `done`, `idle`, `crashed`, `error`, used verbatim as a CSS class. No spaces; an unknown value gets no pill.
- **`goal`:** String; null, missing and `""` are equivalent.
- **`active`, `since`, `created`:** **epoch milliseconds** (`dur(ms)`, 603), or null. Seconds would render as a huge age.
- **`cfg`:**
  - **Absent or null when there is none. Never `{}`**, which is truthy and wrongly makes that tree the project home (850).
  - Passed through opaquely: the page writes back `{...cfg, name, look}` (1134), so unknown keys must round-trip. Don't parse it into a typed struct that drops fields.
- **`puck`:** String or missing. `w.puck || w.name` must match `^[\w.@+/-]{1,200}$` (684).
- **`state`:**
  - Exact strings: `crashed`, `needs`, `raised`, `quiet`, `done`.
  - Anything else, or missing, goes to the "Workspaces" section.
- **`why`:** String when truthy. `.replace` is called on it (933).
- **`moves`:** may be missing, null or `[]`.
- **`pushed`:** String or null; compared to `mr.sha`.
- **`mr`:** may be missing or null.
- **Never read:** `pinned`.

**`moves[]`**
- **`kind`:** must be one of `merge`, `review`, `threads`, `ci`, `conflicts`. **An unknown kind throws** (`MOVE[x.kind](...)` at 670 and 682; `AUTO_NAME[x.kind]` at 1548).
- **Order is priority:** `mv[0]` is the button, and autoRun takes the first `auto`.
- **`prompt`:** String (`.replace`).
- **`n`:** Int.
- **`jobs`:** `[String]` or missing.
- **`repo`, `how`:** String.
- **`target`:** String or null.
- **`sha`:** String.
- **`auto`:** for `merge`, any truthy value. **For other kinds it must be a String prompt**: `true` throws at 1547.
- **Never read:** `own`.

**`mr` (in trees)**
- **Strings:** `ref` (`"!123"`-like, `.replace`d at 1313), `web_url`, `title`, `state` (`opened` and `merged` are special), `target_branch`, `sha`.
- **Bools:** `draft`, `conflicts`, `rebase`, `mergeable`.
- **`threads`:** Int.
- **`pipeline`:** `success`, `failed`, `running`, `pending`, `created`, or null. autoRun regex: `^(created|waiting|preparing|pending|running|scheduled)`.
- **`by`:** `{name, avatar?}` or null. `name` must be a String because `u.name[0]` is read.
- **`assignees`:** an array of objects shaped like `by`.
- **`labels`:** `[String]`.
- **`milestone`, `description`:** **String or null, never an object.** 1356 calls `.toLowerCase()` on every truthy search-hay value (`project`, `branch`, `goal`, `why`, `puck`, `path`, `title`, `target_branch`, `description` too).

**`tree info`** (775-782)
- `dirty` and `unpushed`: Int or null. Null renders as "–" and 0 as "0".
- `commit`: **epoch ms** or null.
- `error`: String.
- Delete counts as "clean" when `!error && !dirty && !unpushed`.

**`tree rm`** (783-788, 760-768)
- **Check order:** `merge`, then `cfg`, then `info`, then **`risks` truthy**, then `error`, then `op === "rm"`, which means success.
- An empty `risks: []` is truthy and opens an empty dialog, so **omit `risks` when there are none**. `ok` is never read.
- **The wording of each risk string matters** (regexes at 763-766):
  - `/uncommitted/`: red "lost" row.
  - `/commit/`: the ` (...)` part is stripped, and "branch keeps it" is added when `/keeps/` matches.
  - Otherwise the first letter is upper-cased. An empty string throws.

**`tree goal`**
- `error` opens a dialog. `ok` is not read.

**`tree cfg`**
- `error`, or `cfg`: the full object, which replaces `w.cfg`. `ok` is not read.

**`tree merge`**
- `path`, `error`, `ref`, `target`, `how`, all interpolated raw (a null prints "null"). `ok` is not read.

**`suggest`**
- `name` must equal the requested name.
- `prompt`: String. When falsy, the button is hidden.
- `label`: falls back to "Next step".
- `error` works only because `prompt` is then missing. `sig` is not read by the terminal.

### 2e. Files the app reads directly

| File | Where | Writer | Contract |
|---|---|---|---|
| `<GREENLIGHT_HOME>/outbox.jsonl` | tail at 1093-1100; `ping()` at 1102-1128 | greenlight.py `outbox()` (719-722) | See below |
| `~/Library/Application Support/Greenlight/{questions,transcript,projects,terminals*}.json` | 503-505, 3285-3289 | **Swift only** | Not a script contract |
| `<GREENLIGHT_HOME>/last.json` | **Not read by the app.** dash.py reads it | greenlight.py scan | Internal to the port, but `snapshot.scanned` must equal `last.json` `ts`. The agent reads it through SKILL.md, and dash.py reads `next.cmd` from it |

**outbox.jsonl**
- **Format:**
  - **Append-only**, with exactly one JSON object per line and `\n`-terminated.
  - Never rewrite, truncate or reorder it. The app's cursor is a line count (`#start N` plus one per line), so a rewrite skips pings or replays them.
- **Writer:**
  - Every line carries `ts`: an **Int, epoch seconds** (`json.dumps({"ts": int} | ev)`).
  - The ping writer (733-741) adds `{text, key?, ws?}`.
  - The resolve writer (779-781) writes `{resolve, why}`.
- **Swift reads:**
  - If `resolve` is a String: `why`, String, defaulting to "no longer needed".
  - Otherwise `text` is a String and required (it goes through `splitOptions`); `key` is `String?`; `ws` is `[String]?` with empties filtered; `ts` is a `Double?` (defaults to now).
- **Writer-side CLI behavior to keep** (`ping_cli`, 762-776):
  - The text must match `\nOptions: *\S.*\|`, or the command exits nonzero with nothing sent.
  - An unknown `--ws` exits nonzero.
  - `@`-prefixed keys are flap-suppressed, and the command prints `skipped: ...`.
  - `recent_keys` reads the outbox back.

## 3. Key order, whitespace and exact text

**Key order:** irrelevant to every consumer.
- Swift dictionaries are unordered. Swift's own `askedJSON` uses `.sortedKeys`.
- JS reads by name. The only `JSON.stringify` comparison (terminal.html:1516-1517, cfg) costs an extra repaint if the order changes.
- **The golden harness is the exception**: it diffs raw stdout. Either emit keys in Python's insertion order (serde_json with `preserve_order`, or structs whose fields are declared in the same order), or make the harness compare parsed JSON.

**Whitespace and escaping:** irrelevant to consumers.
- Concatenated outputs drop newlines anyway.
- Python's `", "`/`": "` separators and `ensure_ascii` escapes match nothing the consumers check.
- **The streams are the exception**: one compact object per line, newline-terminated, flushed after each line (`dash.py stream`, chat events, outbox). Again, the harness diff sees the bytes.

**Exact text that matters:**
- **stdout prefixes:** stdout begins with `{`, and the stream lines Swift accepts begin with `{`.
- **testHost:** the replacement check must keep the `ok`/`nopy` protocol or change Swift in step.
- **`Options: a | b` parsing:** chat `done.text`, ping text, and the ping CLI's precondition regex.
- **Enum strings:**
  - Puck `state` and tree `state`.
  - `agent` and `agent.state`: `working` and `waiting` drive builds and suggests.
  - The chat `e` kinds.
  - `moves[].kind`.
  - `mr.state` and `mr.pipeline`.
  - Lifecycle `source`.
  - `done` values such as `signoff` and `label=x`.
- **Schedule format:** the `*/N * * * *` form round-trips through `auto.py show` and `edit`.
- **tree rm risk wording:** see §2d.
- **null versus missing:**
  - `suggestion` must be `null`, never absent.
  - `cfg` must be absent or null, never `{}`.
  - `risks` must be absent rather than `[]`.
  - `stages[].req` must be `""`, never null.
  - `built` must be a real boolean.
  - `suggestion` must never be `{}` (dashboard).
  - `agent` must always be an object (dashboard).
  - `mr` may be `{}`, null or missing (all equivalent).
  - `ran` needs `ok: true`.
- **Units:** the units are mixed and must be kept per field.
  - **Epoch seconds:** outbox `ts`, snapshot `ts` and `scanned`, `page.designed`.
  - **Epoch ms:** trees `active`, `since` and `created`; tree info `commit`.
  - **Either:** `agent.since`, which the page accepts as seconds, ms or an ISO string.
  - **Hours:** `agent.idle_h`.
  - **Local-time strings:** history and commit `ts` (`"YYYY-MM-DD HH:MM"`).
- **Number types:** counts must be JSON numbers, not strings: `threads`, `approvals`, `n`, `step_index` (an integer).
- **Error and status strings** are shown to the user verbatim: wording changes are cosmetic, not breaking.

## 4. Python touchpoints to change at cutover

Counts are lines containing `python3` or `.py` (grep `python3\|\.py\b`).

| Place | Count | What changes |
|---|---|---|
| app/Greenlight.swift | **42 lines** | **Functional (21):**<br>• 23: `py` = `python3 .../scripts`<br>• 25: chatCmd<br>• 762-764: testHost runs `command -v python3`, `test -f scripts/greenlight.py`, and the "has no python3" messages<br>• 187, 816, 833, 2933, 2939, 2959, 2989, 3058, 3067, 3068, 3078, 3087, 3098, 3111, 3490, 3559: every `"\(py)/<x>.py"` concatenation becomes `"\(bin) <x>"`<br>• 3998: a selftest chat prompt that runs `python3 -c "import time; time.sleep(60)"` needs Python on the host (use `sleep 60`)<br>**Comments (20):** 2, 3, 7, 153, 156, 178, 224, 499, 502, 710, 757, 2924, 2984, 3054, 3083, 3095, 3485, 3553, 3555, 4037 |
| SKILL.md | 21 lines | 8, 12, 14, 19, 20, 23, 27, 33, 41, 43, 44, 50, 52, 54, 60, 62, 64, 73, 74, 75, 83. Lines 14, 19, 20, 27, 44, 54, 62 and 73 contain the literal `python3 ~/.claude/skills/greenlight/scripts/X.py` the agent copies |
| README.md | 16 lines | 20, 21, 25, 42, 43, 63, 77, 84-91 (the file table), 102 (the test loop). Install steps need a build or download step for the binary |
| app/dashboard.html | 9 comment lines | 7-8, 779, 1501, 1547, 1564, 1767, 1769, 1949. No functional dependency |
| app/terminal.html | 10 comment lines | 282, 351, 601, 665, 684, 698, 740, 759, 835, 1521. No functional dependency |
| scripts/chat.py prompt text | 2 | 24: the SKILL.md path is derived from `__file__`. 32: tells the model to run `greenlight.py note` |
| greenlight.py:510 | 1 | `next.cmd = ["python3", ".../greenlight.py", "signoff", ...]` lands in last.json and is run verbatim by the agent (SKILL.md:33) and parsed by dash.py:134 (which looks for `"signoff"` in the argv) |
| examples/lifecycles/release-train.md | 1 | 5 |
| scripts/sync.sh | 0 | It copies the whole checkout except `.git`, `memory` and `app` to `<host>:.claude/skills/greenlight/` (10). It must ship a binary **built for the host's OS and architecture** (often Linux over ssh, not the Mac's), and stop shipping `scripts/*.py` |
| .github/workflows/tests.yml | 4 lines (plus setup) | Lines 11-13 set up Python 3.12; 17 runs pip install pytest; 22-24 loop over `tests/test_*.py`; 27 runs `pytest tests/test_chat.py`. Node 22 is set up at 14-16, but the `.cjs` tests are never run in CI. Needs a cargo build and test step, plus the harness `check rs` |
| app/build.sh | 0 | No Python. It copies dashboard.html, terminal.html, the icons, vendor and ghostty-web.js |
| tests/test_*.py | 10 files | test_ade, adopt, auto, blocking, chat, cleanup, dash, greenlight, harness, release. They import the modules, so they are deleted or ported along with the Python |

**Persisted state on existing hosts.** Cutover must migrate this; it can't just be edited in the repo.
- **The timer:**
  - `~/Library/LaunchAgents/greenlight-auto.plist` has ProgramArguments `[<sys.executable>, <abs path>/auto.py, tick]` (auto.py:155-163, 209).
  - On Linux, `~/.config/systemd/user/greenlight-auto.service` has `ExecStart=<python> <auto.py> tick` (140-144, 216).
  - Deleting auto.py leaves the timer failing every minute, so the Rust `auto install` must overwrite both files. Have cutover run it.
- **`next.cmd` in `<GREENLIGHT_HOME>/last.json`:** it holds the `python3 .../greenlight.py signoff` argv until the next scan rewrites it. Scanning right after install is enough.
- **User automations:** `<GREENLIGHT_HOME>/automations.json` may hold `precheck` strings like `python3 ~/.claude/skills/greenlight/scripts/greenlight.py precheck` (SKILL.md:73-74 teaches `--precheck CMD`; auto.py:101-104 runs them through a shell). Orca automations created per SKILL.md may hold the same strings. Options: leave thin `scripts/<x>.py` shims that exec the binary, or rewrite the strings during cutover.
- **Memory notes and agent habits:** an agent may keep writing `python3 .../X.py` from memory/ notes or old transcripts. The shims cover this too.

**Total.** About **103 lines across 9 files**, plus 3 kinds of persisted host state:
- 42 Swift (21 functional)
- 21 SKILL
- 16 README
- 19 HTML comments
- 4 CI
- 1 example

## 5. tests/test_dashboard.cjs and tests/test_terminal.cjs

**Both use canned data only.** No `child_process` and no script calls.
- test_dashboard.cjs loads `app/dashboard.html` from a `file://` URL in headless Chromium (Playwright). It stubs `webkit.messageHandlers.mc` and feeds hand-written fixtures through `mc.update(SNAP)`. The fixtures are `puck()` and `SNAP` at 24-28, `REPOS` at 29, and inline `mc.tasked`/`mc.suggest`/`mc.merged`/`mc.asked` objects at 126-392.
- test_terminal.cjs does the same with `TREES` (14-30) and inline `term.tree`/`term.suggest` objects (491-595).

**They are not an end-to-end check of Rust output as written.** The assertions are tied to fixture names such as `plan-a` and `/w/sidebar`, and the fixtures aren't regenerated from any script. They do pin the **shape** the pages expect: for example, `mr: null` in the dashboard puck, `{op:"rm", risks:[...]}`, `{op:"merge", ok, ref, target, how}`. That makes them a good spec cross-check. Note: they are not run in CI.

**Cheap way to make them an e2e check:**
1. In a harness scenario, capture the binary's `dash snapshot`, `dash trees` and `dash tree info` stdout.
2. Add a mode that loads each page, injects that stdout as raw JS exactly as Swift does (`mc.update(<stdout>)`, `term.workspaces(<stdout>)`), and asserts no `pageerror`, plus a non-empty render.
3. That catches every "throws on unexpected type" case in §2d: an unknown `moves.kind`, `auto: true`, a null `project`, an object `milestone`, `risks: []`.

**The real black-box check is migration/harness.sh** (`record` with Python, then `check rs`). It covers the CLI side byte for byte, but no consumer parses its output. Pairing it with the page-injection smoke test above covers both sides.
