# GitLab API rate limits: where greenlight thrashes the API

Investigation for issue #150. No code changes; this is the measured picture and a ranked list of fixes.

## TL;DR

- **The limit.** GitLab.com allows 2,000 authenticated API requests per user per minute (`RateLimit-Limit: 2000`,
  `RateLimit-Name: throttle_authenticated_api`, read from response headers). Every glab call greenlight makes shares
  that budget, and so does every glab call an agent makes.
- **The cost of a scan.** One `greenlight.py scan` makes **260 glab calls in 27 s** (measured: 86 GitLab rows, 69 open
  MRs). That's 3.8 calls per open MR, 8 at a time, up to 15 a second. Every open MR is fetched three times over (the
  MR, its discussions, its approvals) on every scan, whether it changed or not.
- **The average is fine.** Greenlight made **57,374 traced glab calls in 26.9 h (~2,130 an hour)** and started 201
  `greenlight.py` CLI scans (7.5 an hour), or **222 full scans (8.2 an hour)** including in-process `ws_state.py` (14)
  and `adopt.py` (7) calls. The median minute has no glab calls at all.
- **Bursts of overlapping scans cause the 429s.** At 09:11–09:12 on 2026-11-24, ten full scans overlapped: nine
  agents' pre-push scans and one `greenlight.py --help`. Traced traffic reached **1,260 calls in the 60 s before the
  first 429** at 09:12:21 and peaked at **1,693 calls in 60 s** across 09:11:27–09:12:27 (including the 429s). GitLab
  answered **475 calls with 429** between 09:12:21 and 09:13:17, 467 of them in the first 8 s (09:12:21–09:12:29).
  Those 429s aren't retried, so they became `error` rows on the board ("greenlight couldn't evaluate: glab api
  …/merge_requests/N: glab: …"), and three scans started during the burst crashed.
- **A bigger outage hides behind the 429s.** When the up-front open-MR listing fails, the whole scan crashes. That
  happened **36 times** in `greenlight.py` CLI scans plus **7 times** in `adopt.py`'s overnight fallback scans (**43
  listing crashes** in all). Overnight, every glab call from every script failed (59 of 59, 22:30–08:08) while `gh`
  kept working, so **20 automation ticks in a row (22:30–08:00)** did nothing for 9.5 hours.
  - 13 ticks were recorded `skipped` and 7 `ok`, and only one tick's agent pinged (`forge-auth`, 02:30).
  - Only 3 of the 43 crashes were rate limits. The other 40 (33 CLI, 7 `adopt.py`) matched none of `diag.classify()`'s
    patterns (`other`).
  - The cause was almost certainly an expired GitLab OAuth token. The same failure came back while this report was
    being finished, and its message (`oauth2: "invalid_grant"`) doesn't match `classify()`'s auth patterns.
- **What to fix.** First, stop agents running a full scan to refresh one workspace, and make full scans single-flight.
  Second, make scans survive failures: retry 429 with backoff, cap concurrency across processes, keep a failed
  project's or MR's last good row rather than crashing or blanking it, and ping when GitLab is down instead of
  skipping silently. The full ranked list is in section 6.

## 1. How this was measured

- **The diagnostic trail**, frozen at 2026-11-24 09:28:59 (`events.jsonl` and `.1`–`.3`): 99,723 events from
  2026-11-23 06:33, 26.93 h.
  - Every call made through `greenlight.run()` logs a `cmd` span with `tool`, `sub`, `ok`, `msg_class`, `pid` and
    `run` (the correlation id children inherit through `GREENLIGHT_RUN`).
  - 83% of the glab calls came from the host checkout `861982f`, the rest from about 20 older checkouts. The glab
    call paths are the same in all of them and in this branch's base, so the numbers apply to current `main`.
- **One instrumented scan**: `greenlight.py scan` with a logging `glab` shim on `PATH` that recorded each call's
  endpoint, with project paths and numbers stripped.
- **Live response headers** from `glab api -i`: `RateLimit-*`, `ETag`, and a conditional (`If-None-Match`) request.

What the trail can't see, so the real load is higher than every number below:

- glab calls that bypass `greenlight.run()`:
  - `cleanup.py`'s `run()`.
  - `audit.py`'s `_run()` and direct `subprocess.run()` (`create_draft_pr`, `checked_merge`), used for:
    - its gates;
    - the background `audit.py merge` a scan or `dash.py` `moves()` starts;
    - `same_target` → `forge_refs` (`glab mr view` and the target branch). That runs inside the scan's `evaluate()`
      for a mergeable row with a cached audit, every 5 s in `dash.py stream`, every 30–60 s (and on launch/actions) in
      `dash.py trees`, and across all rows in `dash.py snapshot()` (called by `page`, `suggest`, and `dwim.py`).
  - `orchestrate.py`'s `run_cmd()`, which executes a row's `next.cmd` (`glab mr update`, `glab mr merge`,
    `audit.py checked-merge`).
  - The glab calls inside `audit.py checked-merge`, used by `dash.py tree auto-merge` and `orchestrate.py`. The manual
    Merge button (`dash.py tree merge`) strips `checked-merge` and runs `glab mr merge --sha` through
    `greenlight.run()` (traced), as does `tree_ready`; `tree_signoff`'s `next.cmd` is local (`greenlight.py signoff`),
    though its optional `Run:` hook runs via untraced `bash -lc`.
  - `diag.py doctor` (`glab auth status` via `subprocess.run()`), on demand and inside `diag.py bundle` / `report`.
