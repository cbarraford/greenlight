rm -f "$W/bin/cmux" "$W/bin/herdr" "$W/bin/t3"  # those ADEs absent: trees()/tree() skip them
# Orca replies name sandbox paths as <W>; the wrapper swaps $W<->"<W>" so fake keys and replies are stable
mkdir -p "$W/fk" && ln -s "$(readlink "$W/bin/orca")" "$W/fk/orca" && rm "$W/bin/orca"
printf '#!/bin/bash\n"$W/fk/orca" "${@//$W/<W>}" | sed "s#<W>#$W#g"; exit ${PIPESTATUS[0]}\n' > "$W/bin/orca" && chmod +x "$W/bin/orca"
for f in $(find "$W/home" -type f); do sed -i "" "s#<W>#$W#g" "$f"; done
