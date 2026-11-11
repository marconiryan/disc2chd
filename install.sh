#!/usr/bin/env bash
# Install disc2chd for the current user (no root needed).
#   ./install.sh                 install or upgrade ~/.local/bin/disc2chd
#   ./install.sh --add-to-path   also add the install folder to PATH in shell rc files
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/src/disc2chd.py"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
dest="$bin_dir/disc2chd"
begin_marker="# >>> disc2chd >>>"
end_marker="# <<< disc2chd <<<"

add_to_path=0
for arg in "$@"; do
    case "$arg" in
        --add-to-path) add_to_path=1 ;;
        -h|--help)
            sed -n '2,4s/^# \{0,1\}//p' "$0"
            exit 0 ;;
        *)
            echo "install.sh: unknown option: $arg" >&2
            exit 2 ;;
    esac
done

if [ ! -f "$src" ]; then
    echo "install.sh: cannot find $src" >&2
    exit 1
fi

# Python 3.10 or newer is required.
if ! command -v python3 >/dev/null 2>&1; then
    echo "install.sh: python3 was not found. Install Python 3.10 or newer first." >&2
    exit 3
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "install.sh: Python 3.10 or newer is required (found $(python3 -V 2>&1))." >&2
    exit 3
fi

mkdir -p "$bin_dir"
old_version=""
if [ -f "$dest" ]; then
    old_version="$("$dest" --version 2>/dev/null || echo "unknown version")"
fi
tmp="$dest.tmp.$$"
cp "$src" "$tmp"
chmod +x "$tmp"
mv -f "$tmp" "$dest"
new_version="$("$dest" --version)"
if [ -n "$old_version" ]; then
    echo "Upgraded $dest ($old_version -> $new_version)"
else
    echo "Installed $new_version to $dest"
fi

echo
echo "Checking dependencies:"
if ! "$dest" --check; then
    echo
    echo "disc2chd is installed, but a required dependency is missing (see above)."
fi

path_line="export PATH=\"$bin_dir:\$PATH\""
fish_line="set -gx PATH \"$bin_dir\" \$PATH"

add_block() {
    # add_block FILE LINE: (re)write the marked block at the end of FILE.
    local file="$1" line="$2" tmpfile
    mkdir -p "$(dirname "$file")"
    touch "$file"
    if grep -qxF "$begin_marker" "$file"; then
        tmpfile="$(mktemp)"
        awk -v b="$begin_marker" -v e="$end_marker" \
            '$0 == b {skip = 1; next} $0 == e {skip = 0; next} !skip' "$file" > "$tmpfile"
        cat "$tmpfile" > "$file"
        rm -f "$tmpfile"
    fi
    printf '%s\n%s\n%s\n' "$begin_marker" "$line" "$end_marker" >> "$file"
    echo "Added PATH block to $file"
}

echo
case ":$PATH:" in
    *":$bin_dir:"*)
        echo "$bin_dir is in PATH. Run: disc2chd --help" ;;
    *)
        if [ "$add_to_path" -eq 1 ]; then
            add_block "$HOME/.bashrc" "$path_line"
            if [ -f "$HOME/.zshrc" ] || [ "${SHELL##*/}" = "zsh" ]; then
                add_block "$HOME/.zshrc" "$path_line"
            fi
            fish_config="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"
            if [ -f "$fish_config" ] || [ "${SHELL##*/}" = "fish" ]; then
                add_block "$fish_config" "$fish_line"
            fi
            echo "Open a new terminal, then run: disc2chd --help"
        else
            echo "$bin_dir is not in your PATH. Add this line to your shell's startup file:"
            echo "  bash (~/.bashrc):                  $path_line"
            echo "  zsh  (~/.zshrc):                   $path_line"
            echo "  fish (~/.config/fish/config.fish): $fish_line"
            echo "Or run: ./install.sh --add-to-path"
        fi ;;
esac
