#!/usr/bin/env bash
# Install or upgrade disc2chd for the current user (no root needed).
# Usage: ./install.sh
set -euo pipefail

if [ "$#" -ne 0 ]; then
    echo "usage: ./install.sh (takes no arguments)" >&2
    exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/src/disc2chd.py"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
dest="$bin_dir/disc2chd"
fish_config="${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish"
begin_marker="# >>> disc2chd >>>"
end_marker="# <<< disc2chd <<<"
path_line="export PATH=\"$bin_dir:\$PATH\""
fish_line="set -gx PATH \"$bin_dir\" \$PATH"
user_shell="${SHELL:-}"
user_shell_name="${user_shell##*/}"

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

# Atomic install or upgrade.
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

# Prints "none", "ok" or "broken" (a begin marker without a matching end marker).
block_state() {
    awk -v b="$begin_marker" -v e="$end_marker" '
        $0 == b { if (open) bad = 1; open = 1; found = 1; next }
        $0 == e { open = 0 }
        END { if (bad || open) print "broken"; else if (found) print "ok"; else print "none" }
    ' "$1"
}

# (Re)write the marked block at the end of a file. Writes through symlinks.
add_block() {
    local file="$1" line="$2" tmpfile
    mkdir -p "$(dirname "$file")"
    touch "$file"
    if [ "$(block_state "$file")" = "ok" ]; then
        tmpfile="$(mktemp)"
        awk -v b="$begin_marker" -v e="$end_marker" \
            '$0 == b {skip = 1; next} $0 == e {skip = 0; next} !skip' "$file" > "$tmpfile"
        cat "$tmpfile" > "$file"
        rm -f "$tmpfile"
    fi
    # Keep the block on its own lines when the file lacks a final newline.
    if [ -s "$file" ] && [ -n "$(tail -c 1 "$file")" ]; then
        printf '\n' >> "$file"
    fi
    printf '%s\n%s\n%s\n' "$begin_marker" "$line" "$end_marker" >> "$file"
    echo "Added $bin_dir to PATH in $file"
}

manual_instruction() {
    case "$1" in
        bash) echo "Add this line to ~/.bashrc (and to ~/.bash_profile or ~/.profile if your terminal starts login shells):"
              echo "  $path_line" ;;
        zsh)  echo "Add this line to ~/.zshrc:"
              echo "  $path_line" ;;
        fish) echo "Add this line to $fish_config:"
              echo "  $fish_line" ;;
        csh|tcsh) echo "Add this line to ~/.${1}rc:"
              echo "  set path = (\"$bin_dir\" \$path)" ;;
        *)    echo "Add this line to your shell's startup file (for example ~/.profile):"
              echo "  $path_line" ;;
    esac
}

echo
case ":$PATH:" in
    *":$bin_dir:"*)
        echo "$bin_dir is already in PATH." ;;
    *)
        targets=("$HOME/.bashrc")
        lines=("$path_line")
        if [ -f "$HOME/.zshrc" ] || [ "$user_shell_name" = "zsh" ]; then
            targets+=("$HOME/.zshrc"); lines+=("$path_line")
        fi
        if [ -f "$fish_config" ] || [ "$user_shell_name" = "fish" ]; then
            targets+=("$fish_config"); lines+=("$fish_line")
        fi
        # Refuse to touch any file whose block is damaged.
        for file in "${targets[@]}"; do
            if [ -f "$file" ] && [ "$(block_state "$file")" = "broken" ]; then
                echo "install.sh: $file has the line '$begin_marker' without a matching" >&2
                echo "'$end_marker' line. It was not changed. Fix or remove those lines by hand," >&2
                echo "then run ./install.sh again. The command is installed at $dest." >&2
                exit 1
            fi
        done
        for i in "${!targets[@]}"; do
            add_block "${targets[$i]}" "${lines[$i]}"
        done
        case "$user_shell_name" in
            bash|zsh|fish) ;;
            *)
                echo "Your shell (${user_shell:-unknown}) is not configured automatically."
                manual_instruction "$user_shell_name" ;;
        esac ;;
esac

# Final check: does a new login + interactive shell find the command?
check_shell="$user_shell"
case "$user_shell_name" in
    bash|zsh|fish|sh|dash|ksh|mksh|yash) ;;
    *) check_shell="" ;;
esac
if [ -z "$check_shell" ] || [ ! -x "$check_shell" ]; then
    check_shell="$(command -v bash || true)"
fi
check_name="${check_shell##*/}"

found=""
if [ -n "$check_shell" ]; then
    out_file="$(mktemp)"
    runner=()
    # A new session keeps the interactive shell away from this terminal.
    if command -v setsid >/dev/null 2>&1; then
        runner=(setsid)
    fi
    ${runner[@]+"${runner[@]}"} "$check_shell" -l -i -c 'command -v disc2chd' \
        </dev/null >"$out_file" 2>/dev/null &
    check_pid=$!
    ( sleep 10; kill -9 "$check_pid" 2>/dev/null ) >/dev/null 2>&1 &
    watchdog=$!
    wait "$check_pid" 2>/dev/null || true
    kill "$watchdog" 2>/dev/null || true
    wait "$watchdog" 2>/dev/null || true
    if grep -qF "$dest" "$out_file"; then
        found=1
    fi
    rm -f "$out_file"
fi

echo
if [ -n "$found" ]; then
    echo "Installed. Open a new terminal and run: disc2chd"
else
    echo "A new $check_name shell does not find disc2chd yet."
    manual_instruction "$check_name"
    echo "The command always works through its full path: $dest"
fi
