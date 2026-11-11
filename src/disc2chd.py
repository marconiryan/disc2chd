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

VERSION = "1.0.0"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_DEPS = 3


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
    print("disc2chd %s: nothing implemented yet" % VERSION)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