- Agents' own glab calls in their terminals, and CI jobs on the same token.

The Mac app reaches GitLab only through `ssh` into scripts on this host, so its traffic is in the trail except where
those scripts bypass `greenlight.run()` (such as `dash.py`'s `same_target` / `kick` calls and `diag.py doctor`).

## 2. Inventory: every path that calls GitLab

| Path | Calls GitLab via | What it fetches | How often |
|---|---|---|---|
| `greenlight.py scan` (`scan()`) | `gl`, `gl_all`, `mr_details` | Per project: the open-MR listing (paged); with a `milestone` stage, active milestones.<br>Per row with no open MR: `issues/:n/closed_by` or a `source_branch=` lookup.<br>Per open MR: the MR, its discussions (paged), its approvals; a failed pipeline's failed jobs; all jobs with `check=` stages; `/blocks` when blocked.<br>Per mergeable row with a cached audit: `audit.merge_verdict` → `forge_refs` (2 **untraced** calls). | every full scan (below) |
| `greenlight.py precheck` | `scan(save=False)`, then `adopt.plan()` | a full scan (~250 calls), then adopt's plan: a median of **309** more (adopt's queries and the blocking scan; the trail can't split them) | every automation tick |
| `greenlight.py report` | `scan()` | another full scan | every tick whose precheck found work |
| `greenlight.py <unknown>` (e.g. `--help`) | `scan()` | **a full scan**: `main()` treats any unrecognised subcommand as `scan` | 11 in 27 h; 9 of them run by the automation's own agent |
| `ws_state.py observe` | `greenlight.scan(save=True)` | a full scan | 14 in 27 h; 11 in ticks, 1–5 min after `report` |
| `adopt.py` (`candidates`, `review_done`, `rerequested`) | `gl_all`, `gl` | 2 global `merge_requests?scope=all` queries (author, reviewer); per review candidate `/approvals`, sometimes `/reviewers`; per blocked MR with no candidate, the MR; a full `scan(save=False)` when `last.json` is older than one tick (7 times overnight, each crashing on its first listing) | every tick (precheck plan, then apply) |
| `blocking.py` (`scan`, `check`) | `gl`, `gl_all` | Per roster repo: open MRs (or per active milestone), and the project.<br>Per MR: pipelines, failed jobs, discussions, approvals, reviewers, last commit, notes. | from `adopt.py` and direct `blocking.py` CLI runs (e.g. `SKILL.md` after `report`, or `--fresh`) whenever its 5-minute cache (`CACHE = 300`) is cold (in practice at least once per tick, inside precheck); `cleanup`, `dash` and `lifestage` only read the cache |
| `cleanup.py` (`Repo.mrs`, `Repo.pushed_heads`) | its own `run()` (**untraced**) | per workspace: linked MRs, a `source_branch=` listing, approvals for review MRs, MR versions | every tick |
| `orchestrate.py` | `mr_details`, `gl`; `run_cmd` (**untraced**) | a fresh `mr_details` (and milestone when unset on repos with a `milestone` stage) per row it acts on; `source_branch=` checks before a goal backfill or free-form dispatch (`branch_has_open_mr`); runs `next.cmd` (`glab mr update`, `glab mr merge`, `audit.py checked-merge`) | per action (no orchestrate events in this window: ticks ran the Claude engine) |
| `audit.py` gates and `audit.py merge` | its own `_run()` and direct `subprocess.run()` (**untraced**) | `glab mr view`, `merge_requests/:iid` (inline comments), the target branch, discussions, notes, `mr create`, `mr merge`, `mr diff` | every push / PR / post / merge gate, and every scan or `moves()` with a mergeable head |
| `dash.py page` / `suggest` (`facts` → `mr_facts`) | `mr_details`, `gl_all` | a full `mr_details` plus the head pipeline's jobs (~5 calls), even when the page's cached layout is reused | per puck page the app builds or re-suggests (323 runs in 27 h) |
| `dash.py stream`, `trees`, `snapshot()` (`page`, `suggest`, `dwim.py`) | `moves()` → `audit.same_target` / `audit.kick` (**untraced**) | 2 calls per mergeable MR with a fresh cached audit (or kicks background `audit.py merge` when missing), in a repo with auto-merge on | every 5 s (`stream`), every 30–60 s + launch/actions (`trees`), and on every `page`, `suggest` or `dwim` call |
| `dash.py` card actions | `tree merge`: `glab mr merge --sha` via `greenlight.run()` (strips `checked-merge`, calls `moves(r)`); `tree auto-merge`: `audit.py checked-merge` (**untraced**); `tree_ready`: `next.cmd` via `greenlight.run()`; `tree_signoff`: local `greenlight.py signoff` + optional `Run:` via `bash -lc` (**untraced**) | `glab mr merge` / `glab mr update` (and any `same_target` call inside `tree_merge`'s `moves(r)`) | per click / auto-merge |
| `dash.py forge` | `gl` | 3 global queries (review, mine, issues), or 2 per repo | on demand |
| `chat.py` | `mr_details`; the chat agent runs `greenlight.py scan` | `mr_details` per question, to check it isn't stale; a full scan when the agent follows its prompt's "rescans every workspace" line | per chat turn |
| Mac app (`App.swift` `rescan()`) | `ssh … greenlight.py scan` | a full scan | at launch and on ↻ |
| Agents in workspaces | `greenlight.py scan` | **a full scan before every push**, as told by the lifecycles' `mr_open` step, `dash.py` `PUSH_RULE`, SKILL.md's brief rules, `prompts/ci.md`, `prompts/threads.md`, `prompts/conflicts.md`, and the push gate's own "rescan" errors | every agent push, and again after each refused gate |
| `diag.py doctor` (`bundle`, `report`) | direct `subprocess.run()` (**untraced**) | `glab auth status` | on demand (and from Mac app bug reports) |
| `whoami()` | `glab api user`, else `gh api user` | the username, cached in `me.json` (on a glab 429 it falls back to, and caches, the GitHub login) | once |

## 3. Measured call counts

### Per scan

The instrumented scan (86 GitLab rows over 5 projects: 69 open MRs, 4 merged, 13 with no MR):

| Endpoint | Calls |
|---|---|
| `merge_requests/:iid/discussions` (paged) | 90 |
| `merge_requests/:iid` | 73 |
| `merge_requests/:iid/approvals` | 69 |
| `merge_requests?…` (open listings, paged; `source_branch=` lookups) | 23 |
| `pipelines/:id/jobs` | 2 |
| `milestones`, `repository/branches/:b`, `glab mr view` | 3 |
| **Total** | **260 in 27.0 s** |

Per-MR endpoints alone are 232 calls: 228 across the 69 open MRs (3.3 per open MR) plus 4 for the merged MRs (3.2 per
MR overall). The open-MR listing already returns every MR, so the per-MR `GET` repeats it only to read `head_pipeline`.

The trail agrees. Completed `scan` and `report` processes made a median of **242** glab calls over a median of 87
rows.

### Per path, over 26.9 h

Each call is put in exactly one group by its `run` id. A run that includes an automation tick counts as the tick; a
run with a chat turn counts as chat; any other run with a scan counts as a standalone scan.

| Origin | glab calls | Share | Per hour |
|---|---|---|---|
| Automation ticks (54, one every 30 min) | 33,947 | 59.2% | 1,261 |
| Standalone scans (agents' pre-push scans, app ↻, `--help`) | 18,574 | 32.4% | 690 |
| Chat turns (and the scans their agent runs) | 2,819 | 4.9% | 105 |
| `dash.py` (puck-page facts, forge) | 1,512 | 2.6% | 56 |
| `ws_state.py` with no run id | 522 | 0.9% | 19 |
| **Total (traced)** | **57,374** | | **2,130** |

Scans:

- 201 `greenlight.py` CLI scans started (7.5 an hour), 165 completed (6.1 an hour).
- By subcommand: `scan` 95, `precheck` 53, `report` 42, `--help` 11.
- `diag.span("scan")` wraps `greenlight.py:main()`, not `scan()` itself, so in-process callers emit no `scan` span:
  adding `ws_state.py observe` (14) and `adopt.py`'s overnight fallback scans (7, all crashed) brings the total to
  **222 full scans started (8.2 an hour)**.

Automation ticks:

- A tick lasts a median of 5 min (longest 12 min).
- A healthy tick makes a median of **~1,050** calls, about 300 a minute at its peak.
- Inside `precheck`, adopt's plan made a median of **309** calls after the scan: **10,064** in all, 17.5% of traced
  calls. That is adopt's queries plus the blocking scan, whose 5-minute cache is always cold at a 30-minute tick.
  The trail can't split the two.
- On 33 ticks, `precheck` and `report` each ran a full scan, a median of 113 s apart (from the end of precheck's scan to the start of report's).
  The second scans cost ~8,500 calls.
- 11 ticks then ran a third full scan in `ws_state.py observe`.
- 13 ticks were recorded `skipped`. Each made exactly 1 glab call: precheck crashed on its first listing, and
  `auto.py` records any nonzero precheck exit as skipped. Seven more ticks (02:30–05:30) had the same crash but were
  recorded `ok`: their agent ran anyway, and its `report` crashed too. No tick in this window had a working precheck
  that found nothing to do.

GitHub, for comparison, costs one GraphQL query per PR (`gh_pr`). There were 2,153 `gh` calls in the same window, and
no GitHub rate-limit errors.

### The rate-limit incident (2026-11-24, 09:11–09:13)

| | |
|---|---|
| Full scans overlapping | 10, each with 8 threads, all standalone: 9 agents' pre-push scans and 1 `greenlight.py --help`. No tick, `ws_state` or chat calls in the window. |
| Peak traced calls | 1,693 in the 60 s from 09:11:27; 651 in the busiest 10 s |
| 429 responses | 475, from 09:12:21 to 09:13:17; 467 of them between 09:12:21 and 09:12:29 |
| Effect | All 10 scans completed, 8 of them with 429 `error` rows. 3 more scans, started at 09:12:29, 09:12:59 and 09:13:11, crashed on the open-MR listing. |

The push gates of the agents in question ran from 09:10:48 to 09:12:54.

At the first 429 (09:12:21), the preceding 60 s held 1,260 traced calls, so at least ~740 untraced calls shared that
minute. If GitLab counts by clock minute it's more: only 719 traced calls came between 09:12:00 and the first 429.
The candidates are the dashboard stream's `same_target` calls, cleanup, audit, orchestrate, agents' own glab, and
CI. A live check while this report was
written, with none of its own scans running, showed 874 requests counted on the account in 40 s
(`RateLimit-Observed`).

Only 6 calendar minutes in the 27 h had more than 500 traced calls.

### The listing outage (2026-11-23 22:30 to 2026-11-24 08:08)

`scan()` lists each project's open MRs before it looks at any row, and nothing catches a failure there
(`for m in gl_all(f"projects/{pid(proj)}/merge_requests?state=opened")`).

- This crashed 36 `greenlight.py` CLI scans (33 with `msg_class: other`, 3 with `rate_limit`) plus 7 in-process
  `adopt.py` fallback scans overnight (all `other`): **43 listing crashes** in all.
- Overnight, the listing wasn't the cause: every glab call from 22:30 to 08:08 failed (59 of 59: greenlight 33,
  dash 17, adopt 7, blocking 1, chat 1), while every `gh` call worked. The last good glab call was at 22:14 and the
  next at 08:21. glab or the GitLab account was down.
- Twenty automation ticks in a row failed this way, from 22:30 to 08:00, along with six standalone scans. 13 ticks
  were recorded `skipped` and 7 `ok`. `auto.py` pings only on `failed`, so it sent nothing. The
  02:30 tick's agent sent the only ping, `forge-auth`.
- The trail keeps the class, not the message. glab's stderr matched none of `diag.classify()`'s patterns, not even
  the 401/403/token ones.
- Even so, the 02:30 agent read it as forge auth, and the outage ended minutes after `glab auth login` was run on the
  host (around 08:04). So the likely cause is an expired glab login whose message `classify()` doesn't recognise.
- **Seen again live while finishing this report (2026-11-24 10:4x).**
  - Every scan crashed on the first project listing with glab's `ERROR Oauth2: "invalid_grant" "The provided
    authorization grant is invalid, expired, revoked, …"`.
  - `glab auth status` reported the same.
  - `diag.classify()` returns `other` for that text, because none of the auth patterns (`401`, `403`, `token`,
    `log in`, …) match `invalid_grant` or `oauth2`.
  - So the overnight `other` failures fit an expired GitLab OAuth token, and the board turns it into a crashed scan
    rather than a "log in to glab" question.

## 4. How rate-limit responses are handled today

- **No retry on 429.** `gl()` calls `run(cmd, tries=3)` (`run()` itself defaults to 1 try). It retries only when
  stderr matches `TRANSIENT_RE` (5xx, gateway errors, timeouts, connection resets). 429 isn't in it, so a
  rate-limited call fails on its first try. Every glab call in the trail shows `tries: 1`.
- **No header-based backoff.** A retry waits `2 * 2**i` seconds (2 s, then 4 s), and never reads `Retry-After` or
  `RateLimit-Reset`. `glab api` shows headers only with `-i`, which puts them ahead of the JSON on stdout.
- **Classified, not handled.** `diag.classify()` tags the failure `msg_class: rate_limit`, so the trail shows the
  cause.
- **A row failure blanks the row.** A failure in a row's `find_mr` becomes `mr = None`,
  `next = {step: "error", owner: "me"}`, and `keep()` saves it. The board loses that MR's facts until the next good
  scan. That is the error the board shows.
- **A listing failure crashes the scan.** A failure in the up-front per-project listing isn't caught, so the whole
  scan crashes (section 3).
- **blocking.py**'s `scan()` skips an MR whose check failed, but raises when a repo's up-front listing fails or every
  check fails; `blocks()` catches all exceptions and returns `None`.
- **No coordination across processes.** Each scan has its own `ThreadPoolExecutor(8)`, so N concurrent scans make up
  to 8N concurrent calls.

## 5. Redundant, duplicated and N+1 calls

1. **A full scan stands in for a one-workspace refresh.** Seven prompt sites tell agents to run `greenlight.py scan`
   before every push "to refresh this workspace's goal and MR" (section 2). The push gate needs that row in
   `last.json` (`audit.py` `row_for`), and when it refuses a stale row it says "rescan", which costs another full
   scan. Each costs 260 calls; the refresh needs about 4.
2. **No single-flight.** Two scans 10 s apart both run in full. Nothing waits for a scan already running.
3. **A tick scans two or three times.** `precheck` scans with `save=False`. Then `report` scans again about 2
   minutes later, and `ws_state.py observe` often scans a third time 1–5 minutes after that. Observe scans so that
   an old `last.json` can't undo a generated goal. That needs fresh goals from the ADE and a re-evaluation of the
   rows whose goal changed, not a fresh GitLab fetch of every MR.
4. **Unchanged MRs are fetched in full every scan.** The listing returns `updated_at`, `sha`, `has_conflicts` and
   `detailed_merge_status` for each MR, but `mr_details` refetches the MR, discussions and approvals for all 69.
5. **N+1 per MR.** Three or more REST calls per open MR, where GitLab GraphQL can return a project's open MRs with
   their head pipeline, approvals and discussion state in a few paged queries.
6. **Unknown subcommands scan.** `greenlight.py --help` runs a full scan. The automation's agent does this on some
   ticks (9 times), probably to read the usage, and gets a scan instead.
7. **The same MR fetched by several scripts in one tick.** The precheck scan reads every workspace MR's approvals.
   Then `blocking.py` (per roster MR), `cleanup.py` (per review workspace), and `orchestrate.py` (`mr_details` per row
   it acts on) read approvals again; `adopt`'s `review_done` skips already-covered workspace rows and blocked
   candidates, but still duplicates `blocking.py` for uncovered roster MRs and repeats across `precheck` →
   `adopt.py apply`. None of them share a cache.
8. **Puck pages refetch what the scan has.** `dash.py page` and `suggest` call `mr_facts` (~5 calls) per page. The
   page shows those facts live, but most are already in the scanned row, and `mr_facts` refetches jobs that
   `mr_details` already read on failed pipelines or repos with `check=` stages.
9. **Rows with no MR are looked up again every scan.** The 13 GitLab rows with no MR each make a `source_branch=` or
   `closed_by` call per scan.
10. **The blocking scan runs every tick.** Its 5-minute cache always expires between 30-minute ticks. So adopt's plan
    re-reads pipelines, discussions and approvals of every open roster MR, including the MRs the scan read seconds
    earlier. That is a large part of the median 309 calls adopt's plan makes each tick, though the trail can't split
    them.

## 6. Ranked fixes

Ranked by calls saved and failures removed per unit of work.

### 1. Refresh one workspace before a push; make full scans single-flight

- **Let the push gate check the push itself (simplest).**
  - `gate push` already makes a `glab mr view` and reads the live head and target (`forge_refs`).
  - With the MR iid from the existing row (or one `source_branch=…&state=opened` lookup when there is none), the goal
    from the ADE combined with `goal_backfill.json` (`goal_with_backfill()`, so backfilled goals aren't rejected as
    `no principal goal`), and the MR title and description from that same view, it can check the push without a scan,
    at 0–1 extra calls.
  - That removes the pre-push scan from all seven prompt sites. Because `glab mr view` + ADE does not fetch
    discussions, approvals or jobs, `gate push` can only apply a **partial row patch** (updating `goal` and
    `mr.{iid,sha,title,description,target_branch,source_branch}`) onto an existing row.
- **Keep a full one-row `greenlight.py scan --ws <workspace>`** for cases a partial patch can't cover:
  - New workspaces not yet in `last.json` (`row_for(path)` is `None`, e.g. `mr_open` / `gate pr`), and
    `gate post --kind review` (which never runs `gate push`), plus Fix 3's post-adopt and goal-change refreshes.
  - It must look up `state=opened` before `state=all`, as the full scan's `open_by_branch` does, or it can pick a
    different MR.
  - Its `evaluate()` calls `merge_verdict`, which can start an audit.
  - GitHub stacked PRs need `_base_mr`, which comes from a listing.
- **Writing one row safely: a new patch kind, modelled on `dashboard_merges`.** `last.json` is shared state.
  - `keep()` already replays `dash.py`'s card-merge patches onto an older full scan instead of dropping that scan.
    But that replay only handles merges: it applies to rows still `opened`, marks the MR `merged`, and copies only
    `next` and `life`.
  - A one-row refresh needs its own patch kind, keyed by workspace `id` and `path`, distinguishing partial field
    patches (`gate push`) from whole-row replacements (`scan --ws`). Stamp each patch with a monotonic
    `patch_started_ns` so `keep()` replays only patches newer than `result["scan_started_ns"]` (newest-wins) and prunes
    older ones — preventing both lost patches and an earlier patch overwriting a newer full scan.
  - Any write that bumps `scan_id` (the card-merge write does) makes in-flight `ws_state` card updates raise "scan
    superseded". Pushes happen more often than scans, so the one-row write should keep `scan_id` (and update
    `STATE / "scan_id"`'s recorded inode after replacing `last.json`, keeping `ws_state`'s fast-path check intact), or
    `observe` should tolerate patch-only changes.
  - It must not bump `ts` either: `adopt.current_rows()` trusts the whole file while `ts` is fresh, and `dash.py`
    exposes `ts` as the last full-scan timestamp (`scanned`).
  - Write the whole merged row list. `close_stale()` resolves the question of any workspace missing from `rows`, and
    `ws_state.keyed()` reads every row's name to pick card keys.
- **Single-flight for full scans.** App ↻, chat ("rescan every workspace") and automation scans take a new lock held
  for the network part. Today there is only the short write lock in `keep()` (`backfill_lock` plus `ws_state.lock`),
  which goal backfills and card writes also take, so it must not be held for the whole scan.
  - Note that requiring a waiter to reuse only a scan that *started after it asked* serializes two overlapping scans
    into two full scans (only 3+ coalesce to 2). Passive callers (`app ↻`, ticks) should reuse a leader that finished
    within a short freshness window (a few seconds); callers that need fresh post-edit state (pushes, `gate pr`/`post`)
    never reuse a full scan and instead use the one-row path.
  - Because `precheck` runs `scan(save=False)` (skipping `keep()`, `ws_state.keyed(migrate=True)`, `_scan_id`, backfill
    pruning and `close_stale()`), a `save=True` waiter cannot reuse `last.json` from a `save=False` leader unless the
    leader writes a side file (Fix 3) that the waiter still passes through the full save path.
- **Saves:** most of the standalone share (32%) and the scans in chat, and it removes the burst that caused the
  incident. Ten pushes at once would cost about 40 calls instead of about 2,600.
- **Risk:** medium, because of the `last.json` invariants above.

### 2. Make scans survive failures

- **Retry 429.** Add `429` and `Too Many Requests` to the retryable set, with a jittered 5–30 s wait and more tries
  for 429.
  - Reading `RateLimit-Reset` would need `glab api -i` and header parsing before `json.loads`. It's worth it later,
    not needed for a first fix.
  - A retry must not hold a concurrency slot while it sleeps.
- **Cap concurrency across processes.** Use a small file-based semaphore under `~/.greenlight` (e.g. 8 `flock`ed slot
  files), so N processes share 8 slots instead of using 8N.
  - At the measured median latency (0.57 s), 8 slots allow about 840 calls a minute from greenlight. With at least
    ~740 a minute of untraced traffic in the incident, that's still close to the limit.
  - Set the cap from measured traffic after fix 7 routes every caller through `run()`, or use a shared token bucket.
- **Budget the retries.** Precheck runs under `auto.py`'s 300 s timeout (median 109 s today, max 220 s), so cap the
  total retry wait per scan. Otherwise backoff can time a tick out.
- **Catch a failed per-project listing.** A failure on one project must not crash the scan or the tick.
- **Keep the last good row, and gate callers on `stale`.** On a failure that can be retried (429, 5xx, timeout, a
  failed listing), carry the previous row's `mr`, `next` and `life` forward with a `stale` flag and the error class.
  - Don't blank `mr`: that hides the MR from the board, and makes `gate push` / `gate pr` treat the workspace as
    having no MR.
  - Don't change `owner` either. `close_stale()` resolves a workspace's open question as soon as its owner isn't
    `me`, so a 429 would silently close real questions.
  - Do drop `next.cmd` from a stale row and explicitly gate `stale` rows out of every automated/UI caller — only
    `orchestrate.py` re-checks full MR state live (manual `tree_merge` only pins `--sha`, `tree auto-merge` checks
    head/target OID, and non-merge auto-run does no live check at all):
    - exclude `stale` rows from `precheck`'s actionable count (otherwise `precheck` exits 0 and launches paid agent
      ticks during a GitLab outage);
    - skip `stale` rows at the top of `orchestrate.py`'s `plan()` alongside `_mr_inconclusive` (so `plan()` doesn't
      run `decide_choice()` LLM calls or goal backfills before `act()`'s live check);
    - suppress `m["auto"]` (non-merge auto-run for `conflicts`, `ci`, `threads`, `review`) and mutation/sign-off
      actions in `dash.py moves()` when `r.get("stale")` is set.
