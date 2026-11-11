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
import concurrent.futures
import fcntl
import hashlib
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
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
# Statuses and small helpers
# --------------------------------------------------------------------------

DVD, CD = "DVD", "CD"

OK = "OK"
OK_INTERNAL_ONLY = "OK_INTERNAL_ONLY"
SKIPPED_EXISTS = "SKIPPED_EXISTS"
SKIPPED_NO_CUE = "SKIPPED_NO_CUE"
SKIPPED_UNSUPPORTED = "SKIPPED_UNSUPPORTED"
FAILED_READ = "FAILED_READ"
FAILED_EXTRACT = "FAILED_EXTRACT"
FAILED_MISSING_FILE = "FAILED_MISSING_FILE"
FAILED_CHDMAN = "FAILED_CHDMAN"
FAILED_VERIFY = "FAILED_VERIFY"
FAILED_HASH_MISMATCH = "FAILED_HASH_MISMATCH"
FAILED_TIMEOUT = "FAILED_TIMEOUT"
FAILED_NO_SPACE = "FAILED_NO_SPACE"
FAILED_NAME_COLLISION = "FAILED_NAME_COLLISION"
FAILED_MEMORY = "FAILED_MEMORY"

SUCCESS = (OK, OK_INTERNAL_ONLY, SKIPPED_EXISTS)
SKIPPED = (SKIPPED_NO_CUE, SKIPPED_UNSUPPORTED)   # files left alone; not failures


def is_failure(status: str | None) -> bool:
    return bool(status) and status.startswith("FAILED_")

SMALL_ISO_BYTES = 750 * 1000 * 1000
ARCHIVE_EXTS = (".7z", ".zip", ".rar")
SCAN_EXTS = (".iso", ".cue", ".bin", ".chd") + ARCHIVE_EXTS
TEMP_PREFIX = ".disc2chd-tmp-"
TEMP_DIR_RE = re.compile(r"^\.disc2chd-tmp-\d+$")

# Codecs without FLAC, used when chdman runs out of memory with FLAC.
NO_FLAC_CODECS = {DVD: "lzma,zlib,huff", CD: "cdlz,cdzl"}
NO_FLAC_WARNING = "converted without FLAC: chdman ran out of memory with FLAC"
NO_FLAC_EXPLANATION = (
    "note: some chdman builds leak memory with FLAC (libFLAC 1.5); to skip the failed "
    "attempt next time, use --compression %s (or %s for CD images)."
    % (NO_FLAC_CODECS[DVD], NO_FLAC_CODECS[CD]))
GIB = 1 << 30
DEFAULT_MEMORY_CAP = 4 * GIB


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def stem_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0
    return "?"


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def fmt_binary(n: int) -> str:
    """Sizes given in binary units, as --max-chdman-memory takes them."""
    for unit, scale in (("GiB", GIB), ("MiB", 1 << 20)):
        if n >= scale:
            return "%.1f %s" % (n / float(scale), unit)
    return "%d KiB" % (n // 1024)


def parse_size(text: str) -> int:
    """'4G', '512M', '1.5GiB' or a plain byte count. Units are binary."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?\s*$", text, re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError("not a size like 4G or 512M: %r" % text)
    scale = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2).upper()]
    value = int(float(m.group(1)) * scale)
    if value < 1 << 20:
        raise argparse.ArgumentTypeError("must be at least 1M: %r" % text)
    return value


def mem_total() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def default_memory_limit(jobs: int) -> int:
    total = mem_total()
    if not total:
        return DEFAULT_MEMORY_CAP
    return min(DEFAULT_MEMORY_CAP, total // (2 * max(1, jobs)))


def read_rss(pid: int) -> int | None:
    """Resident memory of a process in bytes (VmRSS), or None."""
    try:
        with open("/proc/%d/status" % pid) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def savings(source_bytes: int, chd_bytes: int) -> str:
    """'saved 61%' from exact byte counts (rounded down), or 'no savings'."""
    if chd_bytes >= source_bytes or source_bytes <= 0:
        return "no savings"
    return "saved %d%%" % (100 * (source_bytes - chd_bytes) // source_bytes)


def show_path(path: str) -> str:
    """Path relative to the current directory when it is inside it."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


# --------------------------------------------------------------------------
# Cue sheets
# --------------------------------------------------------------------------

FILE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]*)"|(\S+))(?:\s+(\S+))?\s*$', re.IGNORECASE)
TRACK_RE = re.compile(r"^\s*TRACK\s+\d+\s+\S+", re.IGNORECASE)
GAP_RE = re.compile(r"^\s*(PREGAP|POSTGAP)\b", re.IGNORECASE)


@dataclass
class CueSheet:
    lines: list[str]
    files: list[str]              # names exactly as written in FILE lines
    file_line_index: list[int]    # line number of each FILE line
    tracks: int = 0
    has_gaps: bool = False
    has_bom: bool = False
    problem: str | None = None
    bad_types: list[str] = field(default_factory=list)   # unsupported FILE types


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_cue(data: bytes) -> CueSheet:
    lines = decode_text(data).splitlines()
    sheet = CueSheet(lines, [], [], has_bom=data.startswith(b"\xef\xbb\xbf"))
    bad_types = []
    for i, line in enumerate(lines):
        m = FILE_RE.match(line)
        if m:
            name = m.group(1) if m.group(1) is not None else m.group(2)
            ftype = (m.group(3) or "").upper()
            sheet.files.append(name)
            sheet.file_line_index.append(i)
            if ftype != "BINARY":
                bad_types.append("%s (%s)" % (name, ftype or "no type"))
                if (ftype or "no type") not in sheet.bad_types:
                    sheet.bad_types.append(ftype or "no type")
        elif TRACK_RE.match(line):
            sheet.tracks += 1
        elif GAP_RE.match(line):
            sheet.has_gaps = True
    if bad_types:
        sheet.problem = "only BINARY files are supported, found: " + ", ".join(bad_types)
    elif not sheet.files:
        sheet.problem = "no FILE lines"
    elif not sheet.tracks:
        sheet.problem = "no TRACK lines"
    return sheet


# --------------------------------------------------------------------------
# Scan and classification
# --------------------------------------------------------------------------

@dataclass
class Disc:
    kind: str                     # DVD, CD, or "" for problem items
    name: str                     # display / output name (no extension)
    image: str                    # .iso / .cue / .bin path (absolute, or member path)
    data_files: list[str] = field(default_factory=list)   # iso, or bins in cue order
    sizes: list[int] = field(default_factory=list)
    tracks: int = 0
    has_gaps: bool = False
    case_fixed: bool = False
    cue: CueSheet | None = None
    final: str = ""
    status: str | None = None     # set at planning time or when finished
    reason: str = ""
    archive: str | None = None
    root: str = ""
    index: int = 0
    chd_size: int = 0
    seconds: float = 0.0
    started: float = 0.0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)    # warnings shown with the result

    @property
    def size(self) -> int:
        return sum(self.sizes)

    @property
    def rewrite_cue(self) -> bool:
        # chdman cannot read a cue with a UTF-8 BOM, nor file names that only
        # match with different letter case or backslashes.
        return self.kind == CD and (self.case_fixed or bool(self.cue and self.cue.has_bom))

    def label(self) -> str:
        if self.archive:
            return "%s [%s]" % (show_path(self.archive), self.image)
        return show_path(self.image)


@dataclass
class Job:
    root: str
    discs: list[Disc]
    archive: str | None = None
    members: list[tuple[str, int]] = field(default_factory=list)
    verify_chd: str | None = None     # --verify-only jobs
    error: str = ""                   # why extraction failed
    error_status: str = FAILED_EXTRACT

    @property
    def unpacked_size(self) -> int:
        return sum(size for _, size in self.members)


