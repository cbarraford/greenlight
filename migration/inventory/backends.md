# Backends inventory: scripts/dash.py, scripts/chat.py

Scope: `scripts/dash.py` (1102 lines), `scripts/chat.py` (384), `tests/test_dash.py` (323), `tests/test_chat.py` (127).
Callers: `app/Greenlight.swift` over ssh (`ssh()` at Greenlight.swift:423 sets **stderr → /dev/null** and splits stdout on `\n`).
Refs are `file:line`; `d:` = dash.py, `c:` = chat.py, `g:` = greenlight.py, `h:` = harness.py, `td:`/`tc:` = the tests.

Observed with probes (fake `orca` via `ORCA_CLI_COMMAND`, fake `claude` on PATH, Python 3.15). Both test files pass today
(`python3 tests/test_dash.py`; `uv run --with pytest python -m pytest tests/test_chat.py`: 8 passed).

**Runtime floor:** these scripts need Python >= 3.12. `g:822` uses a nested same-quote f-string (PEP 701); `/usr/bin/python3` on macOS fails with SyntaxError on import. Both dash.py and chat.py import greenlight (chat through harness).

---

## 1. CLI surface

### Shared output conventions
- All JSON goes out through `json.dumps(obj)` with **default args**: `ensure_ascii=True` (every non-ASCII char becomes `\uXXXX`, including astral chars as surrogate pairs `\ud83d\ude00`), separators `", "` and `": "`, keys in insertion order, `NaN`/`Infinity` emitted bare if present. **Byte-exact requirement.** serde_json writes compact `,`/`:` and raw UTF-8, so the port needs a custom formatter plus `preserve_order`.
- A single `print()` writes one line per command. Only `dash.py stream` and every `chat.py` event pass `flush=True` (`d:1041,1043`, `c:41`). Single-shot commands rely on the flush at exit.
- File writes use other formats: `indent=2` for `.greenlight.json` (`d:400`) and `indent=1` for `autorun.json` (`d:989`). Both keep `ensure_ascii=True`, and Python's indent mode uses separators `","` + newline and `": "`.
- `sys.exit("string")` prints the string to **stderr** and exits 1. `raise SystemExit(json)` behaves the same way. Uncaught exceptions print a traceback to stderr and exit 1. Because the Swift side throws stderr away, those cases look like "empty stdout, nonzero exit" to the app.

### dash.py (`main`, d:1031-1098). Usage text = module docstring d:1-25
Dispatch is on `a = sys.argv[1:]`. Any args that match no branch below run `sys.exit(__doc__)`, which prints the docstring to stderr and exits 1.

| Subcommand | Arg validation | stdin | stdout | Errors / exit |
|---|---|---|---|---|
| `snapshot` (d:1033) | none | none | `json.dumps(snapshot())` | Not wrapped. A missing `last.json` or an orca failure gives a traceback and exit 1. **No Swift caller.** |
| `stream [secs]` (d:1035-1044) | `float(a[1])`, default `10`. A non-numeric value raises ValueError: traceback, exit 1 | none | One snapshot line per tick, flushed | Each tick's exceptions become `{"error": str(e)[:300]}` on stdout and the loop continues. See §2. Swift runs `stream 5` (Greenlight.swift:2939) and keeps only lines with `hasPrefix("{")`, so error lines also reach `mc.update`. |
| `page <ws> [--fresh] [--model M] [--effort E]` (d:1045-1049) | `NAME_RE.match(ws)` (d:42 `^[\w.@+/-]{1,200}$`). Options are read only when the flag is in `a[:-1]`, so a flag in last position is silently ignored. Each value must `fullmatch [\w.-]{1,40}`, else `sys.exit("bad --model/--effort")` (stderr, exit 1). `--fresh` is recognised anywhere in argv, even as the value of `--model` (`--model --fresh` passes the regex) | none | `{"name", "facts", **cache}` (d:758) | Not wrapped. An unknown ws raises `SystemExit(json.dumps({"error": "no workspace X"}))` (d:474): that JSON goes to **stderr**, exit 1, stdout empty (probe confirmed). A corrupt page cache raises ValueError: traceback (d:749). Designer failures are caught and added as an `"error"` key (d:756). |
| `suggest <ws> [--model M] [--effort E]` (d:1050-1057) | same as `page` | none | `suggest()` dict | Every `Exception` becomes `{"name", "error": str(e)[:200]}`, exit 0. A SystemExit raised inside `facts` is not an `Exception` and escapes. |
| `run <ws>` (d:1058-1060) | NAME_RE | `read().strip()[:8000]` (chars) | `{"name", **run()}`, or `{"name", "error": "empty prompt"}` | Not wrapped. orca exceptions other than RuntimeError (timeout, bad JSON) give a traceback and exit 1. |
| `ask <ws> [--model M] [--effort E]` (d:1061-1072) | same as `page` | none | `{**ask_user()}` (its own `name` key goes first) | Exceptions become `{"name", "error"[:200]}` |
| `answer <ws>` (same branch) | NAME_RE. The option regex is checked here too | `read().strip()[:4000]` | `{"name", **answer()}`, or `"empty answer"` | wrapped as for `ask` |
| `park <ws> on\|off` (d:1073) | `len(a)==3` exactly | none | `{"name", **park()}` | wrapped `[:200]` |
| `task <op> <arg>` (d:1078-1082) | `len(a)==3` and `NAME_RE.match(arg)`. `op` is unvalidated | `read().strip()[:20<<20]` (chars) | `{"op", "arg", **task()}` | wrapped `[:300]` |
| `transcript <ws> [--before C]` (d:1083-1087) | NAME_RE. `C` must `fullmatch [\w.-]{1,200}:\d{1,9}`, else `sys.exit("bad --before")`. `--before` in last position is ignored | none | `{"name", **transcript()}` | Not wrapped. **No Swift caller.** |
| `trees` (d:1088) | none | none | `{"trees": trees()}` | Not wrapped. A scan row without `next` raises KeyError in `state_of`: traceback |
| `tree info\|rm\|goal\|cfg\|merge <path> [--force]` (d:1090-1096) | `fullmatch /[\w./@+-]{1,300}` and no `..`. `--force` is recognised anywhere in `a[3:]` and only affects `rm` | `goal`: `read()[:2000]`, not stripped. `cfg`: `read(8193)` chars | `{"op", "path", **out}` | Every `Exception` becomes `{"op", "path", "error": str(e)[:300]}`, exit 0 |

`task` ops (d:955-1028). `arg` must pass NAME_RE first.
- `repos` (arg is conventionally `-`) returns `{"repos":[{name,host,path}]}`.
- `new <repoName>`: stdin JSON `{text, files:[{name, data:"data:...;base64,..."}]}`. An empty stdin becomes `"{}"`.
- `prompt-get|prompt-set <repo>/<kind>`: `kind ∈ {review, threads, ci, conflicts}`. `repo` must `fullmatch [\w.-]{1,100}` and must not start with `.`. A failure returns `{"error":"bad repo or kind"}`. `prompt-set` writes stdin if it is non-empty and deletes the file if it is empty. Any other `prompt-*` returns `"no op X"`.
- `autorun-get|autorun-set <repo>`: set stdin is `{kind: gateIndex}`. A bad repo returns `"bad repo"`.
- `lifecycle-get|lifecycle-set|lifecycle-reset <repo>`: set stdin is a lifecycle doc JSON. Any other `lifecycle-*` returns `"no op X"`.
- `instructions-get|instructions-set <any>`: set stdin is the text, and empty stdin deletes the file.
- `memory-list <any>`, `memory-set|memory-delete <name>`: `name` must `fullmatch [\w-]{1,100}` and must not be `MEMORY`. A failure returns `"bad note name"`.
- Anything else returns `{"error": "no task op X"}` (d:1028).

