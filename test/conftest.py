"""Synthetic fixtures for the disc2chd tests. Nothing here touches real data.

How to run the tests
    python3 -m pip install pytest      (or use your distro's pytest package)
    python3 -m pytest test             from the repository root

    Needs Python 3.10+ and bash. Tests that need chdman, 7-Zip, zsh or fish are
    skipped, with the reason shown by `pytest -rs`, when those are not installed.
    Every run uses a temporary HOME, a fake os-release and an explicit PATH.

    DISC2CHD_PROBES=1 python3 -m pytest test/test_chdman_probes.py
        also runs optional probes of raw chdman behavior. They describe the chdman
        version they were written against and may fail on other versions.
"""
from __future__ import annotations

import importlib.util
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "disc2chd.py"


def _find_chdman() -> str | None:
    found = shutil.which("chdman")
    if found:
        return found
    fallback = "/usr/bin/mame/chdman"
    return fallback if os.access(fallback, os.X_OK) else None


CHDMAN = _find_chdman()
SEVENZIP = next((shutil.which(n) for n in ("7z", "7zz", "7za") if shutil.which(n)), None)
BASH = shutil.which("bash")
ZSH = shutil.which("zsh")
FISH = shutil.which("fish")
# The interpreter running the tests, without a virtualenv wrapper.
PYTHON = os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def _sevenzip_has_rar() -> bool:
    if not SEVENZIP:
        return False
    res = subprocess.run([SEVENZIP, "i"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    formats = res.stdout.split("Formats:", 1)[-1].split("Codecs:", 1)[0]
    return bool(re.search(r"\sRar5?\s", formats))


SEVENZIP_RAR = _sevenzip_has_rar()

needs_chdman = pytest.mark.skipif(CHDMAN is None, reason="chdman is not installed")
needs_7z = pytest.mark.skipif(SEVENZIP is None, reason="7-Zip (7z, 7zz or 7za) is not installed")
needs_zsh = pytest.mark.skipif(ZSH is None, reason="zsh is not installed")
needs_fish = pytest.mark.skipif(FISH is None, reason="fish is not installed")
probes = pytest.mark.skipif(os.environ.get("DISC2CHD_PROBES") != "1",
                            reason="optional chdman probe; set DISC2CHD_PROBES=1 to run")

FAKE_OS_RELEASE = 'NAME="Test Linux"\nID=testlinux\n'

SECTOR = 2048
RAW_SECTOR = 2352
STATUS_RE = re.compile(r"^(OK|OK_INTERNAL_ONLY|SKIPPED_[A-Z_]+|FAILED_[A-Z_]+)  (.*)$")

# Names with spaces, brackets, quotes, '&', accents and a leading '-'.
AWKWARD = "-Gämé [Disc 1] (Rev A) & 'Co'"


def load_module():
    spec = importlib.util.spec_from_file_location("disc2chd", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["disc2chd"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Image generators
# --------------------------------------------------------------------------

def make_iso(path: Path, sectors: int = 10241, seed: int = 1, random_share: int = 3) -> Path:
    """Fake ISO: 16 zero sectors, a volume descriptor, then mixed content.

    The default is an odd number of sectors (about 21 MB). random_share=1
    makes the content all random, which compresses slowly.
    """
    rng = random.Random(seed)
    with open(path, "wb") as fh:
        fh.write(b"\0" * SECTOR * 16)
        fh.write((b"\x01CD001\x01" + b"\0" * (SECTOR - 7)))
        pattern = (b"DATA%04d" % seed) * (SECTOR // 8)
        for i in range(sectors - 17):
            kind = i % random_share
            if kind == 0:
                fh.write(rng.randbytes(SECTOR))
            elif kind == 1:
                fh.write(pattern)
            else:
                fh.write(b"\0" * SECTOR)
    return path


def make_raw_bin(path: Path, frames: int, seed: int) -> Path:
    rng = random.Random(seed)
    with open(path, "wb") as fh:
        for i in range(frames):
            fh.write(rng.randbytes(RAW_SECTOR) if i % 3 == 0 else bytes([i % 251]) * RAW_SECTOR)
    return path


def single_cue_text(bin_name: str) -> str:
    return 'FILE "%s" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n' % bin_name


def multi_cue_text(data_bin: str, audio_bin: str) -> str:
    return ('FILE "%s" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
            'FILE "%s" BINARY\n  TRACK 02 AUDIO\n    INDEX 00 00:00:00\n    INDEX 01 00:02:00\n'
            % (data_bin, audio_bin))


@pytest.fixture(scope="session")
def assets(tmp_path_factory) -> dict:
    """Generate the expensive images once per session."""
    base = tmp_path_factory.mktemp("assets")
    a = {
        "iso": make_iso(base / "a.iso", seed=1),
        "iso2": make_iso(base / "b.iso", seed=2),
        "iso3": make_iso(base / "c.iso", seed=3),
        "slow_iso": make_iso(base / "slow.iso", sectors=24001, seed=4, random_share=1),
        "data_bin": make_raw_bin(base / "data.bin", 3000, seed=5),
        "audio_bin": make_raw_bin(base / "audio.bin", 600, seed=6),
    }
    return a


# --------------------------------------------------------------------------
# Running the tool
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def tools_path(tmp_path_factory) -> str:
    """An explicit PATH: symlinks to the detected tools, then the system folders."""
    d = tmp_path_factory.mktemp("tools-bin")
    for tool in (CHDMAN, SEVENZIP):
        if tool:
            os.symlink(tool, d / os.path.basename(tool))
    os.symlink(PYTHON, d / "python3")
    return "%s:/usr/bin:/bin" % d


@pytest.fixture(scope="session")
def fake_os_release(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("os") / "os-release"
    path.write_text(FAKE_OS_RELEASE)
    return path


def base_env(home: Path, path: str, os_release: Path) -> dict:
    """The only environment the tool and scripts see in tests."""
    return {"HOME": str(home), "PATH": path, "LANG": "C.UTF-8",
            "DISC2CHD_OS_RELEASE": str(os_release)}


class Runner:
    def __init__(self, home: Path, path: str, os_release: Path):
        self.home = home
        self.path = path
        self.os_release = os_release

    def env(self, extra: dict | None = None) -> dict:
        env = base_env(self.home, self.path, self.os_release)
        if extra:
            env.update(extra)
        return env

    def __call__(self, *args, cwd: Path, env: dict | None = None, yes: bool = True,
                 stdin=subprocess.DEVNULL, timeout: float = 600) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(SCRIPT)] + (["-y"] if yes else []) + [str(a) for a in args]
        return subprocess.run(cmd, cwd=cwd, env=self.env(env), stdin=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout)

    def popen(self, *args, cwd: Path, env: dict | None = None) -> subprocess.Popen:
        cmd = [sys.executable, str(SCRIPT), "-y", "--plain"] + [str(a) for a in args]
        return subprocess.Popen(cmd, cwd=cwd, env=self.env(env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def tty(self, *args, cwd: Path, env: dict | None = None, answer: str | None = None,
            timeout: float = 600) -> tuple[int, str]:
        """Run with a pseudo-terminal as stdin, stdout and stderr.

        Returns (exit code, output). `answer` is typed once a [y/N] prompt shows.
        """
        import pty
        import select
        master, slave = pty.openpty()
        cmd = [sys.executable, str(SCRIPT)] + [str(a) for a in args]
        proc = subprocess.Popen(cmd, cwd=cwd, env=self.env(dict({"COLUMNS": "200"}, **(env or {}))),
                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        out = b""
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                    if answer is not None and b"[y/N]" in out:
                        os.write(master, answer.encode() + b"\n")
                        answer = None
                elif proc.poll() is not None:
                    break
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        return proc.returncode, out.decode("utf-8", "replace").replace("\r\n", "\n")

    @property
    def log_dir(self) -> Path:
        return self.home / ".local" / "state" / "disc2chd" / "logs"

    def last_log(self) -> str:
        logs = sorted(self.log_dir.glob("disc2chd-*.log"), key=lambda p: p.stat().st_mtime)
        return logs[-1].read_text() if logs else ""


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def run(home, tools_path, fake_os_release) -> Runner:
    return Runner(home, tools_path, fake_os_release)


@pytest.fixture
def games(tmp_path) -> Path:
    g = tmp_path / "games"
    g.mkdir()
    return g


def results(output: str) -> list[tuple[str, str]]:
    """(status, rest of line) for every per-disc result line."""
    found = []
    for line in output.splitlines():
        m = STATUS_RE.match(line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def statuses(output: str) -> list[str]:
    return [s for s, _ in results(output)]


def leftovers(folder: Path) -> list[Path]:
    """Temporary files the tool must never leave behind after a clean run."""
    found = []
    for path in folder.rglob("*"):
        if path.name.endswith(".part") or path.name.startswith(".disc2chd-tmp-"):
            found.append(path)
    return found


def chd_info(path: Path) -> str:
    return subprocess.run([CHDMAN, "info", "-i", str(path)], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, check=True).stdout


def data_sha1(path: Path) -> str:
    m = re.search(r"^Data SHA1:\s*([0-9a-f]{40})", chd_info(path), re.MULTILINE)
    return m.group(1)


def sha1_of(*paths: Path) -> str:
    import hashlib
    h = hashlib.sha1()
    for p in paths:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


def seven_zip(archive: Path, cwd: Path, *members: str, level: int = 1) -> Path:
    subprocess.run([SEVENZIP, "a", "-bd", "-mx=%d" % level, str(archive), "--"] + list(members),
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL)
    return archive


def corrupt_middle(path: Path, length: int = 65536) -> None:
    size = path.stat().st_size
    with open(path, "r+b") as fh:
        fh.seek(size // 2)
        fh.write(b"\xa5" * min(length, size // 4))


def make_partial_chd(source_iso: Path, out: Path, delay: float = 2.0) -> Path:
    """Start chdman and stop it with SIGTERM, leaving an incomplete CHD."""
    proc = subprocess.Popen([CHDMAN, "createdvd", "-np", "1", "-i", str(source_iso), "-o", str(out)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while not out.exists() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(delay)
    proc.send_signal(signal.SIGTERM)
    proc.wait()
    assert out.exists()
    return out


def make_rar4(archive: Path, files: list[tuple[str, Path]]) -> Path:
    """Write a RAR 4 archive with stored (uncompressed) members.

    No RAR tool is needed. Members use Windows-style paths, as WinRAR writes them.
    """
    def block(htype: int, flags: int, body: bytes) -> bytes:
        data = struct.pack("<BHH", htype, flags, 7 + len(body)) + body
        return struct.pack("<H", zlib.crc32(data) & 0xFFFF) + data

    dostime = ((2026 - 1980) << 25) | (1 << 21) | (1 << 16)
    with open(archive, "wb") as out:
        out.write(b"Rar!\x1a\x07\x00")
        out.write(block(0x73, 0, struct.pack("<HI", 0, 0)))
        for name, src in files:
            data = Path(src).read_bytes()
            raw = name.replace("/", "\\").encode("utf-8")
            body = struct.pack("<IIBIIBBHI", len(data), len(data), 2, zlib.crc32(data) & 0xFFFFFFFF,
                               dostime, 20, 0x30, len(raw), 0x20) + raw
            out.write(block(0x74, 0x8000, body))
            out.write(data)
        out.write(block(0x7B, 0x4000, b""))
    return archive


def process_running_with(text: str) -> bool:
    """True if any process has `text` in its command line (reads /proc)."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as fh:
                if text.encode() in fh.read():
                    return True
        except OSError:
            pass
    return False


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in folder.rglob("*")}


# --------------------------------------------------------------------------
# Fake binaries and os-release files
# --------------------------------------------------------------------------

@pytest.fixture
def chdman_wrapper(tmp_path) -> tuple[Path, Path]:
    """A chdman that logs its arguments, then runs the real one."""
    d = tmp_path / "wrapper"
    d.mkdir()
    log = d / "calls.log"
    script = d / "chdman"
    script.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexec "%s" "$@"\n' % (log, CHDMAN))
    script.chmod(0o755)
    return script, log


@pytest.fixture
def sleepy_chdman(tmp_path) -> Path:
    """A chdman placed first in PATH that answers the dependency check, then only sleeps."""
    d = tmp_path / "sleepy"
    d.mkdir()
    script = d / "chdman"
    script.write_text(
        "#!/bin/sh\n"
        'if [ $# -eq 0 ]; then echo "chdman - MAME Compressed Hunks of Data (CHD) manager 0.264"; exit 1; fi\n'
        'if [ $# -eq 1 ] && [ "$1" = createdvd ]; then\n'
        '  echo "Usage:"; echo "   chdman createdvd [options], where valid options are:";\n'
        '  echo "      --hunksize, -hs <bytes>: size of each hunk, in bytes"; exit 1; fi\n'
        "sleep 600\n")
    script.chmod(0o755)
    return d


OS_RELEASE = {
    "debian": ('ID=debian\n', "sudo apt install mame-tools", "sudo apt install 7zip"),
    "ubuntu": ('ID=ubuntu\nID_LIKE=debian\n', "sudo apt install mame-tools", "sudo apt install 7zip"),
    "mint": ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "sudo apt install mame-tools", "sudo apt install 7zip"),
    "fedora": ('ID=fedora\n', "sudo dnf install mame-tools", "sudo dnf install 7zip"),
    "arch": ('ID=arch\n', "sudo pacman -S mame-tools", "sudo pacman -S 7zip"),
    "manjaro": ('ID=manjaro\nID_LIKE=arch\n', "sudo pacman -S mame-tools", "sudo pacman -S 7zip"),
    "opensuse": ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n',
                 "sudo zypper install mame-tools", "sudo zypper install 7zip"),
    "batocera": ('ID=batocera\nNAME="Batocera.linux"\n', "/usr/bin/mame/chdman", "which 7z 7zz 7za"),
    "unknown": ('ID=somethingelse\n', "mame-tools", "'7zip' or 'p7zip'"),
}


def write_os_release(folder: Path, family: str) -> Path:
    path = folder / ("os-release-" + family)
    path.write_text(OS_RELEASE[family][0])
    return path
