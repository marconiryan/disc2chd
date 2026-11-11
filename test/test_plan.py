"""The plan printed by --dry-run and before the confirmation."""
from __future__ import annotations

import re
import shutil

from conftest import needs_7z, needs_chdman, seven_zip, single_cue_text, statuses

pytestmark = needs_chdman


def tiny_iso(path, size=4096):
    path.write_bytes(b"\0" * size)
    return path


def add_cd(folder, name):
    (folder / (name + ".bin")).write_bytes(b"\0" * 2352)
    (folder / (name + ".cue")).write_text(single_cue_text(name + ".bin"))


def disc_lines(output):
    return [l.strip() for l in output.splitlines() if re.match(r"^\s+(DVD|CD )\s{1,2}\S", l)]


@needs_7z
def test_each_disc_once_and_no_commands(run, games, tmp_path):
    tiny_iso(games / "Loose Game.iso")
    add_cd(games, "Loose CD")
    stage = tmp_path / "stage"
    stage.mkdir()
    tiny_iso(stage / "Packed Game.iso")
    seven_zip(games / "Packed Game.7z", stage, "Packed Game.iso")
    res = run("--dry-run", cwd=games)
    assert res.returncode == 0, res.stdout
    for name in ("Loose Game", "Loose CD", "Packed Game"):
        assert res.stdout.count(name) == 1, (name, res.stdout)
    assert disc_lines(res.stdout) == ["CD   Loose CD.cue", "DVD  Loose Game.iso", "DVD  Packed Game.7z"]
    assert "To convert: 3 discs" in res.stdout
    assert "$ " not in res.stdout and "chdman" not in res.stdout and "Source size" not in res.stdout
    assert str(games) not in res.stdout.split("\n", 1)[1]


@needs_7z
def test_commands_with_verbose(run, games, tmp_path):
    tiny_iso(games / "Loose Game.iso")
    stage = tmp_path / "stage"
    stage.mkdir()
    tiny_iso(stage / "Inner.iso")
    seven_zip(games / "Packed.7z", stage, "Inner.iso")
    res = run("--dry-run", "--verbose", cwd=games)
    assert res.returncode == 0, res.stdout
    assert "$ chdman createdvd -i 'Loose Game.iso' -o 'Loose Game.chd.part'" in res.stdout
    assert re.search(r"\$ 7z\w* x -y -bd -o<temp> -- Packed\.7z", res.stdout)
    assert "$ chdman createdvd -i '<temp>/Inner.iso' -o Packed.chd.part" in res.stdout
    assert str(games) not in res.stdout.split("\n", 1)[1]


@needs_7z
def test_multi_disc_archive_shows_members(run, games, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    tiny_iso(stage / "Game (Disc 1).iso")
    tiny_iso(stage / "Game (Disc 2).iso")
    seven_zip(games / "Game.7z", stage, "Game (Disc 1).iso", "Game (Disc 2).iso")
    res = run("--dry-run", cwd=games)
    assert res.returncode == 0, res.stdout
    assert disc_lines(res.stdout) == ["DVD  Game.7z [Game (Disc 1).iso]",
                                      "DVD  Game.7z [Game (Disc 2).iso]"]


def test_subfolder_shows_relative_path(run, games):
    (games / "sub" / "deeper").mkdir(parents=True)
    tiny_iso(games / "sub" / "deeper" / "Deep.iso")
    res = run("--dry-run", cwd=games)
    assert res.returncode == 0, res.stdout
    assert disc_lines(res.stdout) == ["DVD  sub/deeper/Deep.iso"]
    res = run("--dry-run", games, cwd=games.parent)
    assert disc_lines(res.stdout) == ["DVD  sub/deeper/Deep.iso"]


@needs_7z
def test_output_name_shown_when_different(run, games, tmp_path):
    tiny_iso(games / "Game.iso")
    stage = tmp_path / "stage"
    stage.mkdir()
    tiny_iso(stage / "Other name.iso")
    seven_zip(games / "Game.7z", stage, "Other name.iso")
    res = run("--dry-run", cwd=games)
    assert res.returncode == 0, res.stdout
    assert disc_lines(res.stdout) == ["DVD  Game.iso", "DVD  Game.7z -> Other name.chd"]
    after = res.stdout.split("Game.7z -> Other name.chd\n", 1)[1].splitlines()[0]
    assert after.strip().startswith("warning: ")
    assert "named after the archive member: Game.chd would also be written by Game.iso" in after
    assert "Problems: none" in res.stdout


def test_skipped_and_problems_sections(run, games):
    tiny_iso(games / "Good.iso")
    (games / "Orphan (Track 2).bin").write_bytes(b"\0" * 2352)
    (games / "Lost.cue").write_text(single_cue_text("Lost.bin"))
    res = run("--dry-run", cwd=games)
    assert res.returncode == 1, res.stdout
    out = res.stdout
    assert "Skipped: 1\n    Orphan (Track 2).bin  no .cue file references it" in out
    assert "Problems: 1\n    Lost.cue  referenced file not found: Lost.bin" in out
    assert statuses(out) == []


def test_no_skipped_section_and_problems_none(run, games):
    tiny_iso(games / "Good.iso")
    res = run("--dry-run", cwd=games)
    assert res.returncode == 0, res.stdout
    assert "Skipped" not in res.stdout
    assert "Problems: none" in res.stdout
    assert "Free space: " in res.stdout


def test_already_converted_section(run, games, assets):
    shutil.copy(assets["iso"], games / "Done.iso")
    assert run(cwd=games).returncode == 0
    tiny_iso(games / "New.iso")
    res = run("--dry-run", cwd=games)
    assert "To convert: 1 disc," in res.stdout
    assert "Already converted: 1 (each CHD is verified before being skipped)\n    Done.chd" in res.stdout


def test_normal_run_lists_up_to_20_discs(run, games):
    for n in range(20):
        tiny_iso(games / ("Game %02d.iso" % n))
    res = run(cwd=games, yes=False)
    assert res.returncode == 2
    assert "To convert: 20 discs" in res.stdout
    assert len(disc_lines(res.stdout)) == 20


def test_normal_run_with_more_than_20_discs_prints_counts(run, games):
    for n in range(21):
        tiny_iso(games / ("Game %02d.iso" % n))
    res = run(cwd=games, yes=False)
    assert res.returncode == 2
    assert "To convert: 21 discs" in res.stdout
    assert disc_lines(res.stdout) == [] and "Game 00" not in res.stdout
    assert "Problems: none" in res.stdout
    res = run("--verbose", cwd=games, yes=False)
    assert len(disc_lines(res.stdout)) == 21
    res = run("--dry-run", cwd=games)
    assert len(disc_lines(res.stdout)) == 21


def test_plan_colors_follow_tty_rules(run, games):
    tiny_iso(games / "Color.iso")
    rc, out = run.tty("--dry-run", cwd=games)
    assert rc == 0 and re.search(r"\033\[\d+m", out)
    rc, out = run.tty("--dry-run", cwd=games, env={"NO_COLOR": "1"})
    assert rc == 0 and not re.search(r"\033\[\d+m", out)
    assert "\033[" not in run("--dry-run", cwd=games).stdout
