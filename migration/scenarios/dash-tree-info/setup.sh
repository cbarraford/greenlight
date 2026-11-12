# a real repo + linked worktree, fixed dates so shas are stable
export GIT_AUTHOR_DATE=2026-11-12T11:00:00Z GIT_COMMITTER_DATE=2026-11-12T11:00:00Z
g() { git -c user.name=T -c user.email=t@example.com -c init.defaultBranch=main "$@"; }
g init -q repo && g -C repo commit -q --allow-empty -m init
g -C repo worktree add -q -b feat ../repo-feat && g -C repo-feat commit -q --allow-empty -m wip
echo x > repo-feat/new.txt && echo y > repo-feat/other.txt
printf '{"name": "Widgets", "look": {"strip": "#222"}}' > repo/.greenlight.json && g -C repo add .greenlight.json && g -C repo commit -q -m cfg
