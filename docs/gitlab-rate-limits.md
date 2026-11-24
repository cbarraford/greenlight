# GitLab API rate limits: where greenlight thrashes the API

Investigation for issue #150. No code changes; this is the measured picture and a ranked list of fixes.

## TL;DR

- GitLab.com allows **2,000 authenticated API requests per user per minute** (`RateLimit-Name: throttle_authenticated_api`,
  read from the response headers). Every glab call greenlight makes, and every glab call an agent makes, shares that budget.
- One `greenlight.py scan` costs **260 glab calls in 27 s** (measured, 86 GitLab rows, 69 open MRs): about 3.6 calls per
  open MR, 8 at a time, peaking at 15 calls a second.
- Greenlight runs **7.7 full scans an hour** on average (201 in 27 h), plus adopt, blocking, dash and chat traffic:
  **~2,200 traced glab calls an hour** (57,374 in 27 h).
- The rate-limit errors come from **bursts of overlapping scans, not the average**. Between 09:11 and 09:12 on
  2026-11-24, ten full scans overlapped (each with 8 threads) and made 1,218 traced calls in one minute. GitLab answered
  **475 calls with 429** in the next 56 s. Those 429s are not retried, so they became `error` rows on the board
  ("greenlight couldn't evaluate: glab api …/merge_requests/N: glab: …"), and three more scans crashed outright.
