"""Orphans, interruptions, timeouts, modes and options (tests 12, 17-23, 28, 29)."""
from __future__ import annotations

import shutil
import signal
import time

import pytest

from conftest import (corrupt_middle, leftovers, make_partial_chd, needs_7z, needs_chdman,
                      process_running_with, results, seven_zip, single_cue_text, snapshot,
                      statuses)

pytestmark = needs_chdman


def wait_for(predicate, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def chdman_running_on(path) -> bool:
    return process_running_with(str(path))


def test_12_orphan_bin_never_passed_to_chdman(run, games, assets, chdman_wrapper):
    wrapper, calls = chdman_wrapper
    shutil.copy(assets["data_bin"], games / "Lonely.bin")
    shutil.copy(assets["data_bin"], games / "Paired.bin")
    (games / "Paired.cue").write_text(single_cue_text("Paired.bin"))
    res = run("--chdman", wrapper, cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert "Lonely.bin  no .cue file references it" in res.stdout
    summary = res.stdout.split("Summary:", 1)[1]
    assert "SKIPPED_NO_CUE: 1" in summary
    logged = calls.read_text()
    assert "createcd" in logged
    for line in logged.splitlines():
        assert "Lonely" not in line
        args = line.split()
        if "-i" in args:
            assert not args[args.index("-i") + 1].lower().endswith(".bin")
    assert not (games / "Lonely.chd").exists()


@pytest.mark.parametrize("sig,code", [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_17_signals_during_conversion(run, games, assets, sig, code):
    shutil.copy(assets["slow_iso"], games / "Slow.iso")
    part = games / "Slow.chd.part"
    proc = run.popen("--threads", "1", cwd=games)
    assert wait_for(part.exists), "conversion did not start"
    time.sleep(0.5)
    proc.send_signal(sig)
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == code, out
    assert "Interrupted, cleaning up" in out
    assert leftovers(games) == []
    assert not (games / "Slow.chd").exists()
    assert wait_for(lambda: not chdman_running_on(part), 10)
    res = run("--threads", "4", cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]


@needs_7z
def test_18_kill_9_leaves_garbage_that_next_run_cleans(run, games, assets, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["slow_iso"], stage / "Slow.iso")
    seven_zip(games / "Slow.7z", stage, "Slow.iso", level=0)
    part = games / "Slow.chd.part"
    proc = run.popen("--threads", "1", cwd=games)
    assert wait_for(part.exists), "conversion did not start"
    time.sleep(0.5)
    proc.kill()
    proc.communicate(timeout=30)
    assert wait_for(lambda: not chdman_running_on(part), 10), "chdman outlived disc2chd"
    garbage = leftovers(games)
    assert part in garbage
    assert any(p.name.startswith(".disc2chd-tmp-") for p in garbage)
    res = run("--threads", "4", cwd=games)
    assert res.returncode == 0, res.stdout
    assert "Removed leftover" in res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert leftovers(games) == []
    assert "Removed leftover from an earlier run" in run.last_log()


@needs_7z
def test_19_dry_run_writes_nothing(run, games, assets, tmp_path):
    shutil.copy(assets["iso"], games / "Plan.iso")
    shutil.copy(assets["data_bin"], games / "Plan CD.bin")
    (games / "Plan CD.cue").write_text(single_cue_text("Plan CD.bin"))
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso2"], stage / "x.iso")
    seven_zip(games / "Arch.7z", stage, "x.iso")
    (games / "stale.chd.part").write_bytes(b"x")
    before = snapshot(games)
    res = run("--dry-run", cwd=games)
    assert res.returncode == 0, res.stdout
    assert "createdvd" in res.stdout and "createcd" in res.stdout
    assert snapshot(games) == before
    assert not run.log_dir.exists()


def test_20_move_source_after_ok(run, games, assets, tmp_path):
    shutil.copy(assets["iso"], games / "Keep.iso")
    shutil.copy(assets["data_bin"], games / "CD.bin")
    (games / "CD.cue").write_text(single_cue_text("CD.bin"))
    (games / "Bad.iso").write_bytes(assets["iso"].read_bytes()[:-5])
    moved = tmp_path / "moved"
    res = run("--move-source-to", moved, cwd=games)
    assert res.returncode == 1, res.stdout
    assert sorted(statuses(res.stdout)) == ["FAILED_CHDMAN", "OK", "OK"]
    assert sorted(p.name for p in moved.iterdir()) == ["CD.bin", "CD.cue", "Keep.iso"]
    assert (games / "Bad.iso").exists()
    assert sorted(p.name for p in games.iterdir()) == ["Bad.iso", "CD.chd", "Keep.chd"]


@needs_7z
def test_20_delete_source_after_ok(run, games, assets, tmp_path):
    shutil.copy(assets["iso"], games / "Gone.iso")
    (games / "Bad.iso").write_bytes(assets["iso"].read_bytes()[:-5])
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso2"], stage / "x.iso")
    archive = seven_zip(games / "Broken.7z", stage, "x.iso")
    corrupt_middle(archive)
    res = run("--delete-source", cwd=games)
    assert res.returncode == 1, res.stdout
    assert sorted(statuses(res.stdout)) == ["FAILED_CHDMAN", "FAILED_EXTRACT", "OK"]
    assert sorted(p.name for p in games.iterdir()) == ["Bad.iso", "Broken.7z", "Gone.chd"]


def test_20_move_and_delete_are_exclusive(run, games, tmp_path):
    res = run("--delete-source", "--move-source-to", tmp_path / "x", cwd=games)
    assert res.returncode == 2


def test_21_timeout(run, games, assets, sleepy_chdman):
    shutil.copy(assets["iso"], games / "Stuck.iso")
    env = {"PATH": "%s:%s" % (sleepy_chdman, run.path)}
    started = time.time()
    res = run("--timeout", "0.05", cwd=games, env=env)
    assert time.time() - started < 60
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout) == ["FAILED_TIMEOUT"]
    assert leftovers(games) == []
    assert not (games / "Stuck.chd").exists()


def test_22_parallel_jobs(run, games, assets):
    for n, key in enumerate(("iso", "iso2", "iso3"), 1):
        shutil.copy(assets[key], games / ("Par %d.iso" % n))
    shutil.copy(assets["data_bin"], games / "Par CD.bin")
    (games / "Par CD.cue").write_text(single_cue_text("Par CD.bin"))
    res = run("--jobs", "2", cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"] * 4
    log = run.last_log()
    for name in ("Par 1", "Par 2", "Par 3", "Par CD"):
        assert log.count("[OK] %s" % name) == 1
    assert leftovers(games) == []


def test_23_verify_only(run, games, assets):
    shutil.copy(assets["iso"], games / "Valid.iso")
    assert run(cwd=games).returncode == 0
    (games / "Valid.iso").unlink()
    shutil.copy(games / "Valid.chd", games / "Corrupt.chd")
    corrupt_middle(games / "Corrupt.chd", 4096)
    make_partial_chd(assets["slow_iso"], games / "Partial.chd")
    res = run("--verify-only", cwd=games, yes=False)
    assert res.returncode == 1, res.stdout
    found = {rest.split()[0]: status for status, rest in results(res.stdout)}
    assert found == {"Corrupt.chd": "FAILED_VERIFY", "Partial.chd": "FAILED_VERIFY",
                     "Valid.chd": "OK"}


def test_28_no_tty_without_yes(run, games, assets):
    shutil.copy(assets["iso"], games / "Ask.iso")
    (games / "stale.chd.part").write_bytes(b"x")
    before = snapshot(games)
    res = run(cwd=games, yes=False)
    assert res.returncode == 2, res.stdout
    assert "-y" in res.stdout
    assert snapshot(games) == before
    assert not run.log_dir.exists()


@pytest.mark.parametrize("extra", [[], ["--plain"]])
def test_29_plain_output(run, games, assets, extra):
    shutil.copy(assets["iso"], games / "Pipe.iso")
    shutil.copy(assets["data_bin"], games / "Pipe CD.bin")
    (games / "Pipe CD.cue").write_text(single_cue_text("Pipe CD.bin"))
    res = run(*extra, cwd=games)
    assert res.returncode == 0, res.stdout
    assert "\r" not in res.stdout
    for phase in ("  convert", "  verify", "  hash source", "  extract for compare", "  compare"):
        assert phase in res.stdout
    # Own hashing reports every 10% deterministically; chdman's lines depend on its speed.
    assert "hash source 100%" in res.stdout
    assert "\033[" not in res.stdout
    assert "ETA" not in res.stdout


def test_extra_home_root_warning(run, games, assets):
    shutil.copy(assets["iso"], games / "Home.iso")
    res = run("--dry-run", cwd=games, env={"HOME": str(games)})
    assert "WARNING: scanning" in res.stdout


def test_extra_output_and_temp_dirs(run, games, assets, tmp_path):
    shutil.copy(assets["data_bin"], games / "Out.bin")
    (games / "Out.cue").write_text(single_cue_text("Out.bin"))
    out, tmp = tmp_path / "out", tmp_path / "tmp"
    tmp.mkdir()
    res = run("-o", out, "--temp-dir", tmp, cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert (out / "Out.chd").exists()
    assert not (games / "Out.chd").exists()
    assert list(tmp.iterdir()) == [] and leftovers(out) == [] and leftovers(games) == []
