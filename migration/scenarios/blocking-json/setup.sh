# Make the fake CLIs' call log atomic under concurrency (see migration/fake).
mkdir -p "$W/realbin"
for f in "$W"/bin/*; do
  t=$(basename "$f"); [ -L "$f" ] || continue; [ "$t" = python3 ] && continue
  ln -s "$(readlink "$f")" "$W/realbin/$t"; rm "$f"
  printf '#!/bin/bash\nl=$(mktemp); FAKE_LOG=$l "%s/realbin/%s" "$@"; rc=$?; cat "$l" >> "%s/calls"; rm -f "$l"; exit $rc\n' "$W" "$t" "$W" > "$f"
  chmod +x "$f"
done