- Most of the overlap comes from the **pre-push `scan` the lifecycle asks of every agent**. Each agent scans every
  workspace (260 calls) just to refresh its own one (about 4 calls' worth). Chat turns and the menubar ↻ do the same.
- The fix that matters most is to **scope those scans to one workspace and make full scans single-flight**. Second is to
  **treat 429 as retryable with backoff** and cap concurrency across processes. Section 6 ranks the full list.

## 1. How this was measured

- **The diagnostic trail** (`~/.greenlight/diag/events.jsonl` and `.1`–`.3`): 99,718 events from 2026-11-23 06:33 to
  2026-11-24 09:28 (27.0 h), on the host's skill checkout (`861982f`). Every call made through `greenlight.run()` logs
  a `cmd` span with `tool`, `sub`, `ok`, `msg_class`, `pid` and `run`. No glab call path changed between `861982f` and
  this branch's base, so the numbers apply to current `main`.
- **One instrumented scan**: `greenlight.py scan` with a logging `glab` shim on `PATH` that records each call's endpoint,
  with project paths and numbers stripped.
- **Live response headers** from `glab api -i`: `RateLimit-Limit`, `RateLimit-Observed`, `RateLimit-Reset`, `ETag`, and
  a conditional (`If-None-Match`) request.

What the trail can't see (so the real load is higher than every number below):

- `cleanup.py` and `audit.py` call glab through their own `run()` / `_run()` helpers, not `greenlight.run()`, so their
  calls aren't in the trail.
- Agents' own glab calls (`glab mr view`, `glab api`, …) in their terminals, and CI jobs on the same token.
- The Mac app's own trail (`app.jsonl`): the app calls GitLab only through `ssh` into scripts on this host, so its
  traffic shows up here.

## 2. Inventory: every path that calls GitLab

| Path | Calls GitLab via | What it fetches | How often |
|---|---|---|---|
| `greenlight.py scan` (`scan()`) | `gl`, `gl_all`, `mr_details` | per project: the open-MR listing (paged) and, when the lifecycle has `milestone`, active milestones; per row with no open MR: `closed_by` or a `source_branch=` lookup; per open MR: the MR, its discussions (paged), its approvals; on a failed pipeline, its failed jobs; on `check=` stages, all jobs; when blocked, `/blocks` | every full scan (below) |
| `greenlight.py precheck` | `scan(save=False)` then `adopt.plan()` | a full scan, then adopt's candidate queries and the blocking roster scan | every automation tick |
| `greenlight.py report` | `scan()` | a full scan | every tick that has work to do |
| `greenlight.py <anything else>` (e.g. `--help`) | `scan()` | **a full scan**: `main()` treats any unknown subcommand as `scan` | 11 times in 27 h |
| `ws_state.py` | `greenlight.scan` | a full scan | 14 runs in 27 h |
| `adopt.py` (`candidates`, `review_done`, `rerequested`) | `gl_all`, `gl` | 3 global `merge_requests?scope=all` queries (author, reviewer, assignee); per review candidate: `/approvals`, sometimes `/reviewers`; per blocked MR with no candidate: the MR | every tick (precheck plan, then apply) |
| `blocking.py` (`scan`, `check`) | `gl`, `gl_all` | per roster repo: open MRs (or per active milestone), the project; per MR: pipelines, failed jobs, discussions, approvals, reviewers, last commit, notes | at most every 5 min (`CACHE = 300`), from adopt and cleanup |
| `cleanup.py` (`Forge.mrs`, versions) | its own `run()` (**untraced**) | per workspace: the linked MRs, a `source_branch=` listing, approvals for review MRs, MR versions | every tick |
| `orchestrate.py` | `mr_details`, `gl` | per acted-on row: a fresh `mr_details` (3+ calls), the milestone; a `source_branch=` check before a goal backfill | per action in a tick |
| `audit.py` gates | its own `_run()` (**untraced**) | `glab mr view`, the target branch, discussions, notes, `mr create`, `mr merge`, `mr diff` | every push / PR / post / merge gate |
| `dash.py facts` / `mr_facts` | `mr_details`, `gl_all` | a full `mr_details` plus the head pipeline's jobs (~5 calls) | each background puck-page build the app queues (323 runs in 27 h) |
| `dash.py forge` | `gl` | 3 global queries (review, mine, issues), or 2 per repo | on demand |
| `chat.py` | `mr_details`; and the chat agent runs `greenlight.py scan` | `mr_details` per question to check it isn't stale; a full scan when the agent follows its prompt's "rescan every workspace" line | per chat turn |
| Mac app (`App.swift` `rescan()`) | `ssh … greenlight.py scan` | a full scan | at launch and on ↻ |
| Agents in workspaces | `greenlight.py scan` (lifecycle `mr_open` step, `dash.py` `PUSH_RULE`, SKILL.md) | **a full scan before every push** | every agent push |
| `whoami()` | `glab api user` | the username, cached in `me.json` | once |

## 3. Measured call counts

### Per scan

The instrumented scan (86 GitLab rows over 5 projects, 69 open MRs, 4 merged, 14 rows with no MR):

| Endpoint | Calls |
|---|---|
| `merge_requests/:iid/discussions` (paged) | 90 |
| `merge_requests/:iid` | 73 |
| `merge_requests/:iid/approvals` | 69 |
| `merge_requests?…` (open listing, paged; `source_branch=` lookups) | 23 |
| `pipelines/:id/jobs` | 2 |
| `milestones`, `repository/branches/:b`, `glab mr view` (automerge audit) | 3 |
| **Total** | **260 in 27.0 s** |

That is three calls for every open MR on every scan whether or not the MR changed, plus extra discussion pages on long
threads. The open-MR listing already returns each MR, so the per-MR `GET` repeats it only to read `head_pipeline`.

The trail agrees: the 201 scan processes made a median of **242** glab calls (p90 546, max 598), over a median of 87 rows.
`precheck` processes made a median of **541**: a full scan, then adopt's plan (candidates plus the blocking scan when its
5-minute cache is cold). That split is inferred from the code, not measured endpoint by endpoint.

### Per path, over 27 h

Calls grouped by the `run` correlation id they carried:

