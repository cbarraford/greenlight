# pi's --append-system-prompt names $W/repo/SKILL.md: rewrite $W to <W> so the fake's key is stable
mkdir -p "$W/fk" && ln -s "$(readlink "$W/bin/pi")" "$W/fk/pi" && rm "$W/bin/pi"
printf '#!/bin/bash\nexec "$W/fk/pi" "${@//$W/<W>}"\n' > "$W/bin/pi" && chmod +x "$W/bin/pi"
