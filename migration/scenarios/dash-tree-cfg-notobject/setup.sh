# a real repo + linked worktree, fixed dates so shas are stable
export GIT_AUTHOR_DATE=2026-11-12T11:00:00Z GIT_COMMITTER_DATE=2026-11-12T11:00:00Z
g() { git -c user.name=T -c user.email=t@example.com -c init.defaultBranch=main "$@"; }
g init -q repo && g -C repo commit -q --allow-empty -m init
g -C repo worktree add -q -b feat ../repo-feat && g -C repo-feat commit -q --allow-empty -m wip
echo x > repo-feat/new.txt && echo y > repo-feat/other.txt
printf '{"name": "Widgets", "look": {"strip": "#222"}}' > repo/.greenlight.json && g -C repo add .greenlight.json && g -C repo commit -q -m cfg
rm -f "$W/bin/cmux" "$W/bin/herdr" "$W/bin/t3"  # those ADEs absent: trees()/tree() skip them
# Orca replies name sandbox paths as <W>; the wrapper swaps $W<->"<W>" so fake keys and replies are stable
mkdir -p "$W/fk" && ln -s "$(readlink "$W/bin/orca")" "$W/fk/orca" && rm "$W/bin/orca"
printf '#!/bin/bash\n"$W/fk/orca" "${@//$W/<W>}" | sed "s#<W>#$W#g"; exit ${PIPESTATUS[0]}\n' > "$W/bin/orca" && chmod +x "$W/bin/orca"
for f in $(find "$W/home" -type f); do sed -i "" "s#<W>#$W#g" "$f"; done