| Origin | glab calls | Per hour | Share |
|---|---|---|---|
| Automation ticks (54 ticks, one every 30 min) | 33,947 | 1,288 | 59% |
| Standalone `greenlight.py` scans (agents' pre-push scans, app ↻, `--help`) | 16,685 | 601 | 29% |
| Runs whose id an `audit.py` gate started (scans run by its child processes) | 3,347 | 124 | 6% |
| Runs started by a chat turn | 3,078 | 114 | 5% |
| `dash.py` (puck-page facts, forge) | 1,520 | 56 | 3% |
| `ws_state.py`, `adopt.py` run standalone | 576 | 21 | 1% |
| **Total (traced)** | **57,374** | **2,202** | |

An automation tick costs a median of **846** calls (max 1,452: precheck, report, a `ws_state` scan and a `--help` scan).
13 of the 54 ticks stopped after precheck, and even those spent about 540 calls to find nothing to do.

Scans by subcommand: `scan` 3.7/h, `precheck` 2.0/h, `report` 1.6/h, `--help` 0.4/h.

GitHub, for comparison, costs one GraphQL query per PR (`gh_pr`): 2,238 `gh` calls in the same 27 h, and the board
shows no GitHub rate-limit errors.

### The rate-limit incident

| | |
|---|---|
| Window | 2026-11-24 09:11–09:13 |
| Full scans overlapping | 10 (each with 8 threads), plus a `ws_state` scan and dash |
| Glab-calling processes in one 10 s window | 10 (the most in the 27 h) |
| Traced calls in the peak minute | 1,218 (568 in the peak 10 s) |
| 429 responses | 475, from 09:12:21 to 09:13:17 |
| Effect | 8 of the 10 finished "ok" with 429 `error` rows on the board; 3 scans started during the burst crashed on the open-MR listing (`uncaught`, `rate_limit`) |

The scans were started by agents pushing (`audit gate push` events from 09:10:48 to 09:12:54) and by chat turns. The
automation tick that was running at the time adds its own scans.

Traced calls only reach 1,218 a minute. The 429s still came, so the untraced traffic (cleanup, audit, agents' own glab,
CI) filled the rest of the 2,000. A live check while writing this, with no scan of this investigation running,
showed **874 requests observed in 40 s** on the account (`RateLimit-Observed`), from other workspaces' scans and chat.

Minutes with more than 500 traced calls: 8 in 27 h. The median minute has none, so the API is idle most of the time and
the bursts hit the limit.

## 4. How rate-limit responses are handled today

- `greenlight.run(cmd, tries=3)` retries only when stderr matches `TRANSIENT_RE` (5xx, gateway, timeouts, connection
  resets). **429 isn't in it**, so a rate-limited call fails on its first try. Every glab call in the trail shows
  `tries: 1`.
- When a retry does happen, it waits `2 * 2**i` seconds (2 s, then 4 s). It never reads `Retry-After` or
  `RateLimit-Reset`. `glab api` without `-i` doesn't show headers at all.
- `diag.classify()` does tag the failure `msg_class: rate_limit`, so the trail shows the cause.
- In `scan()`, a failure inside a row's `find_mr` becomes `next = {step: "error", owner: "me"}` with the raw message,
  which is the error the board shows. A failure in the up-front per-project listing (`gl_all(…merge_requests?state=opened)`)
  isn't caught, so the whole scan crashes.
- `blocking.scan()` skips an MR whose check failed, and only raises when every check failed.
- Nothing coordinates across processes. Each scan has its own `ThreadPoolExecutor(8)`, so N concurrent scans make up
  to 8N concurrent calls.

## 5. Redundant, duplicated and N+1 calls

1. **A full scan stands in for a one-workspace refresh.** The lifecycle's `mr_open` step, `dash.py`'s `PUSH_RULE` and
   SKILL.md's brief rules tell every agent to run `greenlight.py scan` before every push "to refresh this workspace's
   goal and MR". Chat's prompt tells its agent to run one to "rescan every workspace". Each costs 260 calls; the
   refresh needs about 4.
2. **No single-flight.** Two scans 10 s apart both run in full. Nothing reuses a `last.json` written seconds ago, and
   nothing waits on a scan that is already running.
3. **Unchanged MRs are fetched in full on every scan.** The open-MR listing returns `updated_at` (and `sha`) for each
   MR, but `mr_details` fetches the MR, discussions and approvals for all 69 of them every time.
4. **Per-MR N+1.** Three or more REST calls per open MR, where GitLab GraphQL can return the open MRs of a project with
   their head pipeline, approvals and discussions in a few paged queries.
5. **Unknown subcommands scan.** `greenlight.py --help` (or a typo) runs a full scan: 11 of them in 27 h.
6. **Precheck pays full price to find no work.** 13 of 54 ticks spent ~540 calls to learn nothing needed doing.
7. **The same MR fetched by several scripts in one tick.** Precheck's scan reads every MR's approvals. Then adopt reads
   approvals again for review candidates (`review_done`), blocking reads them again per roster MR, cleanup reads them
   again per review workspace, and orchestrate runs `mr_details` again per row it acts on. None of these share a cache.
8. **Rows with no MR are looked up again every scan.** The 14 rows with no MR each make a `source_branch=` or
   `closed_by` call per scan, with nothing to show for it.
9. **Puck-page builds** call `mr_details` plus a jobs listing (~5 calls) per build, instead of the row the scan already has.

## 6. Ranked fixes

Ranked by calls saved and 429 risk removed per unit of work.

### 1. Scope agent, chat and app refreshes to one workspace; make full scans single-flight

- Add `greenlight.py scan --ws <workspace>` (also usable from cwd) that evaluates one row, merges it into `last.json`
  under the scan lock, and costs about 4 calls. Point the lifecycle's `mr_open` step, `PUSH_RULE`, SKILL.md and chat's
  prompt at it. The push gate reads the workspace's row from `last.json` (`audit.py` `row_for`), which is why agents
  scan before pushing, so the one-row scan must keep writing that row.
- A full scan takes a lock, and a second one either waits for the running one and reuses its result, or returns
  `last.json` when it's younger than about 60 s.
- **Saves:** most of the 29% "standalone" share plus the audit and chat shares, about 40% of traced calls. It also
  removes the overlap that caused the incident: ten agent pushes at once cost ~40 calls instead of ~2,600.
- **Risk:** low. The full scan is unchanged. The one-row path has to reproduce `find_mr` and `evaluate` for one row
  (stacked base MRs read `open_by_branch`, so that needs care).

### 2. Retry 429 with backoff, and cap concurrency across processes

- Add `429` and `Too Many Requests` to the retryable set in `_run`, wait until `RateLimit-Reset` or `Retry-After` (or a
  jittered 5–30 s when the header can't be read), and give glab calls more tries on a 429.
- Cap concurrent glab calls per host with a small file-based semaphore under `~/.greenlight` (e.g. 8 slots via `flock`
  on slot files), so N processes share 8 slots instead of using 8N.
- Catch a failed per-project listing in `scan()` and mark that project's rows as errors rather than crashing.
- Show a rate-limited row as "GitLab rate limit, retrying next scan" with `owner` not `me`, so a 429 doesn't put a fake
  question on the board.
- **Saves:** no calls, but turns 429s from board errors and crashed scans into short delays.
- **Risk:** low. Backoff makes a slow scan slower, so the single-flight lock in fix 1 matters, or waiting scans pile up.

### 3. Skip unchanged MRs (per-MR cache keyed on `updated_at` and head)

- Cache each open MR's `mr_details` result in `~/.greenlight` keyed by `(project, iid)`, holding `updated_at`, `sha` and
  the head pipeline's id and status. When the listing shows the same `updated_at` and `sha`, reuse it, and refetch only
  the head pipeline.
- **Check first:** new notes and pushes change an MR's `updated_at`. Approvals and pipeline status changes may not. Use a
  short TTL (e.g. 10 min), and refetch on every row whose next step depends on approvals or CI.
- **Saves:** for the measured board most MRs are idle between scans, so probably two-thirds or more of the 232 per-MR
  calls per scan.
- **Risk:** medium. A stale approval or thread count shows the wrong next step until the TTL runs out.

### 4. Batch with GraphQL per project

- One `glab api graphql` query per project (paged by 20–50 MRs) for `mergeRequests(state: opened)` with `headPipeline`,
  `approvedBy`, `approvalsLeft`, `detailedMergeStatus`, `conflicts`, and the discussions' resolvable and resolved flags.
  This mirrors what `gh_pr` already does for GitHub.
- **Saves:** 260 calls per scan becomes roughly 5–15.
- **Risk:** the largest change. GraphQL has query complexity limits, and every field the board reads has to map from
  the REST shape `evaluate()` uses. Worth doing after fixes 1–3, if the traffic is still high.

### 5. Stop unknown subcommands from scanning

- `main()` should print the usage docstring for `--help`, `-h` or any unknown command and exit nonzero, and scan only on
  `scan`, `report`, `precheck` or no argument.
- **Saves:** 11 full scans in 27 h (~2,700 calls) for a three-line change.
- **Risk:** none, as long as no caller relies on `greenlight.py <other>` scanning. Callers only use `scan`, `report` and
  `precheck`.

### 6. Share one per-tick memo across scripts

- Adopt, blocking, cleanup and orchestrate should read the scan's rows (or a short-lived per-run cache of `gl()` GET
  responses keyed by path) instead of refetching approvals and MRs fetched seconds earlier in the same tick.
- Route `cleanup.py` and `audit.py` glab calls through `greenlight.run()` (or a `diag.span("cmd")`) so they show up in
  the trail. They are invisible today, and the incident shows invisible traffic matters.
- **Saves:** a share of the ~850 calls per tick; exact numbers need the cleanup and audit traffic traced first.

### 7. Cheaper prechecks

- Precheck could reuse a `last.json` from the previous tick plus one listing call per project to see which MRs changed,
  and run the full scan only when something did.
- **Saves:** up to ~540 calls on each tick that has nothing to do (13 of 54 observed).

### 8. Remember "no MR" for a while

- Cache negative `source_branch=` and `closed_by` lookups per row for a few minutes. A workspace whose MR doesn't exist
  yet doesn't need a lookup on every scan. The push gate's PR creation can clear the entry.
- **Saves:** up to ~14 calls per scan here.

### 9. ETag / If-None-Match: not a rate-limit fix

- GitLab does send `ETag` on these endpoints (`merge_requests/:iid`, `/discussions`, the listings) and answers a
  matching `If-None-Match` with `304 Not Modified`.
- **But a 304 still counts against the limit.** Three conditional requests in a row raised `RateLimit-Observed` just as
  full ones did. On top of that, `glab api` exits 1 on a 304 (`glab: HTTP 304`).
- So ETags save bandwidth and JSON parsing, not quota. They are worth adding only as part of fix 3's cache, never on
  their own.

### Not recommended: scanning less often

The 30-minute automation tick is 59% of traced calls, but it's steady (~850 calls spread over a tick of about 12 minutes) and
didn't cause the incident. Fixes 3 and 7 cut its cost without making the board staler.

## 7. Suggested order

1. Fix 5 (trivial) and fix 2 (stops the errors on the board).
2. Fix 1 (removes the bursts and ~40% of calls).
3. Fix 6's tracing half, then re-measure with `diag.py` to size fixes 3, 4 and 7 on real numbers.
4. Fix 3, then fix 4 if still needed.

To re-measure after each fix:

```bash
python3 scripts/diag.py summary          # top_errors: cmd / rate_limit count
python3 scripts/diag.py errors --since 24h
```

These give the per-scan call count (median `cmd` spans with `tool: glab` per `scan` pid) and the peak calls per minute,
which should stay well under 2,000.