class LooseView:
    """Classification helpers for files on disk."""

    def read(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read(1 << 20)

    def size(self, path: str) -> int:
        return os.path.getsize(path)

    def resolve(self, cue_path: str, name: str) -> tuple[str | None, bool]:
        base = os.path.dirname(cue_path)
        candidates = [name] + ([name.replace("\\", "/")] if "\\" in name else [])
        for cand in candidates:
            path = os.path.normpath(os.path.join(base, cand))
            if os.path.isfile(path):
                return path, cand != name
        for cand in candidates:
            path = os.path.normpath(os.path.join(base, cand))
            found = self._resolve_case(path)
            if found:
                return found, True
        return None, False

    @staticmethod
    def _resolve_case(path: str) -> str | None:
        parts = path.split(os.sep)
        cur = os.sep if path.startswith(os.sep) else "."
        for comp in parts:
            if not comp:
                continue
            nxt = os.path.join(cur, comp)
            if os.path.exists(nxt):
                cur = nxt
                continue
            try:
                entries = os.listdir(cur)
            except OSError:
                return None
            matches = [e for e in entries if e.casefold() == comp.casefold()]
            if len(matches) != 1:
                return None
            cur = os.path.join(cur, matches[0])
        return cur if os.path.isfile(cur) else None


class ArchiveView:
    """Classification helpers for members of an archive (paths use '/')."""

    def __init__(self, sevenzip: str, archive: str, members: list[tuple[str, int]]):
        self.sevenzip = sevenzip
        self.archive = archive
        self.sizes = dict(members)
        self.folded: dict[str, list[str]] = {}
        for name in self.sizes:
            self.folded.setdefault(name.casefold(), []).append(name)

    def read(self, member: str) -> bytes:
        cmd = [self.sevenzip, "x", "-so", "-bd", "-spd", "--", self.archive, member]
        try:
            res = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError(str(exc))
        if res.returncode != 0:
            raise OSError("7-Zip could not read %s" % member)
        return res.stdout

    def size(self, member: str) -> int:
        return self.sizes[member]

    def resolve(self, cue_member: str, name: str) -> tuple[str | None, bool]:
        import posixpath
        base = posixpath.dirname(cue_member)
        candidates = [name] + ([name.replace("\\", "/")] if "\\" in name else [])
        for cand in candidates:
            if cand.startswith("/"):
                continue
            path = posixpath.normpath(posixpath.join(base, cand))
            if path == ".." or path.startswith("../"):
                continue
            if path in self.sizes:
                return path, cand != name
            matches = self.folded.get(path.casefold(), [])
            if len(matches) == 1:
                return matches[0], True
        return None, False


def classify(files: list[str], view) -> list[Disc]:
    """Turn a list of files into discs and problem items (section 5.3 rules)."""
    discs: list[Disc] = []
    claimed: set[str] = set()
    for cue_path in [f for f in files if ext_of(f) == ".cue"]:
        try:
            sheet = parse_cue(view.read(cue_path))
        except OSError as exc:
            discs.append(Disc("", stem_of(cue_path), cue_path, status=FAILED_READ,
                              reason="cannot read cue: %s" % exc))
            continue
        resolved, missing, case_fixed = [], [], False
        for name in sheet.files:
            path, fixed = view.resolve(cue_path, name)
            if path is None:
                missing.append(name)
            else:
                resolved.append(path)
                claimed.add(path)
                case_fixed = case_fixed or fixed
        disc = Disc(CD, stem_of(cue_path), cue_path, resolved, [view.size(p) for p in resolved],
                    tracks=sheet.tracks, has_gaps=sheet.has_gaps, case_fixed=case_fixed, cue=sheet)
        if sheet.problem:
            detail = "/".join(sheet.bad_types) if sheet.bad_types else sheet.problem
            disc.kind, disc.status, disc.reason = "", SKIPPED_UNSUPPORTED, "unsupported cue (%s)" % detail
        elif missing:
            disc.status = FAILED_MISSING_FILE
            disc.reason = "referenced file not found: " + ", ".join(missing)
        elif case_fixed:
            disc.warnings.append("the cue names files with different letter case or "
                                 "backslashes; using the matching files")
        discs.append(disc)
    for path in files:
        ext = ext_of(path)
        if path in claimed:
            continue
        if ext == ".iso":
            disc = Disc(DVD, stem_of(path), path, [path], [view.size(path)])
            if disc.size < SMALL_ISO_BYTES:
                disc.warnings.append("ISO is smaller than 750 MB; it may be a CD image. "
                                     "Converting as DVD anyway.")
            discs.append(disc)
        elif ext == ".bin":
            discs.append(Disc("", stem_of(path), path, status=SKIPPED_NO_CUE,
                              reason="no .cue file references it"))
    return discs


def scan_paths(paths: list[str], recursive: bool, progress=None) -> tuple[dict, int]:
    """Return ({absolute file: scan root}, number of files seen)."""
    found: dict[str, str] = {}
    seen = 0
    for given in paths:
        given = os.path.abspath(given)
        if os.path.isfile(given):
            seen += 1
            if ext_of(given) in SCAN_EXTS:
                found.setdefault(given, os.path.dirname(given))
            continue
        for dirpath, dirnames, filenames in os.walk(given):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")) if recursive else []
            for name in sorted(filenames):
                seen += 1
                if progress and seen % 200 == 0:
                    progress(seen)
                if name.startswith(".") or name.lower().endswith(".part"):
                    continue
                if ext_of(name) in SCAN_EXTS:
                    found.setdefault(os.path.join(dirpath, name), given)
    if progress:
        progress(seen)
    return found, seen


def list_archive(sevenzip: str, archive: str) -> tuple[list[tuple[str, int]], str | None]:
    """List archive members with `7z l -slt`. Returns (files, error)."""
    rc, text = capture([sevenzip, "l", "-slt", "-bd", "--", archive], timeout=300)
    if rc != 0:
        tail = [l for l in text.splitlines() if l.strip()][-3:]
        return [], "7-Zip cannot list the archive (exit %d): %s" % (rc, " ".join(tail))
    body = text.split("\n----------\n", 1)
    if len(body) < 2:
        return [], "7-Zip printed no member list"
    members = []
    for block in re.split(r"\n\s*\n", body[1]):
        info = {}
        for line in block.splitlines():
            key, sep, val = line.partition(" = ")
            if sep:
                info[key.strip()] = val
        path = info.get("Path")
        if not path:
            continue
        is_dir = info.get("Folder") == "+" or info.get("Attributes", "").startswith("D")
        if is_dir:
            continue
        try:
            size = int(info.get("Size") or 0)
        except ValueError:
            size = 0
        members.append((path, size))
    return members, None


def unsafe_member(path: str) -> bool:
    norm = path.replace("\\", "/")
    return norm.startswith("/") or ".." in norm.split("/") or bool(re.match(r"^[A-Za-z]:", norm))


def hidden_member(path: str) -> bool:
    return any(p.startswith(".") or p == "__MACOSX" for p in path.replace("\\", "/").split("/"))


def output_dir_for(source: str, args) -> str:
    return os.path.abspath(args.output_dir) if args.output_dir else os.path.dirname(source)


@dataclass
class Plan:
    jobs: list[Job]
    discs: list[Disc]
    roots: list[str]
    files_seen: int


def build_plan(args, deps: Deps, files: dict, seen: int, roots: list[str],
               progress=None) -> Plan:
    loose = sorted(f for f in files if ext_of(f) not in ARCHIVE_EXTS + (".chd",))
    archives = sorted(f for f in files if ext_of(f) in ARCHIVE_EXTS)
    jobs: list[Job] = []
    all_discs: list[Disc] = []

    for disc in classify(loose, LooseView()):
        disc.root = files.get(disc.image, os.path.dirname(disc.image))
        disc.final = os.path.join(output_dir_for(disc.image, args), disc.name + ".chd")
        all_discs.append(disc)
        if disc.kind:
            jobs.append(Job(disc.root, [disc]))

    for n, archive in enumerate(archives, 1):
        if progress:
            progress("Listing archives %d/%d" % (n, len(archives)))
        root = files[archive]
        members, error = list_archive(deps.sevenzip.path, archive)
        if error and ext_of(archive) == ".rar" and deps.rar is False:
            error = "this 7-Zip build cannot open .rar archives (%s)" % error
        bad = [m for m, _ in members if unsafe_member(m)]
        if bad:
            error = "unsafe path in archive: %s" % bad[0]
        if error:
            all_discs.append(Disc(DVD, stem_of(archive), archive, status=FAILED_EXTRACT,
                                  reason=error, root=root,
                                  final=os.path.join(output_dir_for(archive, args),
                                                     stem_of(archive) + ".chd")))
            continue
        wanted = [m for m, _ in members if not hidden_member(m) and ext_of(m) in SCAN_EXTS[:3]]
        discs = classify(sorted(wanted), ArchiveView(deps.sevenzip.path, archive, members))
        real = [d for d in discs if d.kind]
        for disc in discs:
            disc.archive, disc.root = archive, root
            if disc.kind and len(real) == 1:
                disc.name = stem_of(archive)
            disc.final = os.path.join(output_dir_for(archive, args), disc.name + ".chd")
            all_discs.append(disc)
        if real:
            jobs.append(Job(root, real, archive=archive, members=members))

    # Output-name collisions across all jobs (case-insensitive: the output
    # folder may be on a case-insensitive filesystem).
    by_name: dict[str, list[Disc]] = {}
    for disc in all_discs:
        if disc.kind and disc.status is None:
            by_name.setdefault(os.path.normpath(disc.final).casefold(), []).append(disc)
    for group in by_name.values():
        if len(group) > 1:
            for disc in group:
                others = ", ".join(d.label() for d in group if d is not disc)
                disc.status = FAILED_NAME_COLLISION
                disc.reason = "%s would also be written by %s" % (
                    os.path.basename(disc.final), others)
    return Plan(jobs, all_discs, roots, seen)


def chdman_create_cmd(chdman: str, kind: str, source: str, part: str, args,
                      compression: str | None = None) -> list[str]:
    command = "createdvd" if kind == DVD else "createcd"
    # Never hand a .bin to chdman: createcd hangs on it and createdvd
    # silently produces a wrong CHD.
    want = ".iso" if kind == DVD else ".cue"
    if not source.lower().endswith(want):
        raise AssertionError("refusing to run chdman %s on %r" % (command, source))
    cmd = [chdman, command, "-i", source, "-o", part]
    if args.threads:
        cmd += ["-np", str(args.threads)]
    compression = compression or args.compression
    if compression:
        cmd += ["-c", compression]
    if args.hunk_size:
        cmd += ["-hs", str(args.hunk_size)]
    return cmd


def free_space(path: str) -> int:
    while path and not os.path.isdir(path):
        path = os.path.dirname(path)
    try:
        return shutil.disk_usage(path or ".").free
    except OSError:
        return 0


def plan_summary(plan: Plan, args) -> list[str]:
    todo = [d for d in plan.discs if d.kind and d.status is None]
    exists = [d for d in todo if os.path.exists(d.final)]
    convert = [d for d in todo if d not in exists]
    problems = [d for d in plan.discs if is_failure(d.status)]
    skipped = [d for d in plan.discs if d.status in SKIPPED]
    dvd = sum(1 for d in convert if d.kind == DVD)
    lines = ["Scanned %d files in %s" % (plan.files_seen, ", ".join(show_path(r) or "." for r in plan.roots))]
    lines.append("  To convert:    %d (%d DVD, %d CD), %s" % (
        len(convert), dvd, len(convert) - dvd, fmt_size(sum(d.size for d in convert))))
    lines.append("  Existing CHD:  %d (will be verified, then skipped if valid)" % len(exists))
    counts: dict[str, int] = {}
    for d in problems:
        counts[d.status] = counts.get(d.status, 0) + 1
    lines.append("  Problems:      %d%s" % (len(problems), (" (%s)" % ", ".join(
        "%s: %d" % kv for kv in sorted(counts.items()))) if counts else ""))
    if skipped:
        lines.append("  Skipped:       %d (not converted)" % len(skipped))
        for d in skipped:
            lines.append("    %s  %s" % (d.label(), d.reason))
    lines.append("  Source size:   %s" % fmt_size(sum(d.size for d in todo)))
    devices: dict[int, str] = {}
    for d in todo:
        out = os.path.dirname(d.final)
        probe = out if os.path.isdir(out) else os.path.dirname(out)
        try:
            devices.setdefault(os.stat(probe).st_dev, out)
        except OSError:
            pass
    for out in list(devices.values())[:3]:
        lines.append("  Free space:    %s in %s" % (fmt_size(free_space(out)), show_path(out) or "."))
    return lines


def dry_run_lines(plan: Plan, deps: Deps, args) -> list[str]:
    lines = ["Plan (dry run, nothing is written):"]
    for d in plan.discs:
        if not d.kind or d.status is not None:
            continue
        lines.append("  %-3s %s -> %s%s" % (d.kind, d.label(), show_path(d.final),
                                             "  (exists; would be verified)" if os.path.exists(d.final) else ""))
        src = d.image if not d.archive else os.path.join("<temp>", d.image)
        cmd = chdman_create_cmd(deps.chdman.path, d.kind, src, d.final + ".part", args)
        lines.append("      " + " ".join(shlex_quote(c) for c in cmd))
        for w in d.warnings:
            lines.append("      warning: " + w)
    return lines


def shlex_quote(text: str) -> str:
    import shlex
    return shlex.quote(text)


def risky_roots(roots: list[str]) -> list[str]:
    home = os.path.realpath(os.path.expanduser("~"))
    return [r for r in roots if os.path.realpath(r) in ("/", home)]


# --------------------------------------------------------------------------
# Run state, child processes and signals
# --------------------------------------------------------------------------

class Interrupted(Exception):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class JobTimeout(Exception):
    pass


class RunState:
    def __init__(self):
        self.interrupted = 0
        self.lock = threading.Lock()
        self.procs: set = set()
        self.in_progress: set = set()    # .part files and temp dirs to remove on interrupt

    def add_proc(self, proc) -> None:
        with self.lock:
            self.procs.add(proc)

    def drop_proc(self, proc) -> None:
        with self.lock:
            self.procs.discard(proc)

    def kill_all(self) -> None:
        with self.lock:
            procs = list(self.procs)
        for proc in procs:
            kill_tree(proc)

    def track(self, path: str) -> None:
        with self.lock:
            self.in_progress.add(path)

    def untrack(self, path: str) -> None:
        with self.lock:
            self.in_progress.discard(path)

    def remove_tracked(self) -> None:
        with self.lock:
            paths = list(self.in_progress)
        for path in paths:
            remove_path(path)

    def check(self) -> None:
        if self.interrupted:
            raise Interrupted(self.interrupted)


STATE = RunState()


def kill_tree(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def remove_path(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except OSError:
        pass


def _make_pdeathsig():
    """Ask the kernel to kill children if disc2chd itself dies (kill -9)."""
    try:
        import ctypes
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError, ImportError):
        return None

    def setup() -> None:
        prctl(1, int(signal.SIGKILL))   # PR_SET_PDEATHSIG
    return setup


PDEATHSIG = _make_pdeathsig()
PCT_RE = re.compile(r"(-?nan|\d+(?:\.\d+)?)%\s+complete", re.IGNORECASE)


@dataclass
class RunResult:
    rc: int
    lines: list[str]
    nan: bool = False
    killed: bool = False          # disc2chd itself stopped the process
    memory: str = ""              # why the memory watchdog stopped it
    peak_rss: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def last_message(self) -> str:
        skip = ("chdman - MAME", "Fatal error occurred", "7-Zip ")
        msgs = [l.strip() for l in self.lines if l.strip() and not l.startswith(skip)]
        return msgs[-1] if msgs else "no output"


def run_tool(cmd: list[str], deadline: float | None, log, display,
             progress=None, tick=None, max_rss: int | None = None) -> RunResult:
    """Run a child process, streaming its \\r/\\n separated output.

    progress(fraction) is called for every 'NN% complete' line; tick() is
    called about five times per second. With max_rss, the process group is
    killed when the child's resident memory goes over it.
    Raises JobTimeout / Interrupted.
    """
    log.write("  $ " + " ".join(shlex_quote(c) for c in cmd))
    display.command(cmd)
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True,
                            preexec_fn=PDEATHSIG)
    STATE.add_proc(proc)
    lines: list[str] = []
    nan = timed_out = killed = False
    memory = ""
    peak = 0
    last_rss = 0.0
    buf = b""
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)

    def handle(segment: bytes) -> None:
        nonlocal nan
        text = segment.decode("utf-8", "replace")
        m = PCT_RE.search(text)
        if m:
            if "nan" in m.group(1).lower():
                if not nan:
                    nan = killed = True
                    kill_tree(proc)
            elif progress:
                progress(min(1.0, float(m.group(1)) / 100.0))
            return
        if text.strip() and len(lines) < 5000:
            lines.append(text.rstrip())

    try:
        while True:
            if STATE.interrupted:
                killed = True
                kill_tree(proc)
            if deadline is not None and not timed_out and time.monotonic() > deadline:
                timed_out = killed = True
                kill_tree(proc)
            if max_rss and not killed and time.monotonic() - last_rss >= 0.2:
                last_rss = time.monotonic()
                rss = read_rss(proc.pid) or 0
                peak = max(peak, rss)
                if rss > max_rss:
                    killed = True
                    memory = "chdman used %s, over the %s limit (--max-chdman-memory)" % (
                        fmt_binary(rss), fmt_binary(max_rss))
                    kill_tree(proc)
            if sel.select(0.2):
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for part in parts:
                    handle(part)
            if tick:
                tick()
        if buf:
            handle(buf)
        proc.wait()
    finally:
        sel.close()
        proc.stdout.close()
        if proc.poll() is None:
            kill_tree(proc)
            proc.wait()
        STATE.drop_proc(proc)
    log.write("    exit %s after %s%s" % (proc.returncode, fmt_duration(time.monotonic() - started),
                                            "; peak memory %s" % fmt_binary(peak) if peak else ""))
    if memory:
        log.write("    stopped: " + memory)
    STATE.check()
    if timed_out:
        raise JobTimeout()
    return RunResult(proc.returncode, lines, nan, killed, memory, peak)


