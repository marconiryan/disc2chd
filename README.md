# disc2chd

`disc2chd` is a command-line tool that finds disc images in a folder, converts
them to CHD with `chdman`, and verifies every CHD against its source.

## What it does

- Scans the current folder (recursively by default).
- Supports DVD images (`.iso`), CD images (`.cue` + `.bin`), and both inside
  `.7z`, `.zip` or `.rar` archives.
- Converts with `chdman createdvd` (ISO) or `chdman createcd` (cue/bin).
- Verifies every CHD: chdman's own checks, then a comparison with the source data.
- Keeps the original files by default.

## Requirements

- Linux with Python 3.10 or newer
- `chdman` (usually in a package named `mame-tools`)
- 7-Zip (`7z`, `7zz` or `7za`), only needed for archives

Run `disc2chd --check` to see what is installed and how to install what is missing.

## Install

```sh
./install.sh
```

Installs the `disc2chd` command to `~/.local/bin` and, if that folder is not in your `PATH`, adds it to your shell's startup file. Open a new terminal afterwards. Run it again to upgrade.

## Usage

```sh
cd /path/to/games
disc2chd
```

Common options:

| Option | Effect |
|---|---|
| `--dry-run` | Show what would be done; write nothing |
| `-y` | Do not ask for confirmation |
| `-o DIR` | Write the CHD files to `DIR` instead of next to the sources |
| `--delete-source` | Delete each source after its CHD passes verification |
| `--verify-only` | Only check existing `.chd` files |
| `--max-chdman-memory SIZE` | Stop chdman above `SIZE` of memory (e.g. `4G`) and retry without FLAC |

See `disc2chd --help` for all options.

## Uninstall

```sh
./uninstall.sh
```

Removes the command, the `PATH` line added by the installer, and the logs.

## License

MIT. See [LICENSE](LICENSE).
