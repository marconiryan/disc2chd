"""Optional probes of raw chdman behavior (not of disc2chd).

They record what chdman 0.264 and 0.289 do, which disc2chd's design guards
against. They are skipped unless DISC2CHD_PROBES=1, because other chdman
versions may behave differently without disc2chd being wrong.
"""
from __future__ import annotations

import shutil
import subprocess

from conftest import CHDMAN, make_partial_chd, needs_chdman, probes, sha1_of, single_cue_text

pytestmark = [probes, needs_chdman]


def chdman(*args, timeout=60):
    return subprocess.run([CHDMAN] + [str(a) for a in args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


def test_probe_createcd_on_bin_hangs(tmp_path, assets):
    proc = subprocess.Popen([CHDMAN, "createcd", "-i", str(assets["data_bin"]),
                             "-o", str(tmp_path / "x.chd")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=10)
        hung = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        hung = True
    assert hung and b"-nan%" in out


def test_probe_createdvd_accepts_raw_bin(tmp_path):
    raw = tmp_path / "raw.bin"
    raw.write_bytes(b"\x5a" * 2352 * 128)          # also a multiple of 2048
    assert chdman("createdvd", "-i", raw, "-o", tmp_path / "x.chd").returncode == 0


def test_probe_partial_chd_passes_verify(tmp_path, assets):
    part = make_partial_chd(assets["slow_iso"], tmp_path / "p.chd")
    res = chdman("verify", "-i", part)
    assert res.returncode == 0
    assert b"No verification to be done" in res.stdout
    assert b"0" * 40 in chdman("info", "-i", part).stdout


def test_probe_cue_with_bom_rejected(tmp_path, assets):
    shutil.copy(assets["data_bin"], tmp_path / "d.bin")
    (tmp_path / "bom.cue").write_bytes(b"\xef\xbb\xbf" + single_cue_text("d.bin").encode())
    assert chdman("createcd", "-i", tmp_path / "bom.cue", "-o", tmp_path / "b.chd").returncode != 0


def test_probe_cue_with_crlf_accepted(tmp_path, assets):
    shutil.copy(assets["data_bin"], tmp_path / "d.bin")
    (tmp_path / "crlf.cue").write_bytes(single_cue_text("d.bin").replace("\n", "\r\n").encode())
    assert chdman("createcd", "-i", tmp_path / "crlf.cue", "-o", tmp_path / "c.chd").returncode == 0


def test_probe_letter_case_mismatch_rejected(tmp_path, assets):
    shutil.copy(assets["data_bin"], tmp_path / "d.bin")
    (tmp_path / "case.cue").write_text(single_cue_text("D.BIN"))
    assert chdman("createcd", "-i", tmp_path / "case.cue", "-o", tmp_path / "c.chd").returncode != 0


def test_probe_absolute_file_path_rejected_relative_accepted(tmp_path, assets):
    # chdman joins the cue's folder with the FILE value, even an absolute one.
    (tmp_path / "cues").mkdir()
    cue = tmp_path / "cues" / "abs.cue"
    cue.write_text(single_cue_text(str(assets["data_bin"])))
    assert chdman("createcd", "-i", cue, "-o", tmp_path / "abs.chd").returncode != 0
    shutil.copy(assets["data_bin"], tmp_path / "data.bin")
    rel = tmp_path / "cues" / "rel.cue"
    rel.write_text(single_cue_text("../data.bin"))
    assert chdman("createcd", "-i", rel, "-o", tmp_path / "rel.chd").returncode == 0


def test_probe_dvd_data_sha1_equals_iso(tmp_path, assets):
    chd = tmp_path / "a.chd"
    assert chdman("createdvd", "-i", assets["iso"], "-o", chd).returncode == 0
    assert sha1_of(assets["iso"]).encode() in chdman("info", "-i", chd).stdout
