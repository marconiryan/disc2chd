#!/usr/bin/env bash
# Remove disc2chd for the current user: the command, the PATH block added by
# install.sh, and the logs.
# Usage: ./uninstall.sh
set -euo pipefail

if [ "$#" -ne 0 ]; then
    echo "usage: ./uninstall.sh (takes no arguments)" >&2
    exit 2
fi

bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
dest="$bin_dir/disc2chd"
state="${XDG_STATE_HOME:-$HOME/.local/state}/disc2chd"
fish_config="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"
begin_marker="# >>> disc2chd >>>"
end_marker="# <<< disc2chd <<<"

removed=0
warnings=0

if [ -e "$dest" ] || [ -L "$dest" ]; then
    rm -f "$dest"
    echo "Removed $dest"
    removed=1
fi

for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" "$HOME/.zshrc" "$fish_config"; do
    [ -f "$rc" ] || continue
    state_of_rc="$(awk -v b="$begin_marker" -v e="$end_marker" '
        $0 == b { if (open) bad = 1; open = 1; found = 1; next }
        $0 == e { open = 0 }
        END { if (bad || open) print "broken"; else if (found) print "ok"; else print "none" }
    ' "$rc")"
    case "$state_of_rc" in
        ok)
            tmpfile="$(mktemp)"
            awk -v b="$begin_marker" -v e="$end_marker" \
                '$0 == b {skip = 1; next} $0 == e {skip = 0; next} !skip' "$rc" > "$tmpfile"
            cat "$tmpfile" > "$rc"
            rm -f "$tmpfile"
            echo "Removed the PATH block from $rc"
            removed=1 ;;
        broken)
            echo "Warning: $rc has '$begin_marker' without a matching '$end_marker'." >&2
            echo "         It was left unchanged; remove those lines by hand." >&2
            warnings=1 ;;
    esac
done

if [ -d "$state" ]; then
    rm -rf "$state"
    echo "Removed logs in $state"
    removed=1
fi

if [ "$removed" -eq 0 ] && [ "$warnings" -eq 0 ]; then
    echo "disc2chd is not installed; nothing to remove."
fi
exit "$warnings"
