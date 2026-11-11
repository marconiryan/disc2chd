#!/usr/bin/env bash
# Remove disc2chd for the current user.
#   ./uninstall.sh           remove the executable and the PATH block; keep logs
#   ./uninstall.sh --purge   also remove the logs
set -euo pipefail

bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
dest="$bin_dir/disc2chd"
state="${XDG_STATE_HOME:-$HOME/.local/state}/disc2chd"
begin_marker="# >>> disc2chd >>>"
end_marker="# <<< disc2chd <<<"

purge=0
for arg in "$@"; do
    case "$arg" in
        --purge) purge=1 ;;
        -h|--help)
            sed -n '2,4s/^# \{0,1\}//p' "$0"
            exit 0 ;;
        *)
            echo "uninstall.sh: unknown option: $arg" >&2
            exit 2 ;;
    esac
done

removed=0
if [ -e "$dest" ] || [ -L "$dest" ]; then
    rm -f "$dest"
    echo "Removed $dest"
    removed=1
fi

fish_config="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"
for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$fish_config"; do
    if [ -f "$rc" ] && grep -qxF "$begin_marker" "$rc"; then
        tmpfile="$(mktemp)"
        awk -v b="$begin_marker" -v e="$end_marker" \
            '$0 == b {skip = 1; next} $0 == e {skip = 0; next} !skip' "$rc" > "$tmpfile"
        cat "$tmpfile" > "$rc"
        rm -f "$tmpfile"
        echo "Removed PATH block from $rc"
        removed=1
    fi
done

if [ -d "$state" ]; then
    if [ "$purge" -eq 1 ]; then
        rm -rf "$state"
        echo "Removed logs in $state"
        removed=1
    else
        echo "Kept logs in $state (use --purge to remove them)"
    fi
fi

if [ "$removed" -eq 0 ]; then
    echo "Nothing to remove."
fi
