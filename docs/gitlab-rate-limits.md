# GitLab API rate limits: where greenlight thrashes the API

Investigation for issue #150. No code changes; this is the measured picture and a ranked list of fixes.

## TL;DR

- **The limit.** GitLab.com allows 2,000 authenticated API requests per user per minute (`RateLimit-Limit: 2000`,
  `RateLimit-Name: throttle_authenticated_api`, read from response headers). Every glab call greenlight makes shares
  that budget, and so does every glab call an agent makes.
- **The cost of a scan.** One `greenlight.py scan` makes **260 glab calls in 27 s** (measured: 86 GitLab rows, 69 open
  MRs). That's 3.8 calls per open MR, 8 at a time, up to 15 a second. Every open MR is fetched three times over (the
  MR, its discussions, its approvals) on every scan, whether it changed or not.
- **The average is fine.** Greenlight made **57,374 traced glab calls in 26.9 h (~2,130 an hour)** and started 7.5
  full scans an hour. The median minute has no glab calls at all.
- **Bursts of overlapping scans cause the 429s.** At 09:11–09:12 on 2026-11-24, ten full scans overlapped. Agents
  start them before every push. They made **1,693 calls in 60 s**, and GitLab answered **475 of them with 429**.
  Those 429s aren't retried, so they became `error` rows on the board ("greenlight couldn't evaluate: glab api
  …/merge_requests/N: glab: …"), and three scans started during the burst crashed.
- **A bigger outage hides behind the 429s.** When the up-front open-MR listing fails, the whole scan crashes. That
  happened **36 times**, including **20 automation ticks in a row (22:30–08:00)** that did nothing for 9.5 hours.
  The automation recorded each one as "skipped". Only 3 of the 36 were rate limits; the cause of the other 33 isn't
  recorded (`msg_class: other`).
- **What to fix.** First, stop agents running a full scan to refresh one workspace, and make full scans single-flight.
  Second, make scans survive failures: retry 429 with backoff, cap concurrency across processes, and keep a failed
  project's or MR's last good row rather than crashing or blanking it. The full ranked list is in section 6.

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

- glab calls that bypass `greenlight.run()`: `cleanup.py`'s `run()`, `audit.py`'s `_run()` (including the background
  `audit.py merge` each scan starts for mergeable heads), and `orchestrate.py`'s `run_cmd()`, which executes a row's
  `next.cmd` (`glab mr update`, `glab mr merge`).
- Agents' own glab calls in their terminals, and CI jobs on the same token.

The Mac app reaches GitLab only through `ssh` into scripts on this host, so its traffic is in the trail.

## 2. Inventory: every path that calls GitLab

