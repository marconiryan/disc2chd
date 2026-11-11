"""Archives (tests 7-10, 25)."""
from __future__ import annotations

import os
import shutil

from conftest import (AWKWARD, SEVENZIP_RAR, corrupt_middle, data_sha1, leftovers, make_rar4,
                      multi_cue_text, needs_7z, needs_chdman, seven_zip, sha1_of,
                      single_cue_text, statuses)

pytestmark = [needs_chdman, needs_7z]


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
    # The archive member's own name is used when the archive name is taken.
    shutil.copy(assets["iso"], games / "Game.iso")
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso2"], stage / "Other.iso")
    seven_zip(games / "Game.7z", stage, "Other.iso")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK", "OK"]
    assert data_sha1(games / "Game.chd") == sha1_of(games / "Game.iso")
    assert data_sha1(games / "Other.chd") == sha1_of(assets["iso2"])
    assert leftovers(games) == []


def test_25_name_collision_without_way_out(run, games, assets, tmp_path):
    shutil.copy(assets["iso"], games / "Game.iso")
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso2"], stage / "Game.iso")
    seven_zip(games / "Game.7z", stage, "Game.iso")
    res = run(cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout).count("FAILED_NAME_COLLISION") == 2
    assert not (games / "Game.chd").exists()
    assert sorted(p.name for p in games.iterdir()) == ["Game.7z", "Game.iso"]


def test_extra_rar(run, games, assets):
    # The expectation follows what the local 7-Zip can do, detected at runtime.
    make_rar4(games / "Packed.rar", [("folder/inner.iso", assets["iso"])])
    res = run(cwd=games)
    if SEVENZIP_RAR:
        assert res.returncode == 0, res.stdout
        assert statuses(res.stdout) == ["OK"]
        assert data_sha1(games / "Packed.chd") == sha1_of(assets["iso"])
        assert leftovers(games) == []
    else:
        assert res.returncode == 1, res.stdout
        assert statuses(res.stdout) == ["FAILED_EXTRACT"]
        assert "cannot open .rar archives" in res.stdout
        assert not (games / "Packed.chd").exists()


def test_25_renamed_outputs_are_stable_across_runs(run, games, assets, tmp_path):
    # A loose ISO and two single-disc archives that all want Game.chd.
    shutil.copy(assets["iso"], games / "Game.iso")
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso2"], stage / "Second.iso")
    shutil.copy(assets["iso3"], stage / "Third.iso")
    seven_zip(games / "Game.7z", stage, "Second.iso")
    seven_zip(games / "Game.zip", stage, "Third.iso")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"] * 3
    assert data_sha1(games / "Game.chd") == sha1_of(games / "Game.iso")
    assert data_sha1(games / "Second.chd") == sha1_of(assets["iso2"])
    assert data_sha1(games / "Third.chd") == sha1_of(assets["iso3"])
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["SKIPPED_EXISTS"] * 3
    assert sorted(p.name for p in games.glob("*.chd*")) == ["Game.chd", "Second.chd", "Third.chd"]


def test_25_renaming_ignores_scan_order(games, assets, tmp_path):
    from conftest import SEVENZIP, load_module
    m = load_module()
    shutil.copy(assets["iso"], games / "Game.iso")
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copy(assets["iso2"], stage / "Second.iso")
    shutil.copy(assets["iso3"], stage / "Same.iso")
    seven_zip(games / "Game.7z", stage, "Second.iso")
    seven_zip(games / "Game.zip", stage, "Same.iso")
    seven_zip(games / "Same.7z", stage, "Second.iso")
    (games / "Game.chd").write_bytes(b"x")           # files on disk must not matter

    class Args:
        output_dir = None
    deps = m.Deps(m.Tool("chdman", "chdman"), m.Tool("7-Zip", SEVENZIP), True)
    names = sorted(p.name for p in games.iterdir() if p.suffix != ".chd")
    outcomes = []
    for order in (names, names[::-1]):
        files = {str(games / n): str(games) for n in order}
        plan = m.build_plan(Args(), deps, files, len(files), [str(games)])
        outcomes.append(sorted((os.path.basename(d.archive or d.image), os.path.basename(d.final),
                                d.status) for d in plan.discs))
    assert outcomes[0] == outcomes[1]
    # Game.zip falls back to Same.chd, which Same.7z already uses: no way out.
    assert outcomes[0] == [("Game.7z", "Second.chd", None),
                           ("Game.iso", "Game.chd", "FAILED_NAME_COLLISION"),
                           ("Game.zip", "Game.chd", "FAILED_NAME_COLLISION"),
                           ("Same.7z", "Same.chd", None)]
