"""Conversion of loose images (tests 1-6, 11-16, 24)."""
from __future__ import annotations

import shutil
import subprocess

from conftest import (AWKWARD, CHDMAN, chd_info, data_sha1, leftovers, make_partial_chd,
                      multi_cue_text, results, sha1_of, single_cue_text, statuses)


def add_single_cd(folder, assets, name="Single"):
    shutil.copy(assets["data_bin"], folder / (name + ".bin"))
    (folder / (name + ".cue")).write_text(single_cue_text(name + ".bin"))
    return folder / (name + ".cue")


def add_multi_cd(folder, assets, name="Multi"):
    shutil.copy(assets["data_bin"], folder / (name + " (Track 1).bin"))
    shutil.copy(assets["audio_bin"], folder / (name + " (Track 2).bin"))
    (folder / (name + ".cue")).write_text(
        multi_cue_text(name + " (Track 1).bin", name + " (Track 2).bin"))
    return folder / (name + ".cue")


def test_01_loose_iso(run, games, assets):
    iso = games / (AWKWARD + ".iso")
    shutil.copy(assets["iso"], iso)
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    chd = games / (AWKWARD + ".chd")
    info = chd_info(chd)
    assert "Tag='DVD '" in info
    assert data_sha1(chd) == sha1_of(iso)
    assert leftovers(games) == []