| Path | Calls GitLab via | What it fetches | How often |
|---|---|---|---|
| `greenlight.py scan` (`scan()`) | `gl`, `gl_all`, `mr_details` | Per project: the open-MR listing (paged); with a `milestone` stage, active milestones.<br>Per row with no open MR: `issues/:n/closed_by` or a `source_branch=` lookup.<br>Per open MR: the MR, its discussions (paged), its approvals; a failed pipeline's failed jobs; all jobs with `check=` stages; `/blocks` when blocked. | every full scan (below) |
| `greenlight.py precheck` | `scan(save=False)`, then `adopt.plan()` | a full scan, then adopt's candidate queries and (cache cold) the blocking scan | every automation tick |
| `greenlight.py report` | `scan()` | another full scan | every tick whose precheck found work |
| `greenlight.py <unknown>` (e.g. `--help`) | `scan()` | **a full scan**: `main()` treats any unrecognised subcommand as `scan` | 11 in 27 h; 9 of them run by the automation's own agent |
| `ws_state.py observe` | `greenlight.scan(save=True)` | a full scan | 14 in 27 h; 11 in ticks, 1–5 min after `report` |
| `adopt.py` (`candidates`, `review_done`, `rerequested`) | `gl_all`, `gl` | 2 global `merge_requests?scope=all` queries (author, reviewer); per review candidate `/approvals`, sometimes `/reviewers`; per blocked MR with no candidate, the MR | every tick (precheck plan, then apply) |
| `blocking.py` (`scan`, `check`) | `gl`, `gl_all` | Per roster repo: open MRs (or per active milestone), and the project.<br>Per MR: pipelines, failed jobs, discussions, approvals, reviewers, last commit, notes. | only from adopt, at most every 5 min (`CACHE = 300`); cleanup and dash only read the cache |
| `cleanup.py` (`Forge.mrs`, versions) | its own `run()` (**untraced**) | per workspace: linked MRs, a `source_branch=` listing, approvals for review MRs, MR versions | every tick |
| `orchestrate.py` | `mr_details`, `gl`; `run_cmd` (**untraced**) | a fresh `mr_details` and milestone per row it acts on; a `source_branch=` check before a goal backfill; runs `next.cmd` (`glab mr update`, `glab mr merge`) | per action (no orchestrate events in this window: ticks ran the Claude engine) |
| `audit.py` gates and `audit.py merge` | its own `_run()` (**untraced**) | `glab mr view`, the target branch, discussions, notes, `mr create`, `mr merge`, `mr diff` | every push / PR / post / merge gate, and every scan with a mergeable head |
| `dash.py page` / `suggest` (`facts` → `mr_facts`) | `mr_details`, `gl_all` | a full `mr_details` plus the head pipeline's jobs (~5 calls), even when the page's cached layout is reused | per puck page the app builds or re-suggests (323 runs in 27 h) |
| `dash.py forge` | `gl` | 3 global queries (review, mine, issues), or 2 per repo | on demand |
| `chat.py` | `mr_details`; the chat agent runs `greenlight.py scan` | `mr_details` per question, to check it isn't stale; a full scan when the agent follows its prompt's "rescans every workspace" line | per chat turn |
| Mac app (`App.swift` `rescan()`) | `ssh … greenlight.py scan` | a full scan | at launch and on ↻ |
| Agents in workspaces | `greenlight.py scan` | **a full scan before every push**, as told by the lifecycles' `mr_open` step, `dash.py` `PUSH_RULE`, SKILL.md's brief rules, `prompts/ci.md`, `prompts/threads.md`, `prompts/conflicts.md`, and the push gate's own "rescan" errors | every agent push, and again after each refused gate |
| `whoami()` | `glab api user`, else `gh api user` | the username, cached in `me.json` (on a glab 429 it falls back to, and caches, the GitHub login) | once |

## 3. Measured call counts

### Per scan

The instrumented scan (86 GitLab rows over 5 projects, 69 open MRs, 4 merged, 14 rows with no MR):

| Endpoint | Calls |
|---|---|
| `merge_requests/:iid/discussions` (paged) | 90 |
| `merge_requests/:iid` | 73 |
| `merge_requests/:iid/approvals` | 69 |
| `merge_requests?…` (open listings, paged; `source_branch=` lookups) | 23 |
| `pipelines/:id/jobs` | 2 |
| `milestones`, `repository/branches/:b`, `glab mr view` | 3 |
| **Total** | **260 in 27.0 s** |

Per-MR endpoints alone are 232 calls, 3.4 per open MR. The open-MR listing already returns every MR, so the per-MR
`GET` repeats it only to read `head_pipeline`.

The trail agrees. Completed `scan` and `report` processes made a median of **242** glab calls over a median of 87
rows.

### Per path, over 26.9 h

Each call is put in exactly one group by its `run` id. A run that includes an automation tick counts as the tick; a
run with a chat turn counts as chat; any other run with a scan counts as a standalone scan.