def memory_failure(res: RunResult) -> str:
    """Why a chdman run counts as running out of memory, or ''."""
    if res.memory:
        return res.memory
    if res.rc == -signal.SIGKILL and not res.killed:
        return "chdman was killed by SIGKILL (the system likely ran out of memory)"
    return ""


# --------------------------------------------------------------------------
# Log file
# --------------------------------------------------------------------------

def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "disc2chd")


class Log:
    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()
        self.fh = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.fh = open(path, "a", encoding="utf-8")

    @classmethod
    def open_default(cls, explicit: str | None) -> "Log":
        if explicit:
            return cls(explicit)
        folder = os.path.join(state_dir(), "logs")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(folder, "disc2chd-%s.log" % stamp)
        n = 1
        while os.path.exists(path):
            path = os.path.join(folder, "disc2chd-%s-%d.log" % (stamp, n))
            n += 1
        return cls(path)

    def write(self, msg: str) -> None:
        if not self.fh:
            return
        with self.lock:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            for line in msg.splitlines() or [""]:
                self.fh.write("%s %s\n" % (stamp, line))
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()


class NullLog(Log):
    def __init__(self):
        super().__init__(None)


# --------------------------------------------------------------------------
# Temporary folders and stale-file cleanup
# --------------------------------------------------------------------------

