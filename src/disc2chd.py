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
# Statuses and small helpers
# --------------------------------------------------------------------------

DVD, CD = "DVD", "CD"

OK = "OK"
OK_INTERNAL_ONLY = "OK_INTERNAL_ONLY"
SKIPPED_EXISTS = "SKIPPED_EXISTS"
ORPHAN_BIN = "ORPHAN_BIN"
UNSUPPORTED_CUE = "UNSUPPORTED_CUE"
FAILED_EXTRACT = "FAILED_EXTRACT"
FAILED_MISSING_FILE = "FAILED_MISSING_FILE"
FAILED_CHDMAN = "FAILED_CHDMAN"
FAILED_VERIFY = "FAILED_VERIFY"
FAILED_HASH_MISMATCH = "FAILED_HASH_MISMATCH"
FAILED_TIMEOUT = "FAILED_TIMEOUT"
FAILED_NO_SPACE = "FAILED_NO_SPACE"
FAILED_NAME_COLLISION = "FAILED_NAME_COLLISION"

SUCCESS = (OK, OK_INTERNAL_ONLY, SKIPPED_EXISTS)

SMALL_ISO_BYTES = 750 * 1000 * 1000
ARCHIVE_EXTS = (".7z", ".zip", ".rar")
SCAN_EXTS = (".iso", ".cue", ".bin", ".chd") + ARCHIVE_EXTS
TEMP_PREFIX = ".disc2chd-tmp-"
TEMP_DIR_RE = re.compile(r"^\.disc2chd-tmp-\d+$")


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
    problem: str | None = None


def decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_cue(data: bytes) -> CueSheet:
    lines = decode_text(data).splitlines()
    sheet = CueSheet(lines, [], [])
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
    warnings: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(self.sizes)

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
                return path, False
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
                return path, False
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
            discs.append(Disc("", stem_of(cue_path), cue_path, status=UNSUPPORTED_CUE,
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
            disc.kind, disc.status, disc.reason = "", UNSUPPORTED_CUE, sheet.problem
        elif missing:
            disc.status = FAILED_MISSING_FILE
            disc.reason = "referenced file not found: " + ", ".join(missing)
        elif case_fixed:
            disc.warnings.append("the cue names files with different letter case; "
                                 "using the matching files")
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
            discs.append(Disc("", stem_of(path), path, status=ORPHAN_BIN,
                              reason="not referenced by any .cue; never converted without one"))
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


def chdman_create_cmd(chdman: str, kind: str, source: str, part: str, args) -> list[str]:
    command = "createdvd" if kind == DVD else "createcd"
    # Never hand a .bin to chdman: createcd hangs on it and createdvd
    # silently produces a wrong CHD.
    want = ".iso" if kind == DVD else ".cue"
    if not source.lower().endswith(want):
        raise AssertionError("refusing to run chdman %s on %r" % (command, source))
    cmd = [chdman, command, "-i", source, "-o", part]
    if args.threads:
        cmd += ["-np", str(args.threads)]
    if args.compression:
        cmd += ["-c", args.compression]
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
    problems = [d for d in plan.discs if d.status is not None]
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
    for d in problems:
        lines.append("  %-22s %s: %s" % (d.status, d.label(), d.reason))
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
    paths = args.paths or ["."]
    for p in paths:
        if not os.path.exists(p):
            print("disc2chd: no such file or folder: %s" % p, file=sys.stderr)
            return EXIT_USAGE
    roots = [os.path.abspath(p) for p in paths]
    files, seen = scan_paths(paths, not args.no_recursive)
    if any(ext_of(f) in ARCHIVE_EXTS for f in files) and not deps.sevenzip.ok:
        print("disc2chd: archives were found but 7-Zip is missing: %s"
              % deps.sevenzip.problem, file=sys.stderr)
        print("Install: " + install_hint(deps.os_family, "7z"), file=sys.stderr)
        return EXIT_DEPS
    plan = build_plan(args, deps, files, seen, roots)
    print("\n".join(plan_summary(plan, args)))
    if args.dry_run:
        print("\n".join(dry_run_lines(plan, deps, args)))
    failed = any(d.status is not None and d.status not in SUCCESS for d in plan.discs)
    return EXIT_FAIL if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
