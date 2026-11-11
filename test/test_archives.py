"""Archives (tests 7-10, 25)."""
from __future__ import annotations

import shutil

from conftest import (AWKWARD, corrupt_middle, data_sha1, leftovers, multi_cue_text,
                      seven_zip, sha1_of, single_cue_text, statuses)


def test_07_7z_with_one_iso(run, games, assets, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso"], stage / "inner name.iso")
    seven_zip(games / (AWKWARD + ".7z"), stage, "inner name.iso")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    chd = games / (AWKWARD + ".chd")
    assert data_sha1(chd) == sha1_of(assets["iso"])
    assert not (games / "inner name.chd").exists()
    assert leftovers(games) == []


def test_08_zip_with_cue_and_bins_in_subfolder(run, games, assets, tmp_path):
    stage = tmp_path / "stage"
    (stage / "disc").mkdir(parents=True)
    shutil.copy(assets["data_bin"], stage / "disc" / "Game (Track 1).bin")
    shutil.copy(assets["audio_bin"], stage / "disc" / "Game (Track 2).bin")
    (stage / "disc" / "Game.cue").write_text(multi_cue_text("Game (Track 1).bin", "Game (Track 2).bin"))
    seven_zip(games / "Zipped Game.zip", stage, "disc")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert (games / "Zipped Game.chd").exists()
    assert leftovers(games) == []


def test_09_7z_with_two_discs(run, games, assets, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso"], stage / "Game (Disc 1).iso")
    shutil.copy(assets["data_bin"], stage / "Game (Disc 2).bin")
    (stage / "Game (Disc 2).cue").write_text(single_cue_text("Game (Disc 2).bin"))
    seven_zip(games / "Game.7z", stage, "Game (Disc 1).iso", "Game (Disc 2).cue", "Game (Disc 2).bin")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK", "OK"]
    assert data_sha1(games / "Game (Disc 1).chd") == sha1_of(assets["iso"])
    assert (games / "Game (Disc 2).chd").exists()
    assert run.last_log().count("verify OK") == 2
    assert leftovers(games) == []


def test_10_corrupted_7z(run, games, assets, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso"], stage / "Broken.iso")
    archive = seven_zip(games / "Broken.7z", stage, "Broken.iso")
    corrupt_middle(archive)
    res = run(cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout) == ["FAILED_EXTRACT"]
    assert list(games.glob("*.chd")) == []
    assert leftovers(games) == []


def test_25_iso_and_7z_with_same_name(run, games, assets, tmp_path):
    shutil.copy(assets["iso"], games / "Game.iso")
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso2"], stage / "Other.iso")
    seven_zip(games / "Game.7z", stage, "Other.iso")
    res = run(cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout).count("FAILED_NAME_COLLISION") == 2
    assert not (games / "Game.chd").exists()
    assert sorted(p.name for p in games.iterdir()) == ["Game.7z", "Game.iso"]


def test_extra_rar_without_support_fails_clearly(run, games):
    # Not a real RAR (no RAR tool is needed): 7-Zip cannot open it either way.
    (games / "Packed.rar").write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 4096)
    res = run(cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout) == ["FAILED_EXTRACT"]
    assert "7-Zip" in res.stdout
