# Workaround until migration/fake writes its log line in one write(): the script runs fake calls from
# a thread pool, and concurrent appends to the call log interleave. Serialize every faked CLI.
for t in $(ls "$W/bin"); do
  case $(readlink "$W/bin/$t") in */fake) ;; *) continue ;; esac
  mkdir -p "$W/fakebin"; ln -sf "$(readlink "$W/bin/$t")" "$W/fakebin/$t"; rm "$W/bin/$t"
  printf '#!/bin/bash\nuntil mkdir "$W/.fakelock" 2>/dev/null; do sleep 0.01; done\n"$W/fakebin/%s" "$@"; rc=$?\nrmdir "$W/.fakelock"; exit $rc\n' "$t" > "$W/bin/$t"
  chmod +x "$W/bin/$t"
done
mkdir -p plain