def test_02_single_track_cue(run, games, assets, tmp_path):
    add_single_cd(games, assets, AWKWARD)
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    out = tmp_path / "x"
    out.mkdir()
    subprocess.run([CHDMAN, "extractcd", "-i", str(games / (AWKWARD + ".chd")),
                    "-o", str(out / "x.cue"), "-ob", str(out / "x.bin")],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert sha1_of(out / "x.bin") == sha1_of(games / (AWKWARD + ".bin"))
    assert leftovers(games) == []


def test_03_multi_track_cue(run, games, assets):
    add_multi_cd(games, assets)
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    info = chd_info(games / "Multi.chd")
    assert "Tag='CHT2'" in info
    assert info.count("TRACK:") == 2


def test_04_iso_and_cue_in_same_folder(run, games, assets):
    shutil.copy(assets["iso"], games / "Game One.iso")
    add_single_cd(games, assets, "Game Two")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert sorted(statuses(res.stdout)) == ["OK", "OK"]
    assert (games / "Game One.chd").exists() and (games / "Game Two.chd").exists()


def test_05_subfolders(run, games, assets):
    sub = games / "a" / "b"
    sub.mkdir(parents=True)
    shutil.copy(assets["iso"], sub / "Deep.iso")
    res = run("--no-recursive", cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == []
    assert not (sub / "Deep.chd").exists()
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert (sub / "Deep.chd").exists()


def test_06_no_path_scans_current_directory(run, games, assets, tmp_path):
    shutil.copy(assets["iso"], games / "Here.iso")
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(assets["iso2"], other / "Elsewhere.iso")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert (games / "Here.chd").exists()
    assert not (other / "Elsewhere.chd").exists()


def test_11_truncated_iso(run, games, assets):
    data = assets["iso"].read_bytes()
    (games / "Short.iso").write_bytes(data[:-1000])
    res = run(cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout) == ["FAILED_CHDMAN"]
    assert "not divisible" in res.stdout
    assert not (games / "Short.chd").exists()
    assert leftovers(games) == []


def test_13_cue_with_missing_bin(run, games, assets):
    (games / "Lost.cue").write_text(single_cue_text("Lost.bin"))
    res = run(cwd=games)
    assert res.returncode == 1, res.stdout
    assert statuses(res.stdout) == ["FAILED_MISSING_FILE"]
    assert not (games / "Lost.chd").exists()


def test_13_cue_with_letter_case_mismatch(run, games, assets):
    # chdman itself fails here (Linux paths are case-sensitive). disc2chd
    # converts through a corrected copy of the cue in its temp folder and
    # never modifies the user's files.
    shutil.copy(assets["data_bin"], games / "case.bin")
    cue = games / "Case.cue"
    cue.write_text(single_cue_text("CASE.BIN"))
    before = cue.read_bytes()
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert "letter case" in res.stdout
    assert cue.read_bytes() == before
    assert sorted(p.name for p in games.iterdir()) == ["Case.chd", "Case.cue", "case.bin"]


def test_13_chdman_rejects_absolute_file_path(tmp_path, assets):
    # Documents why the corrected cue uses relative paths: chdman joins the
    # cue's folder with the FILE value, even an absolute one.
    (tmp_path / "cues").mkdir()
    cue = tmp_path / "cues" / "abs.cue"
    cue.write_text(single_cue_text(str(assets["data_bin"])))
    res = subprocess.run([CHDMAN, "createcd", "-i", str(cue), "-o", str(tmp_path / "abs.chd")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    assert res.returncode != 0
    rel = tmp_path / "cues" / "rel.cue"
    shutil.copy(assets["data_bin"], tmp_path / "data.bin")
    rel.write_text(single_cue_text("../data.bin"))
    res = subprocess.run([CHDMAN, "createcd", "-i", str(rel), "-o", str(tmp_path / "rel.chd")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    assert res.returncode == 0


def test_14_cue_with_crlf_and_bom(run, games, assets):
    shutil.copy(assets["data_bin"], games / "Dos.bin")
    text = single_cue_text("Dos.bin").replace("\n", "\r\n")
    (games / "Dos.cue").write_bytes(b"\xef\xbb\xbf" + text.encode())
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]


def test_15_valid_chd_already_present(run, games, assets):
    shutil.copy(assets["iso"], games / "Done.iso")
    assert run(cwd=games).returncode == 0
    chd = games / "Done.chd"
    mtime = chd.stat().st_mtime_ns
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["SKIPPED_EXISTS"]
    assert chd.stat().st_mtime_ns == mtime
    log = run.last_log()
    assert "Checking existing" in log and "verify OK" in log and "[SKIPPED_EXISTS]" in log


def test_16_partial_chd_with_final_name(run, games, assets):
    shutil.copy(assets["iso"], games / "Half.iso")
    make_partial_chd(assets["slow_iso"], games / "Half.chd")
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert "existing CHD is invalid" in res.stdout
    invalid = list(games.glob("Half.chd.invalid-*"))
    assert len(invalid) == 1
    assert data_sha1(games / "Half.chd") == sha1_of(games / "Half.iso")


def test_24_exit_codes(run, games, assets, tmp_path):
    shutil.copy(assets["iso"], games / "Good.iso")
    assert run(cwd=games).returncode == 0
    bad = tmp_path / "bad"
    bad.mkdir()
    shutil.copy(assets["iso"], bad / "Good.iso")
    (bad / "Bad.iso").write_bytes(assets["iso"].read_bytes()[:-1])
    res = run(cwd=bad)
    assert res.returncode == 1
    assert sorted(statuses(res.stdout)) == ["FAILED_CHDMAN", "OK"]
    assert dict((s, r) for s, r in results(res.stdout))["FAILED_CHDMAN"].startswith("Bad.iso")


def test_extra_pregap_postgap_cue(run, games, assets):
    # PREGAP/POSTGAP data is in no file. chdman stores the gaps as track
    # metadata and extractcd writes them back as directives, not as data, so
    # the extracted .bin still equals the source bins: the result is OK, not
    # OK_INTERNAL_ONLY.
    shutil.copy(assets["data_bin"], games / "Gap (Track 1).bin")
    shutil.copy(assets["audio_bin"], games / "Gap (Track 2).bin")
    (games / "Gap.cue").write_text(
        'FILE "Gap (Track 1).bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
        'FILE "Gap (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n    PREGAP 00:02:00\n'
        '    INDEX 01 00:00:00\n    POSTGAP 00:01:00\n')
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]
    assert chd_info(games / "Gap.chd").count("TRACK:") == 2