Return shapes are in §3 and the code. Key order: settings results are merged after `op` and `arg`.

### chat.py (`main`, c:355-380). Usage in docstring c:2-12
`cmd = args[0] if args else "send"`. **Any unrecognised first arg is treated as `send`** (the `else:` at c:379), and so is a flag as the first arg. `opt(k)` = `args[args.index(k)+1]`, so a flag in last position raises IndexError: traceback, exit 1. `os.makedirs(DIR)` always runs first (c:356).

| Cmd | stdin | stdout | Notes |
|---|---|---|---|
| `send [--model M] [--effort E] [--name N]` | `read().strip()` | event lines, flushed (§2) | If M is in `harness.H` (`claude, codex, opencode, pi`) it selects the engine with the model left unset. Otherwise engine = claude and model = M. An empty message is still sent (as `WRAP + ""`). Swift: Greenlight.swift:1000 `send --model X --effort Y --name 'N'` (model/effort unquoted). |
| `compact [--model M]` | ignored | If `DIR/session` is missing: one `{"e":"done","ok":true,"text":"Nothing to compact yet.","context":0,"window":null,"ms":null}`. Otherwise a turn of `/compact` | Checks only the **claude** session file (c:375), even when M names another harness. Swift guards with `isClaude`. |
| `new` | none | nothing | Deletes `session` and `session_{claude,codex,opencode,pi}`. Leaves `pid` and `bg.json` |
| `stop` | none | nothing | `killpg(int(read(pid)), SIGTERM)`. ProcessLookupError and ValueError are swallowed. **PermissionError is not**: traceback |
| `bg` | none | `{"shells":[{id,title,started}], "agents":[{title,agent,workspace,started}]}` | **No Swift caller** |
| `bgstop <task_id>` | none | `{"ok": bool}` | No arg: IndexError traceback (observed). **No Swift caller** |

Exit codes: always 0 unless an uncaught exception escapes. Inside `turn`, only the first `mk`/`run` pair is wrapped, and only for ValueError/OSError (c:334-338). A KeyError or AttributeError from `Norm`, raised for example by a malformed stream line that is valid JSON but not an object, a `tool_use` without `id`, or a `compact_boundary` without `compact_metadata`, escapes as a traceback with exit 1. The child keeps running in its own session.

---

## 2. Long-running / streaming modes

### `dash.py stream N` (d:1035-1044)
- `N` is the **sleep interval in seconds** (a float, so `0.1` works). Default 10. Swift uses 5.
- The loop is infinite: `snapshot()`, then `record(pucks)`, then print the line flushed, then `time.sleep(N)`. It never exits on its own. It ends on SIGTERM/SIGHUP or when stdout breaks. A broken stdout raises BrokenPipeError inside the try; the `except` branch then prints again, that print raises outside the try, and the result is a traceback with exit 1. Swift restarts it 10 s after exit (Greenlight.swift:2944).
- A negative N makes `time.sleep` raise ValueError outside the try: traceback.
- `record()` (the history side effect, d:408-420) only runs here. `snapshot` and `page` do not record.
- The first tick happens immediately. The sleep comes after each print.
- **Harness:** spawn `stream 0.05`, read exactly K lines, then SIGTERM. Assert on each line with `ts`/`idle_h`/`designed` masked, plus `$GREENLIGHT_HOME/dash/state.json` and `*.jsonl`. To exercise `record` deterministically, swap the fake orca's output between ticks: the fake reads a counter file and emits scenario N on its Nth call. Recommend the Rust binary **not** grow a tick-bound flag, since the bound belongs on the harness side.