- **Say when GitLab is down.** When every GitLab call in a scan fails, as in the overnight outage, greenlight itself
  should send one keyed ping (e.g. `@gitlab-down`) and resolve it on the first good scan.
  - An exit code isn't enough. The precheck is the owner's shell command, and it recorded the same crash as `skipped`
    on 13 ticks and `ok` on 7.
  - `auto.py`'s failure ping has no key, so it would repeat on every tick.
  - `ping()`'s `recent_keys()` suppresses any `@`-key for `FLAP` (24 h) without checking whether it was resolved, so a
    second outage within 24 h would be silently muted. Check `pinged()` directly and pass `ask=True` (or exclude
    resolved outage keys from `recent_keys()`).
- **Record why.** Add `invalid_grant` (not bare `oauth2` — `auth` precedes `timeout`/`network`/`transient` in
  `diag.CLASSES`, and Go's `oauth2` package prefixes transient/DNS refresh failures with `oauth2:`) to
  `diag.classify()`'s `auth` pattern; that alone would have named the overnight outage. Also add `"no such host"` to
  `network`, record glab's HTTP status on `cmd` spans as an `int` (e.g. `http_status=429`, which passes `diag.clean()`
  without adding a string key to `diag.KEYS`), and record a counter (e.g. `rate_limited_tries`) so 429s that succeed on
  retry aren't hidden when `_run()` clears `msg_class`.
