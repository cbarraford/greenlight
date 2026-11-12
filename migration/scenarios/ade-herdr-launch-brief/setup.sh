mkdir -p "$W/realbin"; ln -s "$(readlink "$W/bin/herdr")" "$W/realbin/herdr"; rm "$W/bin/herdr"
cat > "$W/bin/herdr" <<'SH'
#!/bin/bash
a=(); for x in "$@"; do a+=("${x//$W/\$W}"); done
exec "$W/realbin/herdr" "${a[@]}"
SH
chmod +x "$W/bin/herdr"
