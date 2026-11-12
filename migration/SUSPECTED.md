# Suspected bugs kept as Python (awaiting approve/reject)

The Rust port reproduces each of these on purpose, because the goldens pin Python's behavior.
Mark each line `fix` or `keep`. Every approved fix gets a golden update and a BUGS.md row.
Crash-text cleanups are not listed here; those are already done and recorded in BUGS.md.

## auto
1. Cron parsing has four problems:
   - `*/1` is restricted.
   - `a/n` means just `a`.
   - A 7 anywhere matches Sunday.
   - Ranges aren't validated.

   Fix: proper 5-field parsing with range checks.
2. `add` with no name saves the automation under `"null"`. Fix: require a name.
3. On exec, a precheck timeout or a missing cwd leaves the pid file behind and records no run. Fix: clean up and record an error run.
4. Names aren't validated: `/` crashes and `..` escapes the dir. Fix: reject anything other than `[\w.-]+` and `..`.
5. One bad schedule aborts the whole tick. Fix: skip it and log.
6. The launchd/systemd templates don't escape values: a PATH containing `&`/`<` or spaces breaks them, and the systemd unit has no PATH. Fix: escape the values and add PATH. This gets rewritten at cutover anyway.
7. `alive()`: pid 0, a negative pid or a reused pid reads as alive. Fix: require pid > 0 and check the process.
8. The agent timeout kills only the direct child. Fix: spawn in its own session and killpg.
9. A non-numeric `--timeout` value leaks into the run log. Fix: validate it as an int.

## chat
10. The session-lost retry fires on any failure and repeats the streamed text. Fix: retry only on the session-lost error.
11. The retry leaks `bg.json`. Fix: clean it up.
12. The `("greenlight","greenlight")` duplicate means `"chief"` is no longer exempt (pinned by chat-send-name-chief). Fix: restore `"chief"` in the pair.
13. `compact --model codex` checks only the claude session. Fix: check the session of the engine in use.
14. `stop` doesn't verify that it owns the pid. Fix: compare the process start time or the command.
15. Stop race: the pid is written after spawn. Fix: write it before the turn streams.
16. A malformed stream line crashes the turn and leaves the child running. Fix: skip the line, and kill the child on error.

## ade backends
17. `project_id` keeps userinfo and port (`git:git@gitlab.com/22/...`). Fix: strip both.
18. `flags()` can't take a value that starts with `--`.
19. t3 `read --limit 0` returns every line.
20. `--workspace-status` is silently ignored.
21. herdr swallows `agent_not_ready`.
22. t3 `ms()` treats a naive ISO time as local time.
23. cmux: a missing `updatedAt` raises KeyError.

## greenlight core
24. Any unknown subcommand (`scna`, or `--json` given first) runs a full scan that saves state. Fix: reject unknown commands.
25. A corrupt last.json stops the scan. Fix: treat it as `{}` and warn.
26. The state dir is created without its parents, so a missing parent fails with `[Errno 2]` (gl-home-deep). Fix: `create_dir_all`.
27. `said()` strips the MR title anywhere in the goal, which can delete instructions. Fix: strip only a leading `Merge !N: <title>`.
28. `HOLD_RE` misses "dont merge". Fix: `do ?n[o'’]?t merge`.
29. A closed_by reply that isn't JSON becomes a row error. Fix: catch it and fall back to the branch lookup.

## blocking
30. A non-success pipeline with no failed jobs skips the thread and approval checks.
31. The milestone prefix match lets v1.2 match v1.20. Fix: compare whole versions.

## cleanup
32. Remote parsing is weak: `ssh://host:port` and a trailing slash both break it.
33. Lookups read only one page: GitLab `per_page`, and gh's default of 30. Fix: paginate.
34. `orca terminal list` has no `--limit`, so busy terminals can be missed and their workspace deleted. **Data-loss risk.**
35. `gh pr view` has no 404 tolerance.
36. On GitHub, "approved by me" has no source-branch guard.
37. Scan mutates repos (`fetch --prune`, `remote set-head --auto`). Making it read-only is a behavior change.

## adopt
38. `parse_remote` reads an all-digit first namespace as a port. Fix: allow a port only after `scheme://`.
39. If `create()` partly fails, it leaves an orphan workspace, and the next run hits "branch busy". Fix: record `created[k]` as soon as the worktree exists.
40. `git merge-base --is-ancestor` inherits stdio and has no timeout, which can pollute `--json` stdout. Fix: capture its output and add a timeout.
41. `repo_map` is keyed only by the owner/repo path, so the same path on gitlab and github collides. Fix: key by `(forge, path)`.
42. The page compares the role to `"reviewer"`, but candidates carry `"review"`, so that test never matches. Fix: compare to `"review"`.