### `chat.py send|compact` (`turn` c:321-352, `run` c:226-270)
1. `engine, model = (M, None) if M in harness.H else ("claude", M)` (c:322).
2. The system prompt is `PROMPT` (c:22-34, which embeds the absolute path `<repo>/SKILL.md`). If `name` is truthy and `name.lower()` is not `"greenlight"`, append ` The user has named you {name} in the app; answer to that name.` (c:323). If `$GREENLIGHT_HOME/instructions.md` exists (expanduser'd), append `\n\nThe user's standing instructions, which override the defaults above:\n` + its stripped contents (c:324-326).
3. `sid = session(engine)`: the stripped contents of `DIR/session` (claude) or `DIR/session_<engine>`, or None.
4. If not compact, `text = "USER MESSAGE (reply to this): " + text` (c:35, c:329).
5. Emit `{"e":"start","engine":E,"model":M or "default"}` (c:332). This always comes first, even when the harness is unknown.
6. Build the command with `harness.argv(engine, text, model=, effort=None if compact else effort, sid=, system=prompt, stream=True)` (h:36-58):
   - claude: `[bin, "-p", <perms>, "--output-format","stream-json","--verbose","--include-partial-messages", "--append-system-prompt", PROMPT, "--model", M?, "--effort", E?, "--resume", sid?]`. Empty or None values drop both the flag and the value. perms = `--permission-mode acceptEdits`, or `bypassPermissions` when `config.harnesses.claude.bypass`.
   - codex: `[bin,"exec",("resume",sid)?,"--skip-git-repo-check",perms,"--json",("-m",M)?,("-c","model_reasoning_effort="+eff)?,"-"]`. `xhigh` and `max` map to `high`. **No system flag**, so when `sid` is None the prompt is prepended to the message as `system + "\n\n" + text`.
   - opencode: `[bin,"run",perms,"--format","json",("-m",M)?,("--variant",E)?,("-s",sid)?]`. The prompt is prepended the same way as for codex.
   - pi: `[bin,"-p",perms,"--mode","json",("--model",M)?,("--thinking",E')?,("--session",sid)?,("--append-system-prompt",sys)?]`. `max` maps to `xhigh`. **Raises ValueError unless `harnesses.pi.bypass`**, and that error becomes a done event with ok=false.
   - Unknown engine: impossible here, because the engine is always in H or `claude`. `binary()` = `config.harnesses.<n>.bin`, then `shutil.which`, then `~/.local/bin/<n>`, then `~/.<n>/bin/<n>`, then the bare name.
7. `run()`: `Popen(cmd, stdin/stdout/stderr=PIPE, text=True, cwd=DIR, start_new_session=True)`. Because of the new session, the **child's pgid equals its pid**. Write `DIR/pid` = child pid (c:231), *before* writing stdin. Write the full text to stdin and close it. Iterate stdout lines; a line that is not JSON is skipped silently.
   - Session id: `sid = d.session_id or d.thread_id or d.sessionID or (d.id if d.type=="session") or sid` (c:240). The last truthy value wins.
   - `evs = Norm.<engine>(d)`.
   - **Retry check** (only when `check=True`, i.e. the first non-compact run): if any event is `done` with `ok` true and `IGNORED.match(text or "")` (c:37 `^\s*(no response requested\.?)?\s*$`, case-insensitive), then `killpg(pid, SIGTERM)` (ProcessLookupError swallowed), `wait()`, remove `pid`, and return `(sid,"retry")`. **This done is never emitted**, but earlier text/step events of that run were already streamed. `bg.json` is not removed on this path.
   - Otherwise each event is handled as follows. `bg` events are not printed; they overwrite `DIR/bg.json` with `json.dump(shells)`. A second `done` is dropped (dedupe for opencode). Every other event is printed (`emit` = `print(json.dumps(e), flush=True)`).
   - After EOF: `wait()`. If no done was emitted and the return code is 0, emit `{"e":"done","ok":true,"text":norm.text,"context":norm.context,"window":norm.window,"ms":null}`. This is opencode's implicit done, but it also fires for any harness that exits 0 without a result line. Then remove `pid` and `bg.json`.
   - If no done and the return code is nonzero: `-15` or `143` returns `"Stopped"`. Any other code returns `stderr.read().strip()`, or `f"exit {rc}"` if stderr is empty. If a done was emitted, a nonzero rc is ignored.
8. `turn` after the first run:
   - `"retry"`: emit `{"e":"status","text":"Reply ignored your message, asking again"}`, set `norm.text=""`, and run once more **unchecked** with `sid = new_sid or sid` and text `"Your previous reply ignored the user's message. Answer it now: " + WRAP + msg` (so the RETRY_PREFIX is followed by the already-wrapped text). Then set `sid = None`, which blocks the next branch.
   - If `err` and `sid` and not compact and `err != "Stopped"`: emit `{"e":"status","text":"Session lost, starting a new one"}` and rerun with `sid=None` and the same `text`. **This fires on any failure** (crash, auth, nonzero exit) whenever a prior session existed, not only when the session was lost. For codex and opencode, the system prompt is prepended again.
   - If `new_sid` is set, write it to the session file. This happens **even after a Stop** (probe: S10 persisted). If the last run produced no sid, the old session file stays.
   - If `err`: emit `{"e":"done","ok":false,"text":short(err,500),"context":norm.context,"window":norm.window,"ms":null}`. `short` collapses whitespace.
   - Exceptions from the first `mk`/`run` that are ValueError or OSError (missing binary, pi bypass, BrokenPipe on stdin) produce `{"e":"done","ok":false,"text":str(e),"context":0,"window":null,"ms":null}` with Python's error text, e.g. `"[Errno 2] No such file or directory: 'codex'"` (observed). **No cleanup** of `pid` on BrokenPipe.

**Event schema (Norm, c:70-216)**. Every event has an `e` field. Key order matches the dict literals.

| e | fields | emitted by |
|---|---|---|
| `start` | `engine, model` | turn |
| `status` | `text` | claude system/status (`status.capitalize()`), thinking deltas (`"Thinking"`), codex `item.started` for a non-command item (`type.replace("_"," ").capitalize()`), codex `error` starting with "Reconnecting" (`short(msg,80)`), pi/opencode reasoning, turn retries |
| `usage` | `context` **or** `tps` | claude message_start (context = input + cache_read + cache_creation, or the previous value if that is 0), compact_boundary (`post_tokens`), text deltas (tps, throttled to one per 0.5 s), message_delta (tps = output_tokens / elapsed), opencode step_finish, pi message_end |
| `text_reset` | none | claude content_block_start of type text. codex agent_message, opencode text, pi text_start (codex and opencode emit text_reset followed by the whole text) |
| `text` | `delta` | text_delta. The whole message for codex/opencode |
| `step` | `id, title (short 80), detail (short 160)` | claude assistant tool_use, codex command_execution started (detail = the part after `" -lc "`, with `'` stripped from both ends), opencode tool_use, pi tool_execution_start |
| `step_done` | `id, ok, out (short 120)` | claude user tool_result (a list content is joined from the `text` of dict items), codex command completed (`ok = exit_code==0`), opencode (sent right after `step`), pi tool_execution_end |
| `bg` | `shells:[{id,title,cmd,started}]` | claude system task_started with is_backgrounded, and background_tasks_changed. **Written to bg.json, never printed** |
| `done` | `ok, text, context, window, ms` | claude result (`text = result or last text block`, `window = first modelUsage value's contextWindow`, `ms = duration_ms`). codex turn.completed (`window = 272000`). codex turn.failed or error (`text = str(d.error or d.message)`, **Python repr if that value is a dict**). opencode error. pi agent_end. run/turn fallbacks |

tps rule (c:117-122): `round(chars/4/max(now-msg_t,0.2),1)`. `last_tps` starts at 0, so the first delta after any message_start always emits. With a fast fake (elapsed < 0.2 s), tps is deterministic: 2 chars gives `2.5` (observed).

`step_title` (c:49-67):
- Bash: `(description or "Ran a shell command", command)`.
- Read: `("Read "+basename, path)`.
- Edit/Write/NotebookEdit: `"Edited "+basename`.
- Skill: `"Loaded skill"`.
- Task/Agent: `"Started an agent"`.
- Grep/Glob: `"Searched files"`.
- WebFetch/WebSearch: `"Fetched a page"` / `"Searched the web"`.
- Anything else: `(name.split("__")[-1].replace("_"," "), first str value of input)`.
- `path` is the first of `file_path`, `path`, `notebook_path`.

**Stop** (c:364-369): read `DIR/pid` and send SIGTERM to the whole group. This reaches the agent CLI and all its descendants, but not chat.py's own `send` process, which is in the ssh session's group. Send then sees EOF and rc -15, and emits `done ok:false text:"Stopped"` (observed). Stop has no liveness or identity check on the pid (§8). Swift (Greenlight.swift:1018-1023) shows "Stopped." regardless.

**bgstop** (`stop_shell` c:297-318): `key` = the longest piece of the shell's `cmd` after splitting on `'`, `"` or `\`, stripped (ties go to the first). If `key` is empty or `pid` is missing, return False. Run `ps -eo pid=,ppid=,args=`, build the child map, and collect descendants of the pid (excluding the pid itself) whose args contain `key`. SIGTERM each hit's whole subtree (iteration order is set order). Return `bool(hits)`.

**Deterministic harness proposals**
- A fake agent CLI on PATH named `claude`, `codex`, `opencode` or `pi`, or `config.json` `harnesses.<n>.bin` pointing at it. The fake does three things:
  - appends argv and stdin to a log;
  - prints canned JSONL from a fixture file selected by an env var or by a marker in stdin (as tc:38-51 does with "BUG"/"STUBBORN");
  - exits with a chosen rc.
- Set `CHAT_DIR` to a scratch dir. Set `GREENLIGHT_HOME` to a scratch dir (for instructions.md) and `GREENLIGHT_ME=x` (§4 import side effect).
- Assertions: the event lines (mask `tps` values, or keep the fake under 0.2 s and assert exact), the final contents of `session*`, the absence of `pid` and `bg.json`, and the logged argv/stdin.
- Stop scenario: the fake prints an init line and a tool_use, then sleeps. The harness polls until `DIR/pid` exists, runs `chat.py stop`, and asserts `done` with text `"Stopped"`, exit 0, and the session saved.
- Retry scenarios: canned `result` text `"No response requested."` on the first call, then a real reply on the next call (fake keeps a call counter).
- Session-lost: pre-seed `DIR/session`, the fake exits 1 with stderr when `--resume` is present and succeeds otherwise. Expect the status line, the rerun without `--resume`, and the new sid saved.
- Codex/opencode/pi: one canned stream each covering every branch of `Norm.<engine>`. For opencode also cover clean exit with no final event (implicit done) and a duplicate error (dedupe).

---

## 3. Files

`STATE` = `Path($GREENLIGHT_HOME or ~/.greenlight)`, **not expanduser'd** (g:40). chat.py's `DIR` = `expanduser($CHAT_DIR or ($GREENLIGHT_HOME or "~/.greenlight") + "/chat")` (c:18). `ROOT` = repo or skill dir (g:39). `DASH = STATE/dash` (d:41). `safe(n)` = `re.sub(r"[^\w.@+-]","_",n)` (d:404, Unicode `\w`). `history_file(n)` = `STATE/history/<n with "/"→"__">.md` (g:848).

| Path | R/W | Format | Mode | Where |
|---|---|---|---|---|
| `STATE/last.json` | R | `{"ts", "rows":[...]}` (greenlight scan) | | snapshot d:217, trees d:283, run d:570, tree_merge d:163 |
| `STATE/last.json` | **W** | `json.dumps(snap)` with default separators | full rewrite, no lock | tree_merge d:172-174: the row gets `mr.state="merged"` and `next={"step":"done","owner":"done","action":"merged from Greenlight"}` |
| `STATE/blocks.json` | R | `{"ts", "blocks":{"proj!iid":{reason,why}}}`. `{}` if the roster has no repos. None if missing, corrupt or older than 6 h | | blocking.cached (blocking.py:187) |
| `STATE/config.json` | R (import) | merged shallowly over DEFAULTS (g:57). Only FileNotFoundError is caught, so a corrupt file gives a traceback | | everything |
| `STATE/me.json` | R/W (import) | `{"me"}` | rewrite | g:104-119 |
| `STATE/autorun.json` | R / W | `{repo:{kind:int}}`. Written with `json.dumps(a, indent=1)`. **No mkdir**: if STATE is missing, the write raises | rewrite | autorun d:115, settings d:989 |
| `STATE/prompts/<repo>/<kind>.md` | R/W/unlink | text, `strip()+"\n"` | rewrite | prompt_of d:94, settings d:972-977 |
| `ROOT/prompts/<kind>.md` | R | bundled template (must exist: ci, conflicts, review, threads) | | d:95, d:980 |
| `STATE/lifecycles/<repo>.md` | R/W/unlink | markdown (g:212 `lifecycle_md`) | rewrite | settings d:994-1000. Read through `lifecycle_doc` (falls back to `ROOT/lifecycles/<repo>.md`, then `default.md`) in progress d:199 |
| `STATE/instructions.md` | R/W/unlink | text `strip()+"\n"`. **No mkdir** | rewrite | settings d:1005-1008. **chat.py reads the same file** through an expanduser'd `$GREENLIGHT_HOME/instructions.md` (c:324) |
| `ROOT/memory/MEMORY.md`, `ROOT/memory/<name>.md` | R/W/unlink | index lines `- [n](n.md) — desc` (U+2014). Note files are `rstrip()+"\n"` | rewrite | settings d:1009-1027. **These live in the repo or skill checkout, not STATE.** `ROOT/memory` does not exist in this checkout |
| `STATE/history/<name>.md` | R / **append** | lines `- YYYY-MM-DD HH:MM <text>\n` (local time). Read with HIST_RE `^- (\d{4}-\d\d-\d\d \d\d:\d\d) (.*)` | append | park d:556, run d:585, answer d:716, _note d:883, history d:424 |
| `DASH/state.json` | R/W | `{name:[state,step,owner]}` | rewrite each tick | record d:411-420 |
| `DASH/<safe(name)>.jsonl` | **append** / R | `{"ts":int,"from":[3],"to":[3],"action":str[:200]}` | append | record d:418, history d:428 |
| `DASH/pages/<safe>.json` | R/W | `{"v":2,"sig","designed":int,"layout":{puck,tiles,wide}}` | rewrite | page d:748-755, cached_page d:741 |
| `DASH/suggest/<safe>.json` | R/W | `{"name","sig","prompt","label"}`, written only when the prompt is non-empty | rewrite | suggest d:511-528, cached_suggest d:534 |
| `DASH/ask/<safe>.json` | R/W | `{"key": sha1(q)[:12], "out": {...}}` | rewrite | ask_user d:647-663 |
| `STATE/tasks/<name>/request.md` | W | `request + "\n"` | create | task_new d:928 |
| `STATE/tasks/<name>/files/<fn>` | W | raw decoded bytes | create (`mkdir(parents=True)` on `files`, which raises if it exists) | d:927-930 |
| `<main checkout>/.greenlight.json` | R / W | object. Read only when `st_size <= 8192` and the content is a JSON object. Written as `json.dumps(o, indent=2)+"\n"` | rewrite | repo_cfg d:378-386, tree_cfg d:400 |
| `<worktree>/.git` | stat | its mtime is the `created` ms | | d:320-325 |
| `~/.claude/projects/<path with [^A-Za-z0-9]→"-">/*.jsonl` | R | Claude Code transcript JSONL | | transcript_files d:807, pending_questions d:675 |
| `~/.codex/sessions/*/*/*/rollout-*.jsonl` | R (first line, mtime < 30 d) | `{"payload":{"cwd"...}}` | | d:809-814 |
| `DIR/session`, `DIR/session_<engine>` | R/W | sid text (written raw, read stripped) | rewrite | c:219-223 |
| `DIR/pid` | W/R/rm | child pid decimal | rewrite | c:231, 248, 263, 283, 302, 312, 365 |
| `DIR/bg.json` | W/R/rm | `json.dump(shells)` `[{id,title,cmd,started}]` | rewrite | c:253, 263, 283, 299 |

---

## 4. External invocations

| Who | argv | stdin | cwd / env | timeout | parse | failure |
|---|---|---|---|---|---|---|
| **import of greenlight** (every dash.py and chat.py run) | `glab api user`, then `gh api user` (g:110) | — | inherited | 20 s | JSON `username`/`login` | Runs only when `GREENLIGHT_ME`, `config.me` and `me.json` are all unset. Writes `me.json`. Any exception moves on to the next tool. Black-box harnesses **must set `GREENLIGHT_ME`** or seed `me.json` |
| `greenlight.orca(...)` (g:125) | `[$ORCA_CLI_COMMAND or which(orca-ide) or ~/.config/orca/linux-orca-cli-shim/orca or "orca", *args, ("--environment",$ORCA_ENVIRONMENT)?, "--json"]` | — | inherited | 120 s | JSON `{ok, result, error}`. `ok` false raises `RuntimeError("orca <args>: <error>")`. A nonzero rc raises `RuntimeError("<cmd>: <stderr or stdout[:300]>")` | When `GREENLIGHT_ADE != orca`, routes to `ade_<x>.call(*args)` instead |
| live_agents d:210 | `worktree ps --limit 10000` | | | | `.worktrees` | propagates |
| waiting_term d:601, 606 | `terminal list --limit 10000`, `terminal read --terminal H --limit 60` | | | | `.terminals`, `.terminal{tail,draft}` | propagates |
| answer d:702-713 | `terminal send --terminal H --text T [--enter]`. Esc is `--text "\x1b"`. Then up to 20 × (sleep 0.4 s + `terminal read`) | | | | | propagates. Timeout returns an error dict |
| park d:553 / tree_goal d:371 | `worktree set --worktree id:W --comment G` | | | | | |
| run d:577 | `agent launch --agent A --worktree id:W --prompt P`, in order `[row.next.agent or agents[0], *CONFIG.agents]` deduped | | | | | A RuntimeError matching `LIMIT_RE` (g:68) moves to the next agent. Any other RuntimeError, or one on the last agent, returns `{"error": str[:300]}` |
| tree_rm d:365 | `worktree rm --worktree id:W [--force]` through the ADE's own `call` | | | | | |
| task_new d:939, 948 | `worktree create --repo id:R --name N --setup skip --comment TITLE --no-parent --agent A --prompt BRIEF`, then `worktree set --worktree id:W --display-name N` | | | | `.worktree.id` | Same LIMIT_RE fallback, with the error prefixed `couldn't start the workspace:`. A failed rename is ignored |
| repos d:857 | `repo list` | | | | `.repos[].gitRemoteIdentity.canonicalKey` | |
| chat background c:287 | `worktree ps` (**no `--limit`**) | | | | | OSError/ValueError/KeyError/RuntimeError/Timeout give `agents=[]` |
| trees d:286-291, tree d:330-338 | For each `orca`, `cmux`, `herdr`, `t3`: `greenlight.orca_cli` (always Orca, ignoring GREENLIGHT_ADE) or `ade_<x>.call("worktree","ps","--limit","10000")` | | | | | Any Exception skips that ADE. The adapters shell out to `cmux --id-format uuids --json ...` (ade_cmux.py:24), `herdr [--session $GREENLIGHT_HERDR_SESSION] ...` (ade_herdr.py:21), and `which(t3) or $T3CODE_HOME/bin/t3` (ade_t3.py:19); see those files for the reply shapes. A fake must be either absent or shaped correctly |
| git | `git -C P rev-parse @{u}` (d:311, **no timeout**, only when a move is `auto`). `git -C P status --porcelain --untracked-files=normal`, `log -1 --format=%ct`, `rev-list HEAD --not --remotes` (d:343-348, 20 s each). `log -15 --format=@%h\t%cI\t%s --numstat` (d:438, 10 s) | | | | text | tree_info: a nonzero status rc returns `{"error": stderr or "not a git checkout"[:200]}`. A TimeoutExpired propagates and main wraps it. git_facts: OSError or Timeout returns `{}` |
| tree_merge d:169 | `greenlight.run(next.cmd + ["--match-head-commit" if cmd[0]=="gh" else "--sha", sha], timeout=90)`, e.g. `glab mr merge 58 -R o/r --yes --squash --sha abc` | | | 90 s | stdout ignored | A RuntimeError becomes `{"error": "<cmd>: <stderr[:300]>"[:300]}` |
| mr_facts d:457-461 (page, suggest via facts) | `greenlight.mr_details` (g:260): GitLab `glab api projects/<urlenc>/merge_requests/<iid>` plus `/discussions` (paged), `/approvals`, failed jobs, `/blocks`. GitHub `gh api graphql -F owner= -F name= -F n= -f query=...`. Then GitLab only: `glab api projects/<pid>/pipelines/<id>/jobs?scope[]=failed&scope[]=success&scope[]=running&scope[]=pending&per_page=100&page=N` | | | 45 s, 3 tries with backoff on TRANSIENT_RE | JSON | Any Exception becomes `{"error": str[:200]}` inside `facts.mr_live` |
| **LLM `ask()`** d:499-503 | `harness.argv(CONFIG.llm_harness, prompt, model=M if harness=="claude" else None, effort=E)`, non-stream, no system or sid. Claude: `[claude,"-p","--permission-mode","acceptEdits","--model","claude-haiku-5-5","--effort","low"]` (observed) | the prompt | cwd inherited (the ssh login dir). env inherited | **180 s** | Only stdout is used. The exit code is ignored | A TimeoutExpired propagates |
| ↳ design (page) d:481-496 | prompt = catalog text + `json.dumps(facts)[:30000]` | | | | `re.search(r"\{.*\}", out, re.S)` (greedy, first `{` to last `}`), then `json.loads`, then `validate`. No match gives `{}` | Any exception: `{"error":"designer failed: <str(e)[:200]>"}` and the old layout is kept. Cache `DASH/pages/<safe>.json` is keyed by `sig` and `v` |
| ↳ suggest d:506-529 | prompt with `CONFIG.name`, `CONFIG.suggest_rules`, and `json.dumps(facts(name))[:30000]` (facts runs a **second** full snapshot plus git and forge calls) | | | | `split_label` d:539 | An empty reply gives `prompt:"", label:""` with no cache write. Cache `DASH/suggest/<safe>.json`, keyed by `sig` and the presence of `label` |
| ↳ ask_user d:657 | "A coding agent ended its turn..." + `q[-6000:]` | | | | `re.search(r"\[.*\]", raw, re.S)`, then `json.loads`, then the first 3 items with truthy `str(o).strip()`, labels `str(o)[:200]` | Every exception is swallowed: options `[]` and **no cache write**. Cache `DASH/ask/<safe>.json` is keyed by `sha1(q.encode()).hexdigest()[:12]`. The menu path never calls the LLM |
| chat run c:229 | §2 | the message | cwd=`DIR`, `start_new_session` | none | JSONL | §2 |
| stop_shell c:304 | `ps -eo pid=,ppid=,args=` | | | none | `split(None,2)` | |

---

## 5. Env vars read

| Var | Where | Effect |
|---|---|---|
| `GREENLIGHT_HOME` | g:40 (STATE, not expanduser'd), c:18 and c:324 (expanduser'd), ade_common:9, ade_herdr:15 | the state root |
| `CHAT_DIR` | c:18 | overrides the chat dir. Swift's `--selftest` uses `$GREENLIGHT_HOME/chat-test` (Greenlight.swift:25) |
| `GREENLIGHT_ME` | g:122 | skips the whoami forge calls on import |
| `GREENLIGHT_ADE` | g:127 | routes `greenlight.orca` to `ade_<x>` (does not affect `trees` or `tree`) |
| `ORCA_CLI_COMMAND`, `ORCA_ENVIRONMENT` | g:135-136 | the orca binary, and `--environment X` |
| `HOME` | `Path.home()` d:807,809; `expanduser` c:18,324; h:31 | transcripts, the binary fallbacks |
| `PATH` | `shutil.which` h:31, g:135, ade_t3:19; subprocess lookup | |
| `TZ` (implicit) | `time.localtime`/`strftime` d:431, 557, 586, 717, 839, 884 | history timestamps |
| `GREENLIGHT_HERDR_SESSION`, `T3CODE_HOME` | ade adapters (indirect, through trees/tree) | |
| `CMUX_QUIET=1` | set by ade_cmux:23 for its child | |
| (config.json keys, not env) | `llm_harness`, `agents`, `harnesses.<n>.bin/bypass`, `name`, `suggest_rules`, `blocking`, `me` | |

Children inherit the full environment, and no variable is scrubbed.

---

## 6. Nondeterminism

- **Clocks:**
  - `snapshot.ts = int(time.time())` (d:272), and `now` for `idle_h` (d:219, 238).
  - `record` `ts` (d:419). `page.designed` (d:753).
  - History lines use `strftime('%Y-%m-%d %H:%M')` in local time.
  - The transcript separator uses `strftime('%b %-d %H:%M', localtime(st_ctime))` (d:839). `%-d` is a glibc/BSD extension, `%b` depends on locale, and `st_ctime` is the inode change time on Unix, not the creation time.
  - Chat tps uses `time.time()` deltas (c:104, 117-122). Bg shell `started` is `time.time()` as a float (c:92).
  - `transcript_files` applies a 30-day mtime cutoff and sorts by mtime (d:808-815).
  - `created` is `int(st_mtime*1000)`, float math (d:323).
  - `answer` polls with `sleep(0.4)` × 20.
- **Hashing (deterministic but format-coupled):**
  - `sig(p) = sha1(json.dumps([state, step, owner, agent.state, mr.state]).encode()).hexdigest()[:12]` (d:736). The bytes depend on the `", "` separator, ensure_ascii and `null`. **Any drift invalidates every cached page and suggestion, and changes the `sig` the app compares.** Golden: the freeform row `["quiet", "freeform", "parked", "none", null]` gives `57d9af913dc1` (observed).
  - `ask` cache key `sha1(q.encode('utf-8'))[:12]`.
- **Ordering:**
  - Orca list order drives the iteration order of `live_agents` (a dict). Later duplicate `worktreeId`s win.
  - Agents are sorted by `updatedAt` desc with a **stable** sort, so ties keep their input order. `trees` uses `max(...)`, which keeps the first maximum on ties.
  - pucks sort by `(STATES.index, name)`. The other trees sort by `(not main, project, name)`. `trees()` sorts by `(project.lower(), not main, name.lower())`. Files in `git_facts` sort by `-(add+del)`, stable on insertion order.
  - history sorts by the `ts` string desc, stable (notes before moves on equal minutes), then `[:60]`.
  - `glob` order is filesystem-dependent but is re-sorted by mtime. Memory notes use `sorted(Path)`.
  - The kill order in `stop_shell` is set order.
- **pids:** `DIR/pid` content, and the process tree in `stop_shell`.
- **Random/threads/tmp:** none in these two scripts. greenlight.py imports ThreadPoolExecutor, but these paths don't use it.
- **Live LLM output:** design, suggest and ask. Use fakes.
- **Timing-dependent events:** whether a `usage{tps}` event appears for later deltas depends on the 0.5 s throttle. A fast fake makes only the first delta emit.

---

## 7. Python-isms a Rust port could silently get wrong

- **json.dumps defaults:** `", "`/`": "` separators, `ensure_ascii` (`→` becomes `\u2192`, `·` becomes `\u00b7`, `…` becomes `\u2026`, `❯` becomes `\u276f`), surrogate pairs for astral chars, insertion order (`preserve_order`). The indent modes use `","`+newline. Python's `json.loads` accepts `NaN`/`Infinity` and dumps them bare. Big ints are exact. Floats use the shortest repr (`1.0`, `1e+100`; serde gives `1e100`). Duplicate keys: the last value wins and the key keeps its first position.
- **Truthiness with value return:**
  - `"mr": mr and {...}` emits `{}` (not null) when there is no MR (d:250, observed).
  - `idle_h = w.get("lastOutputAt") and round(...)` yields `null`, `0` or a float (d:238).
  - `block_of` `... and X or None`.
  - In `moves`, `on[...]` values are truthy objects (lists, strings), not bools.
  - `vals[...] or "none"` turns `0`, `""`, `[]` and `None` into `"none"` (d:149).
- **`round(x, 1)`** is correctly rounded on the binary value, with half-even ties (`round(0.25,1)==0.2`). Rust has no direct equivalent: use format-and-parse or a decimal approach. It applies to `idle_h` and `tps`.
- **`str.capitalize()`** lowercases the rest (`"API retry"` becomes `"Api retry"`; `merge_how` gives `"Squash"`; opencode `tool.capitalize()`; codex item types; claude `status`).
- **`str.split()`/`strip()`/`isspace()`/`splitlines()`** use Python's whitespace set, which includes `\x1c`-`\x1f` and `\x85`. `splitlines` also breaks on `\x0b`, `\x0c`, `\u2028` and `\u2029`. Rust's `split_whitespace`/`lines` differ. This affects `" ".join(x.split())` everywhere (goals, prompts, short()), git status line counts, and history parsing.
- **`str.lower()`** is Unicode (`_slug`, `trees` sort, `name.lower()`).
- **Slicing is by code point** (`[:160]`, `[-400:]`, `[:8000]`, `[-4000:]`, `[:30000]` on JSON text, `title[:40]`, `task_title` cap math): never slice bytes. The stdin caps (`[:8000]`, `[:4000]`, `[:2000]`, `read(8193)`, `[:20<<20]`) are in **chars**, but `repo_cfg` checks `st_size <= 8192` in **bytes**.
- **Regex:**
  - `\w`/`\d`/`\s` are Unicode in `str` patterns. Python `\w` is `isalnum()` plus `_`; Rust's Unicode `\w` adds marks and connector punctuation, which is close but not identical.
  - `$` in `re.match(...$)` also matches before a trailing `\n` (NAME_RE d:42, FILE_RE d:850, MENU_RE d:590, HIST_RE, IGNORED c:37). Rust `$` (without `m`) does not. Use `\z`-style semantics deliberately.
  - Greedy `\{.*\}`/`\[.*\]` with DOTALL take the span from the first opener to the last closer.
  - `re.sub` with a lambda is one pass (d:149).
  - The lookahead `[.!?](?=\s|$)` in task_title needs fancy-regex or a manual check. **The Rust `regex` crate has no lookaround.**
  - `\{(\w+)\}` substitution leaves unknown names verbatim.
- **`isinstance(g, int)` accepts `True`/`False`** (d:155, d:988), so `{"ci": true}` counts as gate 1 and is stored as `true`.
- **`str(o)`** on non-string JSON gives the Python repr (`True`, `None`, `{'a': 1}`): ask options d:661, codex `turn.failed` `str(d.get("error"))` c:163, opencode `str(e)` c:185.
- **`str(e)` of exceptions reaches stdout** in many places (`[:200]`/`[:300]`), e.g. `"designer failed: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"` and `"[Errno 2] No such file or directory: 'codex'"`. These can't match byte-for-byte, so fixtures should assert only on prefix or key presence.
- **`base64.b64decode`** is non-strict: it discards non-alphabet chars (whitespace allowed by FILE_RE), and bad padding raises `binascii.Error` (a ValueError). Exact decoded bytes for odd inputs differ from strict Rust decoders.
- **`Path(name).name`:** `..` gives `".."`, which sanitises to `""` and then `"file"`. `"a/."` gives `"a"`. `""` gives `""`.
- **`str.rpartition(":")`** for the cursor. `int(idx)` accepts Unicode digits (the regex already limits them to `\d`).
- **`time.strftime('%-d')`.**
- **`max(..., key=len)` and `max(..., key=updatedAt)`** return the first maximal item. `sorted(reverse=True)` stays stable (it does **not** reverse equal items), so `sort_by(...).rev()` would break it.
- **`next(gen)` raising StopIteration** in `_free` (d:892) when 99 names are taken surfaces as `{"error": ""}` through main's `except Exception`.
- **Exceptions:** an uncaught one means a traceback on stderr and exit 1. `SystemExit(str)` means stderr and exit 1. `except Exception` does **not** catch SystemExit, so `facts()`'s exit escapes `suggest`'s wrapper.
- **`returncode -15`** for a signal death (Rust: `ExitStatusExt::signal()`).
- **`text=True` pipes** do universal newline translation and decode UTF-8 strictly. Invalid UTF-8 from a child raises UnicodeDecodeError (a ValueError) in chat's line loop. The line iterator raises it outside the `json.loads` try, so `except ValueError` never sees it and the result is a traceback. Transcript reads use `errors="replace"`.
- **`dict.update`/`|` merge order** sets the output key order (e.g. `{"name","facts",**cached}`, and `cached | {"error"}` appended last).
- **`"x" in str`** substring checks: `"logged out" in action`, `"review it" in action` (TypeError if `action` is None), `"signoff" in cmd` (list membership).

---

## 8. Suspected bugs (not fixed)

1. **chat `run()` stderr deadlock** (c:229-269): stderr is a PIPE that is only read after `p.wait()`. A child that writes more than the pipe buffer (about 64 KB) to stderr blocks while chat.py blocks reading stdout, and both hang. Claude runs with `--verbose`, and stderr chatter from codex/opencode is plausible.
2. **Stale or reused pid** (c:365-367): `stop` sends `killpg` to whatever pid is in `DIR/pid` with no identity check. If `send` died without cleanup (ssh drop, a traceback from a Norm KeyError, or the BrokenPipe path where `pid` is never removed), a later `stop` can SIGTERM an unrelated process group that reused the pid. A `PermissionError` there gives a traceback.
3. **Stop race:** `pid` is written after `Popen`. A `stop` that lands before that write finds no pid (or the previous one), and the turn keeps running.
4. **Retry path leaks `bg.json`** (c:243-250): it removes only `pid`.
5. **Session-lost retry on any failure** (c:345): a crash after partial output re-sends the message with a fresh session. Already-streamed text and steps are duplicated, and the conversation context is silently dropped and replaced.
6. **The retry check emits nothing for the killed run's done,** but text deltas of "No response requested." were already streamed with `--include-partial-messages`. The app's `live` buffer then shows them until the next `text_reset`.
7. **`name.lower() not in ("greenlight", "greenlight")`** (c:323): the tuple has a duplicate, left over from the chief→greenlight rename. The old name ("chief") is no longer exempt.
8. **`page()` reads its cache without error handling** (d:749), unlike `cached_page` (d:739). A corrupt `pages/<n>.json` crashes `page` but not `snapshot`.
9. **`page` "no workspace" goes to stderr** (d:474, SystemExit). Swift discards stderr, so the app sees only a nonzero exit. Every other subcommand reports this case on stdout.
10. **compact checks only the claude session** (c:375) while honouring `--model codex`, and the `/compact` text would then go to codex.
11. **`_files` name collision** (d:907): the prefixed `f"{len(out)+1}-{name}"` is not checked against `seen`. Files `["3-a","a","a"]` give two `3-a` entries, the second overwrites the first on disk, and the brief lists both.
12. **task_new leaves `STATE/tasks/<name>/` behind** when worktree creation fails (d:927 before d:937). The next attempt gets a `-2` suffix. If `CONFIG.agents` is empty, `res` and `a` are unbound and the error is a NameError string.
13. **`tree_merge` rewrites `last.json` without a lock** (d:172-174) and can race a concurrent `greenlight.py scan`. It also re-reads the file between the check and the write.
14. **Name drift:** snapshot pucks use `last.json` `name` (the scan-time `displayName`, g:599). `run`, `park`, `ask`, `answer` and `transcript` look up the *live* `displayName` (d:548, 563, 595, 821). After a rename and before the next scan, the app's name no longer resolves (`"no workspace X"`).
15. **`MENU_RE` matches only a single digit** (d:590), so menus with 10 or more options lose items.
16. **`chat.py bg` and `bgstop` have no caller** in Greenlight.swift, and the docstring says "Every command streams JSON lines" though `new` and `stop` print nothing.
17. **`git rev-parse @{u}` has no timeout** (d:311), so `trees` could hang on a wedged repo or FS.
18. **`STATE` is not expanduser'd but chat's GREENLIGHT_HOME is.** With `GREENLIGHT_HOME="~/x"`, dash writes `instructions.md` to `./~/x/` while chat reads `$HOME/x/instructions.md`.
19. **`autorun-set` and `instructions-set` don't `mkdir` STATE.** On a fresh host they fail with FileNotFoundError, which becomes an error JSON.
20. **The `transcript` page can exceed `limit`** by its separator entry (limit+1). Cursors are positional, so a session that grew between calls shifts entries.
21. **`trees()` crashes entirely** (no wrapper) if any `last.json` row matching a live path lacks `next` or `agent`. It also mutates the scan row's `agent` while iterating, which is harmless.
22. **`snapshot` agent slicing** `(ag.get("toolInput") or "")[:160]` raises TypeError if Orca ever sends a dict.

---

## 9. Scenario seeds (tests → black-box fixtures)

General harness: a scratch `GREENLIGHT_HOME` with seeded `last.json`, `blocks.json` and `config.json`; `GREENLIGHT_ME=octo`; `ORCA_CLI_COMMAND=<fake orca>` that replies from a scenario file and logs argv; fake `glab`/`gh`/`git`/`claude` on PATH as needed; `HOME` set to a scratch dir (for transcripts). `PATH` must still resolve Python >= 3.12 when running the Python oracle.
Blockers for pure black-box conversion:
- `settings memory-*` writes `ROOT/memory`, the checkout, not STATE. The test overrides `dash.MEMORY` (td:258), so the port needs an override (env or flag) or the fixture must copy the scripts to a scratch ROOT.
- `trees` tests inject `ade_*` Python modules (td:202-209). Black-box tests need fake `cmux`, `herdr` and `t3` binaries shaped per `scripts/ade_*.py`, or must ensure all three are absent.

### tests/test_dash.py (a top-level script, not pytest)

| Lines | Scenario | Kind | Fixture notes |
|---|---|---|---|
| 7-26 | `state_of` table: waiting gives needs; error or "logged out" gives crashed; block gives "You're blocking: …"; working or ci gives working; me gives raised; greenlight or agent gives queued; done gives done; anything else gives quiet. Block precedence comes after waiting and crash. A merged MR is never blocked | **pure** | Black-box form: `snapshot` with last.json rows and `blocks.json` (needs `config.blocking.repos` non-empty, or `cached()` returns `{}`). The BLK fixture is at td:21 |
| 28-36 | `validate`: files can't be a puck, unknown or non-dict widgets are dropped, at most 3 tiles, kv is not a tile, log is not wide | **pure** | Black-box form: `page` with a fake claude returning that JSON (td:28-32) |
| 38-41 | `menu` / `picker` parsing of terminal tails | **pure** | Black-box form: `ask` with a fake orca `terminal read` tail (td:38-40 canned) |
| 46-48 | `answer` refuses when the draft is unsent, with no orca send | mock | Fake orca: worktree ps (waiting agent), terminal list, terminal read `{"draft":"half typed","tail":["❯"]}` |
| 50-55 | `answer` over a menu with free text: Esc, poll read (still menu, then closed `["❯"]`), then send text joined to one line + `--enter` | mock | orca reply sequence at td:52 (`None`, menu tail, `["❯"]`, `None`). `time.sleep` is patched, so black-box takes about 0.8 s |
| 56-58 | `answer` with a digit key sends `--text 2` without Enter | mock | MENU at td:50 |
| 60-66 | `pending_questions`: an open AskUserQuestion, then cleared by its tool_result | mock (transcript_files) | Black-box form: put a JSONL under `$HOME/.claude/projects/<mangled path>/`. Q at td:61 |
| 69-74 | `run` refuses working/waiting agents and unknown workspaces, with no orca launch | mock | fake orca worktree ps |
| 79-87 | `_claude_entries`: command-name, a reminder dropped, meta and sidechain dropped, thinking skipped, tool collapsed | **pure-ish** (file) | Black-box form: `transcript ws` with that JSONL (td:80-85) |
| 92-100 | `park` add/drop `(parked)`, idempotent, case-insensitive | mock | fake orca `worktree set` argv log. comment inputs at td:97-100 |
| 103-112 | `task_title` cuts | **pure** | Covered black-box through `task new` → `--comment` |
| 114-159 | `task new`: request.md, file sanitising (`../../etc/passwd`, dup gives `2-passwd`, `.hidden log` gives `hidden-log`), brief contents, display-name set, history note, free-name suffix `-3` (dir and ws taken), claude usage-limit falls back to codex, other errors don't fall back, rename failure ignored, validation errors (empty text, a non-data URL, over 15 MB) with no orca calls, unknown repo | mock | fake orca with `repo list` (REPO at td:115, needs `gitRemoteIdentity.canonicalKey`), `worktree ps`, `worktree create` returning `{"worktree":{"id":"w9"}}`, and scripted failures by argv match (td:118-128). REQ/B64 at td:116-117 |
| 161-163 | greenlight.evaluate / cleanup.evaluate on planner goals | not dash | Out of scope (greenlight/cleanup) |
| 165-172 | `mr_facts` GitHub: checks become jobs, no glab call | mock | GH_MR at td:165-167. Black-box form: `page` on a GitHub row with fake `gh api graphql` (shape per g:332+) |
| 173-180 | `snapshot` with a GitHub row: ref `#23`, forge, state queued, lifecycle steps from `ROOT/lifecycles/greenlight.md` | mock live_agents | last.json at td:173-176, live worktree at td:177-178 |
| 182-194 | the `suggestion` gate: none, matching sig, stale sig, missing label; `suggest` returns the cache without an LLM call | file | Seed `DASH/suggest/ws-gh.json`. Assert the fake claude was not invoked |
| 197-199 | `split_label` | **pure** | Black-box form: `suggest` with fake claude output |
| 202-223 | `trees`: ADE merge order, archived skipped, first ADE wins a path, the scan row joined, live agent beats scan, mr subset, key set | mock modules | Needs fake cmux/herdr/t3 CLIs. The orca fake must fail. `_wt` shape at td:204-206, scan row at td:211-213, expected dict at td:221-223 |
| 226-248 | `tree_info`, `created`, `tree_rm` risks (dirty, unpushed, pinned), main refused, `--force` argv, `tree_goal` collapse | real git + mock ADE | A real temp git repo and worktree (td:227-231), with the fake ADE call at td:233-237 |
| 250-252 | `tree_cfg` writes `.greenlight.json`; `repo_cfg` reads it back; a worktree is refused; non-objects are refused | real fs | the indent=2 file format |
| 258-273 | settings: lifecycle get, set, reset; bad repo; instructions-set; memory set (index rewrite with `—` and desc), memory delete, `MEMORY` refused | file | ROOT/memory override (see blockers). lifecycle doc of `greenlight` from `ROOT/lifecycles/greenlight.md` |
| 276-310 | `moves`: review prompt fill (`{sha}` in a title not re-filled, missing values become "none"), own prompt with `{default}`, prompt-get/set/clear, bad args, priority conflicts › ci › threads, merge only for a reviewer with a glab cmd, autorun gates (bad repo, filtered kinds, `auto` text, held goal, merge True, cleared) | file | r/mine fixtures at td:276-277, 291-293. Black-box form: `snapshot` or `trees` output `moves` |
| 312-322 | `tree merge`: refused while fixes are outranked; success argv `+ ["--sha","abc123"]`; last.json rewritten; then refused; unknown path | mock run | Fake `glab` that logs argv and exits 0. last.json rows at td:315/317 |

### tests/test_chat.py (pytest)

| Lines | Scenario | Kind | Fixture notes |
|---|---|---|---|
| 10-33 | Norm.claude: usage sum, Bash step title, step_done, text_reset, text, compact_boundary, result with window. Norm.codex: `-lc` stripping, ok, agent_message, Reconnecting status, turn.completed text. `step_title` for Read and mcp names | **pure** | Black-box form: a fake claude/codex emitting exactly these dicts (td:13-30) and asserting the event lines |
| 36-51 | **FAKE claude** script: logs stdin to `argv[0].log`; if the msg contains `BUG` and (is not a retry, or contains `STUBBORN`) it emits a task-notification user line then `result "No response requested."`, else `"Deleted it."`; every line carries `session_id:"s1"` | fixture | **Reuse verbatim** as the canned fake (tc:38-51). Point at it with `harnesses.claude.bin` or PATH |
| 68-75 | an ignored first reply leads to exactly one retry; prompt 2 = `RETRY_PREFIX + WRAP + msg`; one done "Deleted it."; the ignored text is never emitted | subprocess | `CHAT_DIR` scratch. `harness.binary` is monkeypatched (tc:59), so black-box needs a bin override |
| 78-81 | a stubborn retry is not repeated; the done text is "No response requested." | subprocess | same |
| 84-87 | a normal turn: one prompt, no retry | subprocess | same |
| 90-93 | the `IGNORED` regex table | **pure** | |
| 96-101 | bg shells: task_started gives a `bg` event with cmd from the tool_use; background_tasks_changed with `[]` empties it | **pure** | Black-box form: assert `DIR/bg.json` contents mid-run, or `chat.py bg` while `pid` exists |
| 104-106 | `agent_title` strips pasted_content and `/goal` | **pure** | Black-box form: `chat.py bg` with fake orca `worktree ps` (working agents) |
| 109-122 | `stop_shell` kills the matching descendant subtree; an unknown id gives False | real processes | Uses a real `ps`. Black-box form: seed `pid` with a live parent pid and `bg.json`, then run `chat.py bgstop b1` and expect `{"ok": true}` |

Not covered by any test (priority for new fixtures): `dash.py stream` and `record` history lines; `page` designer success, failure, cache and `--fresh`; `suggest` LLM path and cache write; `ask` prompt path (LLM options plus cache); `transcript` pagination and cursors plus codex rollouts; `snapshot` freeform rows, the `hidden` flag, `built`, other-trees list; `chat.py stop` end-to-end (probe recipe in §2); session-lost retry; `compact`; `new`; codex/opencode/pi streams through `run()` (implicit done, duplicate-done dedupe, rc -15/143, stderr error text); missing-binary error done; `instructions.md` and `--name` in the system prompt.
