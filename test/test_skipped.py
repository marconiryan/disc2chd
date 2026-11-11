"""Skipped files, the confirmation question and exit codes."""
from __future__ import annotations

import shutil

from conftest import needs_chdman, results, single_cue_text, snapshot, statuses

pytestmark = needs_chdman

WAVE_CUE = 'FILE "Song.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'


def add_skipped(folder, assets):
    shutil.copy(assets["data_bin"], folder / "Lonely.bin")
    (folder / "Song.wav").write_bytes(b"RIFF")
    (folder / "Music.cue").write_text(WAVE_CUE)


def test_skipped_section_with_reasons(run, games, assets):
    shutil.copy(assets["iso"], games / "Game.iso")
    add_skipped(games, assets)
    res = run("--dry-run", cwd=games)
    assert res.returncode == 0, res.stdout
    skipped = res.stdout.split("Skipped:", 1)[1]
    assert "Lonely.bin  no .cue file references it" in skipped
    assert "Music.cue  unsupported cue (WAVE)" in skipped
    assert statuses(res.stdout) == []


def test_unsupported_cue_is_skipped_not_failed(run, games, assets):
    shutil.copy(assets["iso"], games / "Game.iso")
    (games / "Song.wav").write_bytes(b"RIFF")
    (games / "Music.cue").write_text(WAVE_CUE)
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    summary = res.stdout.split("Summary:", 1)[1]
    assert "SKIPPED_UNSUPPORTED: 1" in summary
    assert "unsupported cue (WAVE)" in summary
    assert not (games / "Music.chd").exists()


def test_confirmation_names_what_it_does(run, games, assets):
    shutil.copy(assets["iso"], games / "One.iso")
    shutil.copy(assets["iso2"], games / "Two.iso")
    add_skipped(games, assets)
    before = snapshot(games)
    rc, out = run.tty(cwd=games, answer="n")
    assert "Convert 2 discs? [y/N]" in out, out
    assert "Proceed?" not in out
    assert rc == 2 and "Nothing done." in out
    assert snapshot(games) == before


def test_confirmation_singular_and_existing(run, games, assets):
    shutil.copy(assets["iso"], games / "One.iso")
    rc, out = run.tty(cwd=games, answer="y")
    assert rc == 0 and "Convert 1 disc? [y/N]" in out, out
    rc, out = run.tty(cwd=games, answer="n")
    assert "Verify 1 existing CHD? [y/N]" in out, out


def test_only_skipped_files_no_prompt(run, games, assets):
    add_skipped(games, assets)
    before = snapshot(games)
    res = run(cwd=games, yes=False)
    assert res.returncode == 0, res.stdout
    assert "[y/N]" not in res.stdout
    assert "Skipped:" in res.stdout and "Nothing to convert." in res.stdout
    assert snapshot(games) == before
    assert not run.log_dir.exists()


def test_exit_0_when_only_skipped_files_remain(run, games, assets):
    shutil.copy(assets["iso"], games / "Good.iso")
    add_skipped(games, assets)
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    summary = res.stdout.split("Summary:", 1)[1]
    assert "SKIPPED_NO_CUE: 1" in summary and "SKIPPED_UNSUPPORTED: 1" in summary
    assert "Problems:" not in summary


def test_exit_1_when_cue_points_to_missing_file(run, games, assets):
    shutil.copy(assets["iso"], games / "Good.iso")
    shutil.copy(assets["data_bin"], games / "Lonely.bin")
    (games / "Lost.cue").write_text(single_cue_text("Lost.bin"))
    res = run(cwd=games)
    assert res.returncode == 1, res.stdout
    assert sorted(statuses(res.stdout)) == ["FAILED_MISSING_FILE", "OK"]
    assert "Lost.bin" in dict(results(res.stdout))["FAILED_MISSING_FILE"]
