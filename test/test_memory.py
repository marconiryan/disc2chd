"""chdman memory watchdog and the retry without FLAC."""
from __future__ import annotations

import shutil

import pytest

from conftest import CHDMAN, PYTHON, data_sha1, leftovers, needs_chdman, results, sha1_of, statuses

pytestmark = needs_chdman

EXPLANATION = "some chdman builds leak memory with FLAC"
WARNING = "converted without FLAC: chdman ran out of memory with FLAC"

FAKE = r'''#!%(python)s
"""Fake chdman: misbehaves when FLAC would be used, else runs the real one."""
import os, signal, sys, time

REAL = %(real)r
MODE = %(mode)r          # "grow" or "sigkill"
ALWAYS = %(always)r      # misbehave even without FLAC
args = sys.argv[1:]


def flac_used():
    if "-c" not in args:
        return True
    codecs = args[args.index("-c") + 1]
    return "flac" in codecs or "cdfl" in codecs


converting = bool(args) and args[0] in ("createdvd", "createcd") and "-o" in args
if converting and (ALWAYS or flac_used()):
    with open(%(log)r, "a") as fh:
        fh.write("misbehave %%s\n" %% " ".join(args))
    if MODE == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    hog = []
    for _ in range(256):                       # up to 4 GiB, far over the test limit
        hog.append(b"\x01" * (16 << 20))
        time.sleep(0.02)
    time.sleep(60)
    sys.exit(0)
with open(%(log)r, "a") as fh:
    fh.write("delegate %%s\n" %% " ".join(args))
os.execv(REAL, [REAL] + args)
'''


@pytest.fixture
def fake_chdman(tmp_path):
    def make(mode: str, always: bool = False):
        d = tmp_path / ("fake-" + mode + ("-always" if always else ""))
        d.mkdir()
        log = d / "calls.log"
        script = d / "chdman"
        script.write_text(FAKE % {"python": PYTHON, "real": CHDMAN, "mode": mode,
                                  "always": always, "log": str(log)})
        script.chmod(0o755)
        return script, log
    return make


def add_cd(folder, assets, name):
    shutil.copy(assets["data_bin"], folder / (name + ".bin"))
    (folder / (name + ".cue")).write_text(
        'FILE "%s.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n' % name)


@pytest.mark.parametrize("mode", ["grow", "sigkill"])
def test_memory_failure_retries_without_flac(run, games, assets, fake_chdman, mode):
    fake, calls = fake_chdman(mode)
    shutil.copy(assets["iso"], games / "Big.iso")
    res = run("--chdman", fake, "--max-chdman-memory", "64M", cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    line = results(res.stdout)[0][1]
    assert WARNING in line
    assert res.stdout.count(EXPLANATION) == 1
    summary = res.stdout.split("Summary:", 1)[1]
    assert "Warnings:" in summary and WARNING in summary
    log = run.last_log()
    assert WARNING in log and "retrying without FLAC" in log
    if mode == "grow":
        assert "over the 64.0 MiB limit" in log
    else:
        assert "killed by SIGKILL" in log
    logged = calls.read_text()
    assert "misbehave createdvd" in logged
    assert "delegate createdvd" in logged and "-c lzma,zlib,huff" in logged
    assert data_sha1(games / "Big.chd") == sha1_of(games / "Big.iso")
    assert leftovers(games) == []


def test_memory_failure_cd_uses_cd_codecs(run, games, assets, fake_chdman):
    fake, calls = fake_chdman("grow")
    add_cd(games, assets, "Disc")
    res = run("--chdman", fake, "--max-chdman-memory", "64M", cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert "delegate createcd" in calls.read_text() and "-c cdlz,cdzl" in calls.read_text()
    assert leftovers(games) == []


def test_memory_failure_with_compression_given(run, games, assets, fake_chdman):
    fake, calls = fake_chdman("grow")
    shutil.copy(assets["iso"], games / "Big.iso")
    res = run("--chdman", fake, "--max-chdman-memory", "64M",
              "--compression", "lzma,flac", cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout) == ["FAILED_MEMORY"]
    assert "try --compression lzma,zlib,huff" in results(res.stdout)[0][1]
    assert calls.read_text().count("createdvd -i") == 1
    assert not (games / "Big.chd").exists()
    assert leftovers(games) == []


def test_memory_failure_retry_also_fails(run, games, assets, fake_chdman):
    fake, calls = fake_chdman("grow", always=True)
    shutil.copy(assets["iso"], games / "Big.iso")
    res = run("--chdman", fake, "--max-chdman-memory", "64M", cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout) == ["FAILED_MEMORY"]
    reason = results(res.stdout)[0][1]
    assert "retry without FLAC also failed" in reason
    assert "try --compression lzma,zlib,huff" in reason
    assert calls.read_text().count("misbehave createdvd") == 2
    assert not (games / "Big.chd").exists()
    assert leftovers(games) == []


def test_memory_explanation_printed_once(run, games, assets, fake_chdman):
    fake, _ = fake_chdman("sigkill")
    shutil.copy(assets["iso"], games / "One.iso")
    shutil.copy(assets["iso2"], games / "Two.iso")
    add_cd(games, assets, "Three")
    res = run("--chdman", fake, cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"] * 3
    assert res.stdout.count(EXPLANATION) == 1
    assert run.last_log().count(EXPLANATION) == 1
    assert sum(WARNING in rest for _, rest in results(res.stdout)) == 3


def test_watchdog_quiet_with_real_chdman_and_default_limit(run, games, assets):
    shutil.copy(assets["iso"], games / "Normal.iso")
    add_cd(games, assets, "Normal CD")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK", "OK"]
    log = run.last_log()
    assert "chdman memory limit:" in log
    for text in ("FLAC", "out of memory", "over the"):
        assert text not in res.stdout and text not in log