class Workspace:
    """Per-run hidden folders named .disc2chd-tmp-<pid>.

    Each one holds a locked '.lock' file while this run is alive, so a later
    run can tell a live folder from one left behind by a crash.
    """

    def __init__(self, temp_dir: str | None):
        self.temp_dir = os.path.abspath(temp_dir) if temp_dir else None
        self.dirs: dict[str, tuple[str, int]] = {}
        self.lock = threading.Lock()

    def _marker(self, parent: str) -> str:
        with self.lock:
            if parent in self.dirs:
                return self.dirs[parent][0]
            os.makedirs(parent, exist_ok=True)
            path = os.path.join(parent, "%s%d" % (TEMP_PREFIX, os.getpid()))
            os.makedirs(path, exist_ok=True)
            fd = os.open(os.path.join(path, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.dirs[parent] = (path, fd)
            return path

    def job_dir(self, out_dir: str) -> str:
        base = self._marker(out_dir)
        if self.temp_dir:
            base = self._marker(self.temp_dir)
        path = tempfile.mkdtemp(prefix="job-", dir=base)
        STATE.track(path)
        return path

    def close(self) -> None:
        with self.lock:
            items = list(self.dirs.values())
            self.dirs.clear()
        for path, fd in items:
            shutil.rmtree(path, ignore_errors=True)
            try:
                os.close(fd)
            except OSError:
                pass


def temp_dir_is_live(path: str) -> bool:
    lock = os.path.join(path, ".lock")
    if os.path.exists(lock):
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)
    m = re.search(r"(\d+)$", path)
    if not m:
        return False
    try:
        os.kill(int(m.group(1)), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return int(m.group(1)) != os.getpid()


def clean_stale(folders: list[str], recursive: bool, log) -> list[str]:
    """Remove *.chd.part files and .disc2chd-tmp-* folders left by crashed runs."""
    removed: list[str] = []
    seen: set[str] = set()
    for top in folders:
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            real = os.path.realpath(dirpath)
            if real in seen:
                dirnames[:] = []
                continue
            seen.add(real)
            live_here = False
            for d in [d for d in dirnames if TEMP_DIR_RE.match(d)]:
                path = os.path.join(dirpath, d)
                if temp_dir_is_live(path):
                    live_here = True
                else:
                    shutil.rmtree(path, ignore_errors=True)
                    removed.append(path)
            if not live_here:
                for name in filenames:
                    if name.lower().endswith(".chd.part"):
                        path = os.path.join(dirpath, name)
                        remove_path(path)
                        removed.append(path)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")) if recursive else []
    for path in removed:
        log.write("Removed leftover from an earlier run: %s" % path)
    return removed


# --------------------------------------------------------------------------
# CHD checks and hashing
# --------------------------------------------------------------------------

ZERO_SHA1 = "0" * 40


@dataclass
class ChdInfo:
    data_sha1: str | None
    tags: list[str]
    tracks: int


def chd_info(chdman: str, path: str, deadline, log, display) -> tuple[ChdInfo | None, str]:
    res = run_tool([chdman, "info", "-i", path], deadline, log, display)
    if res.rc != 0:
        return None, "chdman info exited %d: %s" % (res.rc, res.last_message())
    text = res.text
    m = re.search(r"^Data SHA1:\s*([0-9a-fA-F]{40})", text, re.MULTILINE)
    return ChdInfo(m.group(1).lower() if m else None,
                   re.findall(r"Tag='([^']*)'", text),
                   len(re.findall(r"TRACK:\d+", text))), ""


def check_chd(chdman: str, path: str, kind: str | None, tracks: int, deadline,
              log, display, progress=None) -> tuple[bool, str, ChdInfo | None]:
    """Integrity checks (steps 2 and 3): chdman verify plus chdman info."""
    res = run_tool([chdman, "verify", "-i", path], deadline, log, display, progress=progress)
    text = res.text
    if "No verification to be done" in text:
        return False, "CHD has no checksum (incomplete file from an interrupted run)", None
    if res.rc != 0:
        return False, "chdman verify exited %d: %s" % (res.rc, res.last_message()), None
    if "Overall SHA1 verification successful!" not in text:
        return False, "chdman verify did not report success", None
    info, reason = chd_info(chdman, path, deadline, log, display)
    if info is None:
        return False, reason, None
    if not info.data_sha1:
        return False, "chdman info shows no Data SHA1", info
    if info.data_sha1 == ZERO_SHA1:
        return False, "Data SHA1 is all zeros (incomplete file)", info
    if kind == DVD and "DVD " not in info.tags:
        return False, "not a DVD CHD (tags: %s)" % ", ".join(info.tags), info
    if kind == CD:
        if "CHT2" not in info.tags:
            return False, "not a CD CHD (tags: %s)" % ", ".join(info.tags), info
        if tracks and info.tracks != tracks:
            return False, "CHD has %d tracks, cue has %d" % (info.tracks, tracks), info
    if kind is None and "DVD " not in info.tags and "CHT2" not in info.tags:
        return False, "not a DVD or CD CHD (tags: %s)" % ", ".join(info.tags), info
    log.write("    verify OK; Data SHA1 %s; tags %s; tracks %d"
              % (info.data_sha1, ",".join(t.strip() for t in info.tags), info.tracks))
    return True, "", info


def sha1_files(paths: list[str], deadline, progress=None) -> str:
    """SHA1 of the files concatenated, read in 8 MiB chunks (nothing is written)."""
    h = hashlib.sha1()
    done = 0
    for path in paths:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    break
                h.update(chunk)
                done += len(chunk)
                STATE.check()
                if deadline is not None and time.monotonic() > deadline:
                    raise JobTimeout()
                if progress:
                    progress(done)
    return h.hexdigest()


def folder_bytes(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def same_device(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return True


def space_problem(temp_bytes: int, out_bytes: int, temp_dir: str, out_dir: str) -> str | None:
    """Check free space with a 5% margin. Returns a message when short."""
    temp_need = int(temp_bytes * 1.05)
    out_need = int(out_bytes * 1.05)
    if same_device(temp_dir, out_dir):
        need, free = temp_need + out_need, free_space(out_dir)
        if free < need:
            return "need %s, only %s free in %s" % (fmt_size(need), fmt_size(free), out_dir)
        return None
    for need, folder in ((temp_need, temp_dir), (out_need, out_dir)):
        free = free_space(folder)
        if free < need:
            return "need %s, only %s free in %s" % (fmt_size(need), fmt_size(free), folder)
    return None


def publish(part: str, final: str) -> bool:
    """Move part to final without ever overwriting an existing file."""
    try:
        os.link(part, final)
    except FileExistsError:
        return False
    except OSError:
        if os.path.exists(final):
            return False
        os.replace(part, final)
        return True
    os.unlink(part)
    return True


def write_fixed_cue(disc: Disc, real_paths: list[str], folder: str) -> str:
    """Copy of the cue whose FILE lines point to the real files.

    chdman joins the cue's folder with the FILE value even when that value is
    absolute, so the paths are written relative to the new cue's folder.
    """
    lines = list(disc.cue.lines)
    for idx, real in zip(disc.cue.file_line_index, real_paths):
        rel = os.path.relpath(os.path.abspath(real), folder)
        if '"' in rel:
            raise ValueError("file name contains a double quote: %s" % real)
        lines[idx] = 'FILE "%s" BINARY' % rel
    path = os.path.join(folder, "fixed.cue")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------

class Processor:
    def __init__(self, args, deps: Deps, display, log, workspace: Workspace,
                 keep_archives: set[str]):
        self.args = args
        self.chdman = deps.chdman.path
        self.sevenzip = deps.sevenzip.path
        self.display = display
        self.log = log
        self.ws = workspace
        self.keep_archives = keep_archives
        self.timeout = args.timeout * 60.0
        self.max_rss = args.max_chdman_memory
        self.explained = False
        self.lock = threading.Lock()

    # -- helpers ----------------------------------------------------------

    def finish(self, disc: Disc, status: str, reason: str = "") -> None:
        disc.status, disc.reason = status, reason
        if disc.started:
            disc.seconds = time.monotonic() - disc.started
        self.log.write("[%s] %s%s" % (status, disc.label(), (": " + reason) if reason else ""))
        for note in disc.notes:
            self.log.write("  warning: " + note)
        self.display.result(disc)

    def explain_flac_once(self) -> None:
        with self.lock:
            if self.explained:
                return
            self.explained = True
        self.log.write(NO_FLAC_EXPLANATION)
        self.display.info(NO_FLAC_EXPLANATION)

    def create(self, disc: Disc, source: str, part: str, deadline: float,
               compression: str | None = None) -> RunResult:
        cmd = chdman_create_cmd(self.chdman, disc.kind, os.path.abspath(source), part,
                                self.args, compression)
        t0 = time.monotonic()
        res = run_tool(cmd, deadline, self.log, self.display,
                       progress=self.phase_cb(disc, "convert"), max_rss=self.max_rss)
        self.log.write("  convert took %s" % fmt_duration(time.monotonic() - t0))
        return res

    def phase_cb(self, disc: Disc, name: str, total: int | None = None):
        self.display.phase(disc, name, 0.0)
        if total:
            return lambda done: self.display.phase(disc, name, done / float(total))
        return lambda frac: self.display.phase(disc, name, frac)

    # -- jobs ---------------------------------------------------------------

    def run_job(self, job: Job) -> None:
        if job.verify_chd:
            self.verify_existing_only(job.discs[0])
            return
        extracted: dict[str, str] | None = None
        job_tmp = None
        try:
            for disc in job.discs:
                if disc.status is not None:
                    continue
                self.display.header(disc)
                for w in disc.warnings:
                    self.display.warn(w)
                    self.log.write("  warning: " + w)
                started = disc.started = time.monotonic()
                deadline = started + self.timeout
                try:
                    if os.path.exists(disc.final) and self.existing_is_valid(disc, deadline):
                        disc.chd_size = os.path.getsize(disc.final)
                        disc.seconds = time.monotonic() - started
                        self.finish(disc, SKIPPED_EXISTS, "valid CHD already present")
                        continue
                    if job.archive:
                        if extracted is None:
                            job_tmp = self.ws.job_dir(os.path.dirname(disc.final))
                            extracted = self.extract(job, disc, job_tmp)
                        if not extracted:
                            self.finish(disc, job.error_status, job.error or "extraction failed")
                            continue
                    self.convert(disc, extracted, deadline)
                except JobTimeout:
                    self.finish(disc, FAILED_TIMEOUT, "took longer than %s" % fmt_duration(self.timeout))
                finally:
                    disc.seconds = time.monotonic() - started
                    self.display.disc_done(disc)
        finally:
            if job_tmp:
                remove_path(job_tmp)
                STATE.untrack(job_tmp)
        self.dispose_sources(job)

    def existing_is_valid(self, disc: Disc, deadline: float) -> bool:
        self.log.write("Checking existing %s" % disc.final)
        ok, reason, _ = check_chd(self.chdman, disc.final, disc.kind, disc.tracks, deadline,
                                  self.log, self.display, self.phase_cb(disc, "verify"))
        if ok:
            return True
        bad = "%s.invalid-%s" % (disc.final, time.strftime("%Y%m%d-%H%M%S"))
        os.rename(disc.final, bad)
        msg = "existing CHD is invalid (%s); renamed to %s" % (reason, os.path.basename(bad))
        self.log.write("  " + msg)
        self.display.warn(msg)
        return False

    def extract(self, job: Job, disc: Disc, tmp: str) -> dict[str, str]:
        out_dir = os.path.dirname(disc.final)
        pending = [d for d in job.discs if d.status is None]
        compare = 0 if self.args.fast_verify else max([d.size for d in pending if d.kind == CD] or [0])
        problem = space_problem(job.unpacked_size + compare, sum(d.size for d in pending), tmp, out_dir)
        if problem:
            job.error_status, job.error = FAILED_NO_SPACE, problem
            return {}
        total = max(1, job.unpacked_size)
        update = self.phase_cb(disc, "extract", total)
        last = [0.0]

        def tick() -> None:
            now = time.monotonic()
            if now - last[0] >= 0.5:
                last[0] = now
                update(min(total, folder_bytes(tmp)))

        self.log.write("Extracting %s (%s unpacked)" % (job.archive, fmt_size(job.unpacked_size)))
        res = run_tool([self.sevenzip, "x", "-y", "-bd", "-o" + tmp, "--", job.archive],
                       time.monotonic() + self.timeout, self.log, self.display, tick=tick)
        if res.rc != 0:
            job.error = "7-Zip exited %d: %s" % (res.rc, res.last_message())
            return {}
        mapping = {}
        for d in pending:
            for member in [d.image] + d.data_files:
                path = os.path.join(tmp, *member.replace("\\", "/").split("/"))
                if not os.path.isfile(path):
                    job.error = "member missing after extraction: %s" % member
                    return {}
                mapping[member] = path
        return mapping

    def convert(self, disc: Disc, extracted: dict[str, str] | None, deadline: float) -> None:
        args = self.args
        out_dir = os.path.dirname(disc.final)
        os.makedirs(out_dir, exist_ok=True)
        part = disc.final + ".part"
        source = extracted[disc.image] if extracted else disc.image
        data_files = [extracted[f] for f in disc.data_files] if extracted else list(disc.data_files)
        tmp = None
        STATE.track(part)
        try:
            remove_path(part)
            problem = space_problem(0 if args.fast_verify or disc.kind == DVD else disc.size,
                                    disc.size, out_dir, out_dir)
            if problem:
                self.finish(disc, FAILED_NO_SPACE, problem)
                return
            if disc.kind == CD and (disc.rewrite_cue or not args.fast_verify):
                tmp = self.ws.job_dir(out_dir)
            if disc.rewrite_cue:
                source = write_fixed_cue(disc, data_files, tmp)
                self.log.write("  using a corrected copy of the cue: %s" % source)
            self.log.write("Converting %s (%s, %s)\n  source: %s\n  data files: %s" % (
                disc.label(), disc.kind, fmt_size(disc.size), source, ", ".join(data_files)))
            res = self.create(disc, source, part, deadline)
            memory = memory_failure(res)
            if memory:
                hint = "try --compression %s" % NO_FLAC_CODECS[disc.kind]
                if args.compression:
                    self.finish(disc, FAILED_MEMORY, "%s; %s" % (memory, hint))
                    return
                self.explain_flac_once()
                self.log.write("  %s; retrying without FLAC" % memory)
                self.display.warn("chdman ran out of memory with FLAC; retrying without FLAC")
                remove_path(part)
                res = self.create(disc, source, part, deadline, NO_FLAC_CODECS[disc.kind])
                retry_problem = memory_failure(res)
                if not retry_problem and res.nan:
                    retry_problem = "chdman stalled"
                if not retry_problem and (res.rc != 0 or not os.path.isfile(part)):
                    retry_problem = "chdman exited %d: %s" % (res.rc, res.last_message())
                if retry_problem:
                    self.finish(disc, FAILED_MEMORY, "%s; the retry without FLAC also failed (%s); %s"
                                % (memory, retry_problem, hint))
                    return
                disc.notes.append(NO_FLAC_WARNING)
            if res.nan:
                self.finish(disc, FAILED_CHDMAN, "chdman stalled (progress showed -nan%) and was stopped")
                return
            if res.rc != 0 or not os.path.isfile(part):
                self.finish(disc, FAILED_CHDMAN, "chdman exited %d: %s" % (res.rc, res.last_message()))
                return
            ok, reason, info = check_chd(self.chdman, part, disc.kind, disc.tracks, deadline,
                                         self.log, self.display, self.phase_cb(disc, "verify"))
            if not ok:
                self.finish(disc, FAILED_VERIFY, reason)
                return
            status, note = OK, ""
            if not args.fast_verify:
                status, note = self.compare(disc, part, info, data_files, tmp, deadline)
                if status not in SUCCESS:
                    self.finish(disc, status, note)
                    return
            else:
                self.log.write("  comparison with the source skipped (--fast-verify)")
            if not publish(part, disc.final):
                self.finish(disc, FAILED_NAME_COLLISION,
                            "%s appeared during conversion; not overwritten" % disc.final)
                return
            disc.chd_size = os.path.getsize(disc.final)
            self.log.write("  sizes: %d -> %d bytes, %s" % (
                disc.size, disc.chd_size, savings(disc.size, disc.chd_size)))
            self.finish(disc, status, note)
        finally:
            remove_path(part)
            STATE.untrack(part)
            if tmp:
                remove_path(tmp)
                STATE.untrack(tmp)

    def compare(self, disc: Disc, part: str, info: ChdInfo, data_files: list[str],
                tmp: str | None, deadline: float) -> tuple[str, str]:
        """Step 4: compare the CHD content with the source."""
        src_sha = sha1_files(data_files, deadline,
                             self.phase_cb(disc, "hash source", max(1, disc.size)))
        self.log.write("  source SHA1: %s" % src_sha)
        if disc.kind == DVD:
            self.display.phase(disc, "compare", 1.0)
            if src_sha != info.data_sha1:
                return FAILED_HASH_MISMATCH, "CHD Data SHA1 %s != ISO SHA1 %s" % (info.data_sha1, src_sha)
            self.log.write("  CHD Data SHA1 equals the ISO SHA1")
            return OK, ""
        cue_out = os.path.join(tmp, "compare.cue")
        bin_out = os.path.join(tmp, "compare.bin")
        try:
            res = run_tool([self.chdman, "extractcd", "-i", part, "-o", cue_out, "-ob", bin_out, "-f"],
                           deadline, self.log, self.display,
                           progress=self.phase_cb(disc, "extract for compare"))
            if res.rc != 0 or not os.path.isfile(bin_out):
                return FAILED_VERIFY, "chdman extractcd exited %d: %s" % (res.rc, res.last_message())
            chd_sha = sha1_files([bin_out], deadline,
                                 self.phase_cb(disc, "compare", max(1, os.path.getsize(bin_out))))
        finally:
            remove_path(cue_out)
            remove_path(bin_out)
        self.log.write("  extracted SHA1: %s" % chd_sha)
        if chd_sha == src_sha:
            self.log.write("  extracted data equals the source bins in cue order")
            return OK, ""
        if disc.has_gaps:
            note = ("extracted data differs from the source bins; the cue uses PREGAP/POSTGAP, "
                    "which chdman may store differently. Internal CHD checks passed.")
            self.log.write("  " + note)
            return OK_INTERNAL_ONLY, note
        return FAILED_HASH_MISMATCH, "extracted SHA1 %s != source SHA1 %s" % (chd_sha, src_sha)

    def verify_existing_only(self, disc: Disc) -> None:
        self.display.header(disc)
        started = disc.started = time.monotonic()
        try:
            ok, reason, _ = check_chd(self.chdman, disc.image, None, 0,
                                      started + self.timeout, self.log, self.display,
                                      self.phase_cb(disc, "verify"))
            disc.chd_size = disc.size
            disc.seconds = time.monotonic() - started
            self.finish(disc, OK if ok else FAILED_VERIFY, reason)
        except JobTimeout:
            self.finish(disc, FAILED_TIMEOUT, "took longer than %s" % fmt_duration(self.timeout))
        self.display.disc_done(disc)

    def dispose_sources(self, job: Job) -> None:
        args = self.args
        if not (args.move_source_to or args.delete_source):
            return
        if any(d.status != OK for d in job.discs) or job.archive in self.keep_archives:
            self.log.write("Sources kept for %s (not every disc is OK)" % (job.archive or job.discs[0].image))
            return
        if job.archive:
            paths = [job.archive]
        else:
            paths = []
            for d in job.discs:
                for p in [d.image] + d.data_files:
                    if p not in paths:
                        paths.append(p)
        for path in paths:
            try:
                if args.delete_source:
                    os.remove(path)
                    self.log.write("Deleted source %s" % path)
                    continue
                rel = os.path.relpath(path, job.root)
                if rel.startswith(".."):
                    rel = os.path.basename(path)
                dest = os.path.join(os.path.abspath(args.move_source_to), rel)
                if os.path.exists(dest):
                    self.display.warn("not moving %s: %s already exists" % (path, dest))
                    self.log.write("Not moved (destination exists): %s" % path)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(path, dest)
                self.log.write("Moved source %s -> %s" % (path, dest))
            except OSError as exc:
                self.display.warn("could not handle source %s: %s" % (path, exc))
                self.log.write("Source handling failed for %s: %s" % (path, exc))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

COLORS = {"green": "32", "cyan": "36", "blue": "34", "yellow": "33", "red": "31", "bold": "1"}


def status_color(status: str) -> str:
    if status == OK:
        return "green"
    if status == OK_INTERNAL_ONLY:
        return "cyan"
    if status == SKIPPED_EXISTS:
        return "blue"
    if status in SKIPPED:
        return "yellow"
    return "red"


@dataclass
class PhaseState:
    disc: Disc
    name: str
    started: float
    frac: float = 0.0
    step: int = 0


class Display:
    """Terminal output: headers, live status line (or plain lines), results."""

    def __init__(self, args):
        self.quiet = args.quiet
        self.verbose = args.verbose
        self.tty = sys.stdout.isatty()
        self.live = self.tty and not args.plain
        self.color = self.tty and not args.no_color and "NO_COLOR" not in os.environ
        self.multi = args.jobs > 1
        self.lock = threading.RLock()
        self.total_discs = self.done_discs = 0
        self.active: dict[int, PhaseState] = {}
        self.shown = False
        self.last_render = 0.0

    # -- low level ------------------------------------------------------------

    def paint(self, text: str, color: str) -> str:
        return "\033[%sm%s\033[0m" % (COLORS[color], text) if self.color else text

    def _clear_line(self) -> None:
        if self.shown:
            sys.stdout.write("\r\033[K")
            self.shown = False

    def out(self, text: str) -> None:
        with self.lock:
            self._clear_line()
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
            if self.active and self.live and not self.quiet:
                self._render(force=True)

    def clear(self) -> None:
        with self.lock:
            self._clear_line()
            sys.stdout.flush()

    # -- messages ---------------------------------------------------------------

    def info(self, text: str) -> None:
        if not self.quiet:
            self.out(text)

    def warn(self, text: str) -> None:
        if not self.quiet:
            self.out("  " + self.paint("warning:", "yellow") + " " + text)

    def banner(self, text: str) -> None:
        line = "!" * min(78, len(text) + 4)
        self.out(self.paint("%s\n! %s !\n%s" % (line, text, line), "red"))

    def command(self, cmd: list[str]) -> None:
        if self.verbose:
            self.out("    $ " + " ".join(shlex_quote(c) for c in cmd))

    def scan_progress(self, value) -> None:
        if not self.live or self.quiet:
            return
        text = value if isinstance(value, str) else "Scanning... %d files" % value
        with self.lock:
            sys.stdout.write("\r\033[K" + text)
            sys.stdout.flush()
            self.shown = True

    def scan_done(self, seen) -> None:
        self.clear()

    def summary(self, lines: list[str]) -> None:
        for line in lines:
            m = re.match(r"^(\s+)([A-Z_]+)(\s.*)$", line)
            if m and (m.group(2) in SUCCESS + SKIPPED or is_failure(m.group(2))):
                line = m.group(1) + self.paint(m.group(2), status_color(m.group(2))) + m.group(3)
            self.out(line)

    # -- per disc ---------------------------------------------------------------

    def start(self, total_discs: int) -> None:
        self.total_discs = total_discs

    def header(self, disc: Disc) -> None:
        width = len(str(self.total_discs))
        self.info(self.paint("[%*d/%d] %s   %s   %s" % (
            width, disc.index, self.total_discs, disc.name, disc.kind, fmt_size(disc.size)), "bold"))

    def phase(self, disc: Disc, name: str, frac: float) -> None:
        if self.quiet:
            return
        with self.lock:
            now = time.monotonic()
            st = self.active.get(disc.index)
            changed = st is None or st.name != name
            if changed:
                st = PhaseState(disc, name, now)
                self.active[disc.index] = st
            st.frac = max(0.0, min(1.0, frac))
            prefix = "  [%d] " % disc.index if self.multi else "  "
            if self.live:
                self._render(force=changed)
            elif changed:
                self.out("%s%s" % (prefix, name))
            else:
                step = int(st.frac * 10 + 1e-9)
                if step > st.step:
                    st.step = step
                    self.out("%s%s %d%%" % (prefix, name, step * 10))

    def disc_done(self, disc: Disc) -> None:
        with self.lock:
            self.active.pop(disc.index, None)
            self.done_discs += 1
            if not self.active:
                self._clear_line()

    def result(self, disc: Disc) -> None:
        line = result_line(disc)
        self.out(self.paint(disc.status, status_color(disc.status)) + line[len(disc.status):])

    # -- live status line ---------------------------------------------------------

    def _render(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self.active or (not force and now - self.last_render < 0.1):
            return
        self.last_render = now
        states = sorted(self.active.values(), key=lambda s: s.disc.index)
        if len(states) == 1 and not self.multi:
            st = states[0]
            filled = int(st.frac * 20)
            text = "  %-19s [%s%s] %5.1f%%" % (st.name, "#" * filled, "-" * (20 - filled), st.frac * 100)
        else:
            text = "  " + "  ".join("[%d] %s %d%%" % (s.disc.index, s.name, s.frac * 100) for s in states)
        text += " | %d/%d discs done" % (self.done_discs, self.total_discs)
        width = shutil.get_terminal_size((100, 20)).columns - 1
        sys.stdout.write("\r\033[K" + text[:width])
        sys.stdout.flush()
        self.shown = True


def result_line(disc: Disc) -> str:
    if disc.kind == "CHD":
        text = "%s  %s  %s  %s" % (disc.status, show_path(disc.image), fmt_size(disc.size),
                                   fmt_duration(disc.seconds))
        return text + (": " + disc.reason if disc.reason else "")
    if disc.status in (OK, OK_INTERNAL_ONLY, SKIPPED_EXISTS) and disc.chd_size:
        text = "%s  %s  %s -> %s (%s)  %s" % (
            disc.status, os.path.basename(disc.final), fmt_size(disc.size), fmt_size(disc.chd_size),
            savings(disc.size, disc.chd_size), fmt_duration(disc.seconds))
        if disc.status == OK_INTERNAL_ONLY:
            text += "  (%s)" % disc.reason
        for note in disc.notes:
            text += "  warning: " + note
        return text
    return "%s  %s: %s" % (disc.status, disc.label(), disc.reason)


def final_summary(discs: list[Disc], elapsed: float, log_path: str | None) -> list[str]:
    counts: dict[str, int] = {}
    for d in discs:
        if d.status:
            counts[d.status] = counts.get(d.status, 0) + 1
    lines = ["", "Summary:"]
    lines.append("  " + (", ".join("%s: %d" % kv for kv in sorted(counts.items())) or "nothing done"))
    failures = [d for d in discs if is_failure(d.status)]
    if failures:
        lines.append("  Problems:")
        for d in failures:
            lines.append("    %-22s %s: %s" % (d.status, d.label(), d.reason))
    skipped = [d for d in discs if d.status in SKIPPED]
    if skipped:
        lines.append("  Skipped:")
        for d in skipped:
            lines.append("    %-22s %s: %s" % (d.status, d.label(), d.reason))
    noted = [d for d in discs if d.notes]
    if noted:
        lines.append("  Warnings:")
        for d in noted:
            for note in d.notes:
                lines.append("    %s: %s" % (os.path.basename(d.final), note))
    done = [d for d in discs if d.status in (OK, OK_INTERNAL_ONLY) and d.kind in (DVD, CD)]
    if done:
        src = sum(d.size for d in done)
        chd = sum(d.chd_size for d in done)
        if chd < src:
            lines.append("  Size: %s -> %s (saved %s, %s)" % (
                fmt_size(src), fmt_size(chd), fmt_size(src - chd), savings(src, chd)[len("saved "):]))
        else:
            lines.append("  Size: %s -> %s (no savings)" % (fmt_size(src), fmt_size(chd)))
    lines.append("  Elapsed: %s" % fmt_duration(elapsed))
    if log_path:
        lines.append("  Log: %s" % log_path)
    return lines


def exit_code_for(discs: list[Disc]) -> int:
    return EXIT_FAIL if any(is_failure(d.status) for d in discs) else EXIT_OK


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
    p.add_argument("--max-chdman-memory", metavar="SIZE", type=parse_size,
                   help="stop chdman when its memory use goes over SIZE (e.g. 4G, 512M) and "
                        "retry without FLAC (default: 4G or half the RAM divided by --jobs, "
                        "whichever is smaller)")
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


def install_signal_handlers() -> None:
    def handler(signum, frame):
        if STATE.interrupted:
            return
        STATE.interrupted = signum
        sys.stdout.write("\nInterrupted, cleaning up\u2026\n")
        sys.stdout.flush()
        STATE.kill_all()
        raise Interrupted(signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)


def confirm_question(convert: int, existing: int) -> str:
    if convert:
        return "Convert %d disc%s? [y/N] " % (convert, "" if convert == 1 else "s")
    return "Verify %d existing CHD%s? [y/N] " % (existing, "" if existing == 1 else "s")


def confirm(args, question: str) -> int | None:
    """Return an exit code to stop with, or None to go on."""
    if args.yes:
        return None
    if not sys.stdin.isatty():
        print("disc2chd: standard input is not a terminal; pass -y to proceed without asking.",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        answer = input(question)
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("Nothing done.")
        return EXIT_USAGE
    return None


def run_jobs(jobs: list[Job], processor: Processor, workers: int) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(processor.run_job, job): job for job in jobs}
        pending = set(futures)
        try:
            while pending:
                done, pending = concurrent.futures.wait(pending, timeout=0.3)
                for fut in done:
                    exc = fut.exception()
                    if exc is not None and not isinstance(exc, Interrupted):
                        job = futures[fut]
                        processor.log.write("Internal error in job %s: %r" % (
                            job.archive or job.discs[0].image, exc))
                        for d in job.discs:
                            if d.status is None:
                                processor.finish(d, FAILED_CHDMAN, "internal error: %s" % exc)
        except Interrupted:
            STATE.kill_all()
            concurrent.futures.wait(pending, timeout=20)
            raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_chdman_memory is None:
        args.max_chdman_memory = default_memory_limit(args.jobs)
    install_signal_handlers()
    try:
        return run(args)
    except Interrupted as exc:
        STATE.kill_all()
        STATE.remove_tracked()
        return 128 + exc.signum


def run(args) -> int:
    started = time.monotonic()
    deps = check_dependencies(args)
    if args.check:
        print("\n".join(dependency_report(deps)))
        return EXIT_OK if deps.chdman.ok else EXIT_DEPS
    if not deps.chdman.ok:
        print("disc2chd: chdman: %s" % deps.chdman.problem, file=sys.stderr)
        print("Install: " + install_hint(deps.os_family, "chdman"), file=sys.stderr)
        return EXIT_DEPS
    paths = args.paths or ["."]
    for p in paths:
        if not os.path.exists(p):
            print("disc2chd: no such file or folder: %s" % p, file=sys.stderr)
            return EXIT_USAGE
    roots = [os.path.abspath(p) for p in paths]
    display = Display(args)
    files, seen = scan_paths(paths, not args.no_recursive, display.scan_progress)
    display.scan_done(seen)

    if args.verify_only:
        jobs = []
        for path in sorted(f for f in files if ext_of(f) == ".chd"):
            disc = Disc("CHD", stem_of(path), path, [path], [os.path.getsize(path)],
                        final=path, root=files[path])
            jobs.append(Job(files[path], [disc], verify_chd=path))
        plan = Plan(jobs, [j.discs[0] for j in jobs], roots, seen)
        if not jobs:
            print("No .chd files found.")
            return EXIT_OK
        print("Verifying %d CHD files (%s)" % (len(jobs), fmt_size(sum(j.discs[0].size for j in jobs))))
    else:
        if any(ext_of(f) in ARCHIVE_EXTS for f in files) and not deps.sevenzip.ok:
            print("disc2chd: archives were found but 7-Zip is missing: %s"
                  % deps.sevenzip.problem, file=sys.stderr)
            print("Install: " + install_hint(deps.os_family, "7z"), file=sys.stderr)
            return EXIT_DEPS
        plan = build_plan(args, deps, files, seen, roots, display.scan_progress)
        display.scan_done(None)
        if not plan.discs:
            print("No disc images found in %s." % ", ".join(show_path(r) or "." for r in roots))
            return EXIT_OK
        if not args.quiet:
            print("\n".join(plan_summary(plan, args)))
        for d in plan.discs:
            if is_failure(d.status):
                display.result(d)
    for root in risky_roots(roots):
        display.banner("WARNING: scanning %s. This may cover far more than a games folder." % root)
    if args.dry_run:
        if not args.verify_only:
            print("\n".join(dry_run_lines(plan, deps, args)))
        return exit_code_for(plan.discs)

    jobs = [j for j in plan.jobs if any(d.status is None for d in j.discs)]
    if not jobs:
        print("Nothing to convert.")
        return exit_code_for(plan.discs)
    if not args.verify_only:
        pending = [d for j in jobs for d in j.discs if d.status is None]
        existing = sum(1 for d in pending if os.path.exists(d.final))
        stop = confirm(args, confirm_question(len(pending) - existing, existing))
        if stop is not None:
            return stop

    log = Log.open_default(args.log)
    log.write("disc2chd %s; chdman %s %s; 7-Zip %s %s" % (
        VERSION, deps.chdman.path, deps.chdman.version, deps.sevenzip.path, deps.sevenzip.version))
    log.write("Arguments: %s" % " ".join(sys.argv[1:]))
    log.write("chdman memory limit: %s" % fmt_binary(args.max_chdman_memory))
    for line in plan_summary(plan, args) if not args.verify_only else []:
        log.write(line)
    workspace = Workspace(args.temp_dir)
    try:
        if not args.verify_only:
            clean = list(roots)
            if args.output_dir:
                clean.append(os.path.abspath(args.output_dir))
            if args.temp_dir:
                clean.append(os.path.abspath(args.temp_dir))
            for path in clean_stale(clean, not args.no_recursive, log):
                display.info("Removed leftover: %s" % show_path(path))
        todo = [d for j in jobs for d in j.discs if d.status is None]
        for n, disc in enumerate(todo, 1):
            disc.index = n
        display.start(len(todo))
        keep = {d.archive for d in plan.discs if d.archive and d.status is not None}
        processor = Processor(args, deps, display, log, workspace, keep)
        run_jobs(jobs, processor, args.jobs)
        display.clear()
        summary = final_summary(plan.discs, time.monotonic() - started, log.path)
        for line in summary:
            log.write(line)
        display.summary(summary)
        return exit_code_for(plan.discs)
    except Interrupted as exc:
        display.clear()
        log.write("Interrupted by signal %d; cleaning up" % exc.signum)
        raise
    finally:
        STATE.remove_tracked()
        workspace.close()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
