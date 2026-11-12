# claude's --append-system-prompt names $W/repo/SKILL.md: rewrite $W to <W> so the fake's key is stable
mkdir -p "$W/fk" && ln -s "$(readlink "$W/bin/claude")" "$W/fk/claude" && rm "$W/bin/claude"
printf '#!/bin/bash\nexec "$W/fk/claude" "${@//$W/<W>}"\n' > "$W/bin/claude" && chmod +x "$W/bin/claude"