| Origin | glab calls | Share | Per hour |
|---|---|---|---|
| Automation ticks (54, one every 30 min) | 33,947 | 59.2% | 1,261 |
| Standalone scans (agents' pre-push scans, app ↻, and the `audit.py merge` they start) | 18,574 | 32.4% | 690 |
| Chat turns (and the scans their agent runs) | 2,819 | 4.9% | 105 |
| `dash.py` (puck-page facts, forge) | 1,512 | 2.6% | 56 |
| `ws_state.py` with no run id | 522 | 0.9% | 19 |
| **Total (traced)** | **57,374** | | **2,130** |

Scans:

- 201 started (7.5 an hour), 165 completed (6.1 an hour).
- By subcommand: `scan` 95, `precheck` 53, `report` 42, `--help` 11.

Automation ticks:

- A tick lasts a median of 5 min (longest 12 min).
- A healthy tick makes a median of **~1,050** calls, about 300 a minute at its peak.
- On 33 ticks, `precheck` and `report` each ran a full scan, a median of 106 s apart (~8,500 calls for the second
  scans).
- 11 ticks then ran a third full scan in `ws_state.py observe`.
- 13 ticks were recorded `skipped`. Each made exactly 1 glab call: precheck crashed on its first listing, and
  `auto.py` records any nonzero precheck exit as skipped. No tick in this window had a working precheck that found
  nothing to do.

GitHub, for comparison, costs one GraphQL query per PR (`gh_pr`). There were 2,153 `gh` calls in the same window, and
no GitHub rate-limit errors.

### The rate-limit incident (2026-11-24, 09:11–09:13)

| | |
|---|---|
| Full scans overlapping | 10, each with 8 threads, all standalone (agents' pre-push scans); no tick, `ws_state` or chat calls in the window |
| Peak traced calls | 1,693 in the 60 s from 09:11:27; 651 in the busiest 10 s |
| 429 responses | 475, from 09:12:21 to 09:13:17 |
| Effect | All 10 scans completed, 8 of them with 429 `error` rows. 3 more scans started during the burst (09:12:29, 09:13:00, 09:13:17) crashed on the open-MR listing. |

The push gates of the agents in question ran from 09:10:48 to 09:12:54.

Traced calls reached 1,693 in a rolling minute. GitLab still refused calls, so roughly 300 or more untraced calls
filled the rest of the 2,000: cleanup, audit, orchestrate, agents' own glab, CI. A live check while this report was
written, with none of its own scans running, showed 874 requests counted on the account in 40 s
(`RateLimit-Observed`).

Only 6 calendar minutes in the 27 h had more than 500 traced calls.

### The listing outage (2026-11-23 22:30 to 2026-11-24 08:01)

`scan()` lists each project's open MRs before it looks at any row, and nothing catches a failure there
(`for m in gl_all(f"projects/{pid(proj)}/merge_requests?state=opened")`).

- This crashed 36 scans in the window: 33 with `msg_class: other`, 3 with `rate_limit`.
- 20 automation ticks in a row failed this way, from 22:30 to 08:00, along with six standalone scans.
- The trail doesn't keep the message, only its class, so the cause of the 33 is unknown. They aren't 429s.

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
- **blocking.py** skips an MR whose check failed, and only raises when every check failed.
- **No coordination across processes.** Each scan has its own `ThreadPoolExecutor(8)`, so N concurrent scans make up
  to 8N concurrent calls.

## 5. Redundant, duplicated and N+1 calls

1. **A full scan stands in for a one-workspace refresh.** Seven prompt sites tell agents to run `greenlight.py scan`
   before every push "to refresh this workspace's goal and MR" (section 2). The push gate needs that row in
   `last.json` (`audit.py` `row_for`), and when it refuses a stale row it says "rescan", which costs another full
   scan. Each costs 260 calls; the refresh needs about 4.
2. **No single-flight.** Two scans 10 s apart both run in full. Nothing waits for a scan already running.
3. **A tick scans two or three times.** `precheck` scans with `save=False`. Then `report` scans again about 2
   minutes later, and `ws_state.py observe` often scans a third time 1–5 minutes after that. Observe needs fresh
   goals, which come from the ADE, not GitLab.
4. **Unchanged MRs are fetched in full every scan.** The listing returns `updated_at`, `sha`, `has_conflicts` and
   `detailed_merge_status` for each MR, but `mr_details` refetches the MR, discussions and approvals for all 69.
5. **N+1 per MR.** Three or more REST calls per open MR, where GitLab GraphQL can return a project's open MRs with
   their head pipeline, approvals and discussion state in a few paged queries.
6. **Unknown subcommands scan.** `greenlight.py --help` runs a full scan. The automation's agent does this on some
   ticks (9 times), probably to read the usage, and gets a scan instead.
7. **The same MR fetched by several scripts in one tick.** The precheck scan reads every MR's approvals. Then each of
   these reads approvals again: adopt's `review_done` per candidate, blocking per roster MR, cleanup per review
   workspace, and orchestrate (`mr_details`) per row it acts on. None of them share a cache.
8. **Puck pages refetch what the scan has.** `dash.py page` and `suggest` call `mr_facts` (~5 calls) before checking
   whether the cached layout is still good. `mr_facts` also refetches jobs `mr_details` just read.
9. **Rows with no MR are looked up again every scan.** The 14 rows with no MR each make a `source_branch=` or
   `closed_by` call per scan.

## 6. Ranked fixes

Ranked by calls saved and failures removed per unit of work.

### 1. Refresh one workspace before a push; make full scans single-flight

- **One-workspace refresh, ideally done by the push gate itself.** `gate push` already reads the live head and target
  (`forge_refs`). What it needs from the scan is the goal and the MR link, which is `find_mr` + `evaluate` for one
  row: about 4 calls. Doing it inside `gate push` / `gate pr` removes the scan from all seven prompt sites and from
  the "rescan" loop. A `greenlight.py scan --ws <workspace>` is the other shape, for the prompts that keep it.
- **Writing one row safely.** `last.json` is shared state, and a one-row write has to keep these intact:
  - `keep()` drops any scan whose `scan_started_ns` is older than the saved one. A one-row write must not bump
    `scan_started_ns`, `ts` or `scan_id`, or it will discard a full scan already in flight.
    - `adopt.current_rows()` trusts the whole file while `ts` is fresh.
    - `ws_state.observe` raises "scan superseded" when `scan_id` changes.
    - The report diffs against `prev_ts`.
  - Give each row its own `_scanned_ns`, and have `keep()` merge row by row, newest wins. Otherwise a full scan that
    started before the push overwrites the fresh row with the old `sha`, and the gate refuses again.
  - Write the whole merged row list. `close_stale()` resolves the question of any workspace missing from `rows`, and
    `ws_state.keyed()` reads every row's name to pick card keys.
- **Single-flight for full scans.** App ↻, chat ("rescan every workspace") and automation scans take a new lock held
  for the network part. Today there is only the short write lock in `keep()`, which pings and card writes also need,
  so it must not be held for the whole scan. A second full scan waits, then reuses the result only when that scan
  started after it was asked for. The push path never reuses; it always takes a fresh one-row refresh.
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
  files), so N processes share 8 slots instead of using 8N. At the measured median latency (0.57 s), 8 slots allow
  about 840 calls a minute, under the limit. A shared token bucket is the exact tool if calls get faster.
- **Catch a failed per-project listing.** A failure on one project must not crash the scan or the tick.
- **Keep the last good row.** On a failure that can be retried (429, 5xx, timeout, a failed listing), carry the
  previous row's `mr`, `next` and `life` forward with a `stale` flag and the error class.
  - Don't blank `mr`: that hides the MR from the board, and makes `gate push` / `gate pr` treat the workspace as
    having no MR.
  - Don't change `owner` either. `close_stale()` resolves a workspace's open question as soon as its owner isn't
    `me`, so a 429 would silently close real questions.
- **Classify the outage.** Add the missing patterns to `diag.classify()` so a failure like the 33 `other` crashes
  (auth, DNS, a glab error) gets a class next time.
- **Saves:** few calls, but it turns 429s into short delays, and turns a 9.5-hour silent outage into stale-but-visible
  rows.
- **Risk:** low, once fix 1's single-flight stops waiting scans piling up behind backoff.

### 3. Scan once per tick

- Precheck saves its result to a side file keyed by `GREENLIGHT_RUN`. `report` in the same run reuses it when it's a
  few minutes old, and refreshes only the workspaces `adopt apply` created (with fix 1's one-row path).
- `ws_state.py observe` takes its rows from this run's `last.json` and re-reads only goals from the ADE.
- **Saves:** about 8,500 calls from the second scans on 33 ticks, plus about 2,900 from 11 observe scans: about 20% of
  traced calls.
- **Risk:** low to medium. `report` must still see what `adopt apply` just did.

### 4. Stop unknown subcommands from scanning

- `main()` prints the usage docstring for `--help`, `-h` or an unknown subcommand and exits nonzero. Only `scan`,
  `report`, `precheck` or no argument scan. The named subcommands (`note`, `ping`, `resolve`, `ade`, `signoff`,
  `history`) already return before the fallthrough.
- **Saves:** 11 full scans in 27 h (~2,700 calls). The automation's agent gets the usage it was asking for.
- **Risk:** none.

### 5. Skip unchanged MRs

- Cache each open MR's `mr_details` result keyed by `(project, iid)`, with `updated_at`, `sha`, `has_conflicts`,
  `detailed_merge_status`, and the head pipeline's id and status. When the listing's values match, reuse the cache
  and refetch only the head pipeline.
- **Check first:** new notes and pushes change `updated_at`, but approvals and pipeline status may not. Use a short
  TTL (e.g. 10 min), and always refetch approvals for the rows waiting on them (15 of 69 in the median scan).
- **Saves:** most of the 232 per-MR calls per scan on a quiet board.
- **Risk:** medium. A stale approval or thread count shows the wrong next step until the TTL runs out.

### 6. Batch with GraphQL per project

- One paged `glab api graphql` query per project for `mergeRequests(state: opened)` with `headPipeline`, `approvedBy`,
  `approvalsLeft`, `detailedMergeStatus`, `conflicts` and the discussions' resolvable and resolved flags. This mirrors
  `gh_pr` for GitHub.
- **Saves:** 260 calls per scan becomes roughly 5–15.
- **Risk:** the largest change. GraphQL has complexity limits, and every field `evaluate()` reads has to map from the
  REST shape. Worth doing after fixes 1–5, if traffic is still high.

### 7. Trace everything; share one memo per tick

- Route `cleanup.py`, `audit.py` and `orchestrate.py`'s `run_cmd` glab calls through `greenlight.run()` (or a
  `diag.span("cmd")`). The incident shows untraced traffic matters.
- Adopt, blocking, cleanup and orchestrate read the scan's rows, or a short-lived per-run cache of `gl()` GETs keyed
  by path, instead of refetching approvals and MRs from seconds earlier.
- **Saves:** unknown until the untraced calls are traced, which is why this half comes first.

### 8. Build puck pages from the scanned row

- `dash.py page` checks the layout signature before calling `facts()`, and `mr_facts` takes jobs from `mr_details`
  when it already has them.
- **Saves:** most of `dash.py`'s 1,512 calls.

### 9. Remember "no MR" for a while

- Cache negative `source_branch=` and `closed_by` lookups per row for a few minutes, and clear the entry when the PR
  gate creates one.
- **Saves:** up to ~14 calls per scan here.

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

1. Fix 4, which is trivial. Then fix 2, which removes the board errors and the silent outages.
2. Fix 1 removes the bursts. Fix 3 removes the per-tick double scans.
3. Fix 7's tracing, then re-measure to size fixes 5, 6 and 8 on real numbers.

`diag.py summary` and `errors` show the rate-limit error counts (`top_errors` with `msg_class: rate_limit`), but not
calls per scan or calls per minute. This prints both from the trail:

```bash
python3 - <<'EOF'
import collections, glob, json, os, statistics
ev = [json.loads(l) for f in glob.glob(os.path.expanduser("~/.greenlight/diag/events.jsonl*")) for l in open(f) if l.strip()]
g = sorted((e for e in ev if e.get("kind") == "cmd" and e.get("tool") == "glab"), key=lambda e: e["ts"])
per_pid = collections.Counter(e["pid"] for e in g)
scans = [per_pid[e["pid"]] for e in ev if e.get("kind") == "scan" and e.get("ok") and e.get("op") in ("scan", "report")]
ts, j, peak = [e["ts"] for e in g], 0, 0
for i, t in enumerate(ts):
    while ts[j] < t - 60:
        j += 1
    peak = max(peak, i - j + 1)
print({"glab_calls": len(g), "hours": round((ts[-1] - ts[0]) / 3600, 1), "median_calls_per_scan": statistics.median(scans or [0]),
       "peak_calls_per_60s": peak, "rate_limited": sum(e.get("msg_class") == "rate_limit" for e in g)})
EOF
```

After the fixes, the peak per 60 s should stay well under 2,000, and the median calls per scan should fall with
fixes 5 and 6.
