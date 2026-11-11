#!/usr/bin/env python3
"""disc2chd: batch-convert disc images to CHD and verify every result."""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 10)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "disc2chd: Python %d.%d or newer is required (found %s).\n"
        % (MIN_PYTHON[0], MIN_PYTHON[1], sys.version.split()[0])
    )
    sys.exit(3)

import argparse
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

VERSION = "1.0.0"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_DEPS = 3

CHDMAN_FALLBACK = "/usr/bin/mame/chdman"
SEVENZIP_NAMES = ("7z", "7zz", "7za")


# --------------------------------------------------------------------------
# Dependency check
# --------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    path: str | None = None
    version: str | None = None
    problem: str | None = None   # set when the tool cannot be used

    @property
    def ok(self) -> bool:
        return self.path is not None and self.problem is None


@dataclass
class Deps:
    chdman: Tool
    sevenzip: Tool
    rar: bool | None = None       # None = unknown
    os_family: str = "unknown"
    os_name: str = "unknown"


def capture(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    """Run a short command and return (exit code, combined output)."""
    try:
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return res.returncode, res.stdout.decode("utf-8", "replace")


def read_os_release() -> tuple[str, list[str], str]:
    path = os.environ.get("DISC2CHD_OS_RELEASE") or "/etc/os-release"
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if "=" not in line or line.startswith("#"):
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip().strip("\"'")
    except OSError:
        pass
    return (values.get("ID", "").lower(),
            values.get("ID_LIKE", "").lower().split(),
            values.get("PRETTY_NAME") or values.get("NAME") or "unknown")


def os_family(os_id: str, id_like: list[str]) -> str:
    def match(ident: str) -> str | None:
        if ident == "batocera":
            return "batocera"
        if ident in ("debian", "ubuntu"):
            return "debian"
        if ident == "fedora":
            return "fedora"
        if ident == "arch":
            return "arch"
        if ident.startswith("opensuse") or ident in ("suse", "sles"):
            return "opensuse"
        return None
    for ident in [os_id] + id_like:
        fam = match(ident)
        if fam:
            return fam
    return "unknown"


INSTALL_HINTS = {
    "debian": ("sudo apt install mame-tools",
               "sudo apt install 7zip   (older releases: sudo apt install p7zip-full)"),
    "fedora": ("sudo dnf install mame-tools",
               "sudo dnf install 7zip   (older releases: sudo dnf install p7zip p7zip-plugins)"),
    "arch": ("sudo pacman -S mame-tools",
             "sudo pacman -S 7zip"),
    "opensuse": ("sudo zypper install mame-tools",
                 "sudo zypper install 7zip   (older releases: sudo zypper install p7zip-full)"),
    "batocera": ("chdman normally ships at %s on this system. Update the system, "
                 "or pass --chdman PATH." % CHDMAN_FALLBACK,
                 "Check whether a 7-Zip binary exists with: which 7z 7zz 7za. "
                 "If none does, extract the archives on another computer, or pass --7z PATH."),
    "unknown": ("chdman is usually in a package named 'mame-tools'. "
                "Install it with your package manager.",
                "7-Zip is usually in a package named '7zip' or 'p7zip'. "
                "Install it with your package manager."),
}


def install_hint(family: str, which: str) -> str:
    chd, sz = INSTALL_HINTS.get(family, INSTALL_HINTS["unknown"])
    return chd if which == "chdman" else sz


def is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def locate(explicit: str | None, env_name: str, names: tuple[str, ...],
           fallbacks: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
    """Return (path, problem). An explicit path or env value must work."""
    for label, value in (("given path", explicit), (env_name, os.environ.get(env_name))):
        if value:
            found = value if os.sep in value else shutil.which(value)
            if found and is_executable(found):
                return os.path.abspath(found), None
            return None, "%s %r is not an executable file" % (label, value)
    for name in names:
        found = shutil.which(name)
        if found:
            return found, None
    for path in fallbacks:
        if is_executable(path):
            return path, None
    return None, None


def probe_chdman(explicit: str | None) -> Tool:
    tool = Tool("chdman")
    path, problem = locate(explicit, "DISC2CHD_CHDMAN", ("chdman",), (CHDMAN_FALLBACK,))
    if not path:
        tool.problem = problem or "not found"
        return tool
    tool.path = path
    _, text = capture([path])
    m = re.search(r"manager\s+(\d+(?:\.\d+)+)", text)
    tool.version = m.group(1) if m else None
    if not text.strip():
        tool.problem = "does not run"
        return tool
    _, usage = capture([path, "createdvd"])
    if "createdvd" not in usage or "--hunksize" not in usage:
        tool.problem = "this chdman does not support createdvd (too old)"
    return tool


def probe_7z(explicit: str | None) -> tuple[Tool, bool | None]:
    tool = Tool("7-Zip")
    path, problem = locate(explicit, "DISC2CHD_7Z", SEVENZIP_NAMES)
    if not path:
        tool.problem = problem or "not found"
        return tool, None
    tool.path = path
    _, text = capture([path])
    m = re.search(r"7-Zip(?:\s*\(\w\))?(?:\s*\[\d+\])?\s+(\d+(?:\.\d+)+)", text)
    tool.version = m.group(1) if m else None
    if "7-Zip" not in text:
        tool.problem = "does not look like 7-Zip"
        return tool, None
    rc, info = capture([path, "i"])
    if rc != 0:
        return tool, None
    formats = info.split("Formats:", 1)[-1].split("Codecs:", 1)[0]
    return tool, bool(re.search(r"\sRar5?\s", formats))


def check_dependencies(args) -> Deps:
    os_id, like, pretty = read_os_release()
    chd = probe_chdman(args.chdman)
    sz, rar = probe_7z(args.sevenzip)
    return Deps(chd, sz, rar, os_family(os_id, like), pretty)


def dependency_report(deps: Deps) -> list[str]:
    lines = ["disc2chd %s" % VERSION,
             "  Python   %s  OK" % sys.version.split()[0],
             "  System   %s (family: %s)" % (deps.os_name, deps.os_family)]
    c = deps.chdman
    if c.ok:
        lines.append("  chdman   %s  version %s  createdvd: yes  OK" % (c.path, c.version or "?"))
    else:
        lines.append("  chdman   MISSING: %s%s" % (c.problem, " (%s)" % c.path if c.path else ""))
        lines.append("           Install: " + install_hint(deps.os_family, "chdman"))
    s = deps.sevenzip
    if s.ok:
        rar = {True: "yes", False: "no", None: "unknown"}[deps.rar]
        lines.append("  7-Zip    %s  version %s  RAR: %s  OK" % (s.path, s.version or "?", rar))
        if deps.rar is not True:
            lines.append("           WARNING: this 7-Zip build cannot open .rar archives; "
                         ".rar files will fail.")
    else:
        lines.append("  7-Zip    MISSING: %s (only needed for .7z/.zip/.rar archives)" % s.problem)
        lines.append("           Install: " + install_hint(deps.os_family, "7z"))
    return lines


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer: %r" % text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more: %r" % text)
    return value


def positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number: %r" % text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0: %r" % text)
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="disc2chd",
        description=(
            "Find disc images (.iso, .cue/.bin, and the same inside .7z/.zip/.rar) "
            "under the given paths (default: the current directory), convert them "
            "to CHD with chdman and verify every CHD against its source."
        ),
    )
    p.add_argument("paths", nargs="*", metavar="PATH",
                   help="folders or files to process (default: current directory)")
    p.add_argument("--no-recursive", action="store_true",
                   help="only scan the top level of each folder")
    p.add_argument("-o", "--output-dir", metavar="DIR",
                   help="write CHD files here (default: next to each source)")
    p.add_argument("--temp-dir", metavar="DIR",
                   help="where to extract archives and temporary files "
                        "(default: a hidden folder inside the output folder)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--move-source-to", metavar="DIR",
                     help="move sources here after the CHD passes verification")
    src.add_argument("--delete-source", action="store_true",
                     help="delete sources after the CHD passes verification")
    p.add_argument("--fast-verify", action="store_true",
                   help="skip the comparison of the CHD against the source")
    p.add_argument("--compression", metavar="LIST",
                   help="chdman codecs (passed as -c)")
    p.add_argument("--hunk-size", metavar="BYTES", type=positive_int,
                   help="chdman hunk size (passed as -hs)")
    p.add_argument("--threads", metavar="N", type=positive_int,
                   help="chdman processors (passed as -np)")
    p.add_argument("--jobs", metavar="N", type=positive_int, default=1,
                   help="parallel jobs (default: 1)")
    p.add_argument("--timeout", metavar="MIN", type=positive_float, default=180.0,
                   help="maximum minutes per disc (default: 180)")
    p.add_argument("--chdman", metavar="PATH", help="chdman binary to use")
    p.add_argument("--7z", dest="sevenzip", metavar="PATH", help="7-Zip binary to use")
    p.add_argument("--dry-run", action="store_true",
                   help="show the plan and write nothing")
    p.add_argument("--verify-only", action="store_true",
                   help="only verify existing .chd files")
    p.add_argument("--check", action="store_true",
                   help="report on dependencies and exit")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    out = p.add_mutually_exclusive_group()
    out.add_argument("-q", "--quiet", action="store_true",
                     help="only print per-disc results and the summary")
    out.add_argument("-v", "--verbose", action="store_true",
                     help="also print the chdman and 7-Zip commands")
    p.add_argument("--plain", action="store_true", help="no in-place progress updates")
    p.add_argument("--no-color", action="store_true", help="disable colors")
    p.add_argument("--log", metavar="FILE", help="log file (default: under the state folder)")
    p.add_argument("--version", action="version", version="disc2chd " + VERSION)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    deps = check_dependencies(args)
    if args.check:
        print("\n".join(dependency_report(deps)))
        return EXIT_OK if deps.chdman.ok else EXIT_DEPS
    if not deps.chdman.ok:
        print("disc2chd: chdman: %s" % deps.chdman.problem, file=sys.stderr)
        print("Install: " + install_hint(deps.os_family, "chdman"), file=sys.stderr)
        return EXIT_DEPS
    print("disc2chd %s: scanning not implemented yet" % VERSION)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