- **Ask for a login.** When the class is `auth`, the ping should say so ("glab's GitLab login expired: run `glab auth
  login`") rather than leave crashed scans.
- **Saves:** few calls, but it turns 429s into short delays, and turns a 9.5-hour silent outage into one ping.
- **Risk:** low, once fix 1's single-flight stops waiting scans piling up behind backoff.

### 3. Scan once per tick

- Precheck saves its result to a side file keyed by `GREENLIGHT_RUN`. `report` in the same run reuses it when it's a
  few minutes old, and refreshes only the workspaces `adopt apply` created (with fix 1's one-row path).
- `ws_state.py observe` re-reads goals from the ADE.
  - `last.json`'s `r["mr"]` is a projected summary dict (`author` is a string, keys are renamed, and `_discussions`,
    `_approvals_left`, `_can_approve`, `detailed_merge_status`, etc. are dropped), so `evaluate()` cannot run directly
    on it.
  - For rows whose goal changed, either refresh just those rows via fix 1's one-row path, or re-run `evaluate()`
    offline against fix 5's raw `mr_details()` cache (plus cached active milestones) and fall back to fix 1's one-row
    path when the new goal changes which MR the row tracks (`Merge MR for #N`).
- The blocking scan can reuse fix 5's raw per-MR cache (which already retains `_discussions`, extended to also preserve
  `approval_rules_left`) rather than `last.json` rows, while still fetching `blocking.py`-specific fields
  (`/reviewers`, `/commits?per_page=1`, `/notes`, and failed jobs on `pending`/`running` pipelines) where needed. Up to
  17.5% of traced calls; how they split isn't measured.
