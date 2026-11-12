# Workaround until migration/fake writes its log line in one write(): the script runs fake calls from
# a thread pool, and concurrent appends to the call log interleave. Serialize every faked CLI.
for t in $(ls "$W/bin"); do
  case $(readlink "$W/bin/$t") in */fake) ;; *) continue ;; esac
  mkdir -p "$W/fakebin"; ln -sf "$(readlink "$W/bin/$t")" "$W/fakebin/$t"; rm "$W/bin/$t"
  printf '#!/bin/bash\nuntil mkdir "$W/.fakelock" 2>/dev/null; do sleep 0.01; done\n"$W/fakebin/%s" "$@"; rc=$?\nrmdir "$W/.fakelock"; exit $rc\n' "$t" > "$W/bin/$t"
  chmod +x "$W/bin/$t"
done
# Real git repos with forge-looking remotes: $W/cwd/gitssh maps git@<host>:<path> to $W/origins/<path>.
set -e
export GIT_AUTHOR_NAME=Dev GIT_AUTHOR_EMAIL=dev@example.com GIT_COMMITTER_NAME=Dev GIT_COMMITTER_EMAIL=dev@example.com
export GIT_AUTHOR_DATE=2026-11-01T00:00:00Z GIT_COMMITTER_DATE=2026-11-01T00:00:00Z
cat > gitssh <<'S'
#!/bin/sh
for a; do last=$a; done
p=$(printf '%s' "$last" | sed "s/^git-[a-z-]* '\(.*\)'$/\1/"); p=${p#/}
cmd=$(printf '%s' "$last" | sed "s/^git-\([a-z-]*\) .*/\1/")
exec git "$cmd" "$W/origins/$p"
S
chmod +x gitssh
repo() {  # repo <dir> <owner/name> <default branch> <forge host>
  git init -q --bare -b "$3" "$W/origins/$2.git"
  git init -q -b "$3" "$1"; git -C "$1" remote add origin "$W/origins/$2.git"
  echo base > "$1/README"; git -C "$1" add README; git -C "$1" commit -qm init; git -C "$1" push -q -u origin "$3"
  R=$1 HOST=$4 PROJ=$2
}
wt() { git -C "$R" worktree add -q -b "$2" "../wt/$1" "${3:-HEAD}"; }        # wt <dir> <branch> [base]
detached() { git -C "$R" worktree add -q --detach "../wt/$1"; }
commit() { echo "$2" > "wt/$1/$2"; git -C "wt/$1" add "$2"; git -C "wt/$1" commit -qm "$2"; }
push() { git -C "wt/$1" push -q -u origin "${2:-HEAD}"; }
finish() { git -C "$R" remote set-url origin "git@$HOST:$PROJ.git"; }
repo widgets acme/widgets develop gitlab.com
wt merged feat-merged;   commit merged m1;  push merged
wt open feat-open;       commit open o1;    push open
wt closed feat-closed;   commit closed c1;  push closed; echo junk > wt/closed/untracked.txt
wt unpushed feat-unpushed; commit unpushed u1; push unpushed; commit unpushed u2
wt rebased feat-rebased; commit rebased r1; push rebased; commit rebased r2
wt mrhead feat-mrhead;   commit mrhead h1
wt busy feat-busy;       commit busy b1;    push busy
wt fresh feat-fresh
wt idle feat-idle
wt noact feat-noact
wt wip feat-wip;         commit wip w1
wt twice feat-twice;     commit twice t1;   push twice
wt reopen feat-reopen;   commit reopen ro1; push reopen
wt review-approved mr-22
wt review-cant mr-23
wt review-own bob-feature; git -C wt/review-own config branch.bob-feature.merge refs/heads/bob-feature
wt review-pending mr-25
wt review-approved-own bob-other
detached detached-review
detached detached-missing
detached detached-linked
wt linked feat-linked;   commit linked l1;  push linked
wt lookup-err feat-err;  commit lookup-err e1; push lookup-err
wt renamed local-name;   commit renamed n1; push renamed HEAD:remote-name
git -C wt/renamed config branch.local-name.remote origin; git -C wt/renamed config branch.local-name.merge refs/heads/remote-name
wt weird feat-weird;     commit weird x1;   push weird
wt twin feat-twin;       commit twin tw1;   push twin
mkdir -p wt/default-dir wt/pinned
wt landed feat-landed;   commit landed la1; push landed
git -C widgets merge -q --ff-only feat-landed; git -C widgets push -q origin develop
finish
