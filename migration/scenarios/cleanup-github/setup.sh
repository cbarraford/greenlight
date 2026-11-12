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
repo gadgets acme/gadgets main github.com
wt merged fix-1;    commit merged m1; push merged
wt approved review-6
wt open fix-7;      commit open o1;   push open
wt closed fix-8;    commit closed c1; push closed; commit closed c2
wt commented review-9
wt linked fix-10;   commit linked l1; push linked
finish