- **Saves:** about 8,500 calls from the second scans on 33 ticks, plus about 2,900 from 11 observe scans: about 20% of
  traced calls, before any blocking saving.
- **Risk:** medium.
  - `report` must still see what `adopt apply` and the agent just did.
  - Precheck's `save=False` skips `ws_state.keyed(migrate=True)`, `_scan_id`, `keep()` / `close_stale()`,
    `decisions.backfilled_goal()` and backfill pruning — and `backfill_read` / `prune_candidates` /
    `superseded_backfills` are local to `scan()` today. The side file must persist those inputs (or recompute them) so
    `report` runs the full save path cleanly.
  - Orchestrate re-reads the live MR before it acts, so a stale row makes it skip an action, not take a wrong one.

### 4. Stop unknown subcommands from scanning

- `main()` prints the usage docstring and exits `0` for `--help` / `-h` (matching `argparse` scripts like `diag.py`
  and `adopt.py`, so agents checking usage don't see a failed command), and exits nonzero for unknown subcommands. Only
  `scan`, `report`, `precheck` or no argument scan (decide explicitly whether to keep or deprecate bare undocumented
  `greenlight.py --json`). The named subcommands (`note`, `ping`, `resolve`, `ade`, `signoff`, `history`) already
  return before the fallthrough.
- **Saves:** 11 full scans in 27 h (~2,700 calls). The automation's agent gets the usage it was asking for.
- **Risk:** none.

### 5. Skip unchanged MRs

- Cache each open MR's raw `mr_details` result keyed by `(project, iid)`, fingerprinted on the listing's `updated_at`,
  `sha`, `has_conflicts`, `detailed_merge_status`, `blocking_discussions_resolved` (plus `draft`, `labels`,
  `milestone`, `assignees`, `reviewers`, `target_branch`, `title`, `description`), and the cached head pipeline's id
  and status. When the fingerprint matches, reuse the cached discussions and MR metadata.
- **Check first:** new notes and pushes change `updated_at`, while approvals, thread resolution and pipeline status may
  not. Use a short TTL (e.g. 10 min), and always refetch approvals for rows whose next step depends on `_approved_by`
  (`approved`, `review`, and human-approval-waiting `merge` rows — 15+ of 69 in the median scan). Note that refetching
  the head pipeline on every unchanged MR (69 calls) plus 15+ approval calls still leaves ~85–100+ per-MR calls; also
  caching terminal pipelines within the TTL is needed to cut most of the 232 per-MR calls.
- **Saves:** most of the 232 per-MR calls per scan on a quiet board (once terminal pipelines are cached too).
- **Risk:** medium. A stale approval or thread count shows the wrong next step until the TTL runs out.

### 6. Batch with GraphQL per project

- One paged `glab api graphql` query per project for `mergeRequests(state: opened)` covering MR headers (`assignees`,
  `reviewers`, `labels`, `milestone`, `description`), `headPipeline`, approvals (`approvedBy`, `approvalsRequired`,
  `approvalsLeft`, `userPermissions`), `detailedMergeStatus`, `conflicts`, and `blockingDiscussionsResolved` —
  mirroring `gh_pr` for GitHub.
- `evaluate()` and `one()` also need per-note discussion details (`awaiting_me` reads the last note's author;
  `thread_line` reads the first note's author, body and diff position; `_reviewed_sha` scans every note's author,
  `created_at` and `position.head_sha`), plus conditional failed jobs, `check=` jobs and `/blocks`. Nesting
  `mergeRequests → discussions → notes` in one query hits GitLab's GraphQL complexity ceiling and requires nested
  pagination, so pair the batched GraphQL header/pipeline/approval query with fix 5's cache and selective per-MR
  follow-ups for changed discussions and jobs.
- **Saves:** cuts 260 calls per scan toward ~5–20 on a quiet board (higher when many threads or failed jobs change).
- **Risk:** the largest change. GraphQL has complexity limits, and every field `evaluate()` reads has to map from the
  REST shape. Worth doing after fixes 1–5, if traffic is still high.

### 7. Trace everything; share one memo per tick

- Route `cleanup.py`, `audit.py` (both `_run()` and direct `subprocess.run()` in `create_draft_pr` and
  `checked_merge`, including `same_target` / `forge_refs`), `orchestrate.py`'s `run_cmd`, and `diag.py doctor`
  (`glab auth status`) through `greenlight.run()` (or a `diag.span("cmd")`). The incident shows untraced traffic
  matters: at least ~740 calls in its peak minute.
- Cache `forge_refs` for a few seconds **only in the advisory polling/display callers** (`dash.py stream`, `trees`,
  `snapshot()`), never inside `audit.forge_refs()` or any gate / `checked-merge` / `merge_audit` / `tree_merge` path —
  those rely on live back-to-back TOCTOU checks.
- Share a short-lived per-run `gl()` GET cache only across read-only scan/plan phases (`adopt`, `blocking`),
  **excluding** `orchestrate.py`'s live re-checks (`current_mr_state`, `guard_merge`, `act_one`, `branch_has_open_mr`)
  and post-`act()` `cleanup.py` reads so mutations never act on pre-mutation state.
- **Saves:** unknown until the untraced calls are traced, which is why this half comes first.

### 8. Build puck pages from the scanned row

- Keep `mr_live`, because the page renders it live. Persist `pipeline_id` and `updated_at` in `last.json`'s `r["mr"]`
  (neither is saved today) so `mr_live` can be built from the scanned row plus one jobs call (or cached for a minute),
  and have `mr_facts` reuse `mr["_checks"]` from `mr_details` when present (filtered to `dash.py`'s active job scopes:
  `failed`, `success`, `running`, `pending`). The layout cache isn't the waste.
- **Saves:** most of `dash.py`'s 1,512 calls.

### 9. Remember "no MR" for a while

- Cache negative `source_branch=` and `closed_by` lookups per row for a few minutes, and clear the entry when the PR
  gate creates one.
- **Saves:** up to ~13 calls per scan here.

### Not a rate-limit fix: ETag / If-None-Match

- GitLab sends `ETag` on these endpoints (`merge_requests/:iid`, `/discussions`, the listings) and answers a matching
  `If-None-Match` with `304 Not Modified`.
- **A 304 still counts against the limit.** Three conditional requests in a row raised `RateLimit-Observed` just as
  full ones did, and `glab api` exits 1 on a 304 (`glab: HTTP 304`).
- ETags save bandwidth and JSON parsing, not quota. They're worth using only inside fix 5's cache.

### Not recommended: scanning less often

Ticks are 59% of traced calls, but they're a steady ~300 calls a minute for a few minutes every half hour, and they
had no part in the incident. Fix 3 cuts their cost by about a third without making the board staler.

## 7. Suggested order and how to re-measure

1. Fix 4 (trivial), fix 7's tracing (and `dash.py` advisory `forge_refs` caching), and the non-backoff resilience pieces
   of fix 2 (catch listing failure, `diag.classify()`, outage ping, stale-row preservation and caller gating).
2. Fix 1 (removes the bursts and adds single-flight), paired with fix 2's 429 retry backoff and cross-process
   concurrency cap sized from fix 7's newly traced numbers.
3. Fix 3's precheck→report reuse and `observe` one-row/offline refresh.
4. Re-measure, then ship fix 5 (which also unlocks fix 3's blocking-scan cache sharing), fix 8, fix 9, and fix 6 if
   traffic is still high.

`diag.py summary` and `errors` show the rate-limit error counts (`top_errors` with `msg_class: rate_limit`), but not
calls per scan or calls per minute. This prints both from the trail:

```bash
python3 - <<'EOF'
import collections, json, os, statistics
base = os.path.join(os.path.expanduser(os.environ.get("GREENLIGHT_HOME", "~/.greenlight")), "diag", "events.jsonl")
ev = []
for path in [f"{base}.{i}" for i in (3, 2, 1)] + [base]:
    if not os.path.exists(path):
        continue
    with open(path, errors="replace") as f:
        for line in f:
            if line.strip():
                try:
                    ev.append(json.loads(line))
                except ValueError:
                    pass
all_ts = [e["ts"] for e in ev if isinstance(e.get("ts"), (int, float))]
g = sorted((e for e in ev if e.get("kind") == "cmd" and e.get("tool") == "glab"), key=lambda e: e["ts"])
by_rp = collections.defaultdict(list)
for e in g:
    by_rp[(e.get("run"), e.get("pid"))].append((e["ts"], e.get("tries", 1)))
scans = [
    sum(tr for t, tr in by_rp[(s.get("run"), s.get("pid"))] if s["ts"] - s.get("secs", 0) - 1 <= t <= s["ts"] + 1)
    for s in ev if s.get("kind") == "scan" and s.get("ok") and s.get("op") in ("scan", "report", "precheck")
]
j, win, peak = 0, 0, 0
for i, e in enumerate(g):
    win += e.get("tries", 1)
    while g[j]["ts"] < e["ts"] - 60:
        win -= g[j].get("tries", 1)
        j += 1
    peak = max(peak, win)
hours = round((max(all_ts) - min(all_ts)) / 3600, 1) if len(all_ts) > 1 else 0.0
print({"glab_attempts": sum(e.get("tries", 1) for e in g), "hours": hours,
       "median_calls_per_scan": statistics.median(scans or [0]), "peak_calls_per_60s": peak,
       "rate_limited": sum(e.get("msg_class") == "rate_limit" for e in g)})
EOF
```

Measurement caveats as fixes land:

- Once fix 2 retries 429s, `_run()` clears `msg_class` on each retry, so `rate_limited` above counts only calls whose
  final attempt failed with 429 until fix 2 also logs a `rate_limited_tries` counter (weighting by `tries` keeps
  attempt totals and peaks accurate).
- Direct in-process `scan()` calls (`ws_state.py`, `adopt.py`) emit no `scan` span until `diag.span("scan")` moves into
  `scan()`.
- If fix 1 keeps `greenlight.py scan --ws`, give it a distinct `op` (e.g. `scan_ws`) so 1-row refreshes don't skew
  `median_calls_per_scan`.

After the fixes, the peak per 60 s should stay well under 2,000, and the median calls per scan should fall with
fixes 5 and 6.
