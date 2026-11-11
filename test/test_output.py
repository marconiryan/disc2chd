"""Progress output and the result line."""
from __future__ import annotations

import random
import re
import shutil

from conftest import load_module, needs_chdman, results, single_cue_text, statuses

m = load_module()


def test_savings_wording():
    assert m.savings(1000, 385) == "saved 61%"
    assert m.savings(1000, 999) == "saved 0%"
    assert m.savings(1000, 1000) == "no savings"
    assert m.savings(1000, 1200) == "no savings"


@needs_chdman
def test_no_eta_in_live_output(run, games, assets):
    shutil.copy(assets["slow_iso"], games / "Live.iso")
    rc, out = run.tty("-y", "--threads", "1", cwd=games)
    assert rc == 0, out
    assert "\r" in out, "no live status line"
    assert "discs done" in out
    assert re.search(r"convert\s+\[[#-]{20}\]\s+\d+\.\d%", out)
    assert "ETA" not in out
    assert not re.search(r"\d/s\b", out)


@needs_chdman
def test_no_eta_in_plain_output(run, games, assets):
    shutil.copy(assets["iso"], games / "Plain.iso")
    shutil.copy(assets["data_bin"], games / "Plain CD.bin")
    (games / "Plain CD.cue").write_text(single_cue_text("Plain CD.bin"))
    for extra in ([], ["--plain"]):
        for chd in games.glob("*.chd"):
            chd.unlink()
        res = run(*extra, cwd=games)
        assert res.returncode == 0, res.stdout
        assert "ETA" not in res.stdout
        assert "convert" in res.stdout


@needs_chdman
def test_result_line_and_log_show_space_saved(run, games, assets):
    iso = games / "Saved.iso"
    shutil.copy(assets["iso"], iso)
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    src, chd = iso.stat().st_size, (games / "Saved.chd").stat().st_size
    pct = 100 * (src - chd) // src
    assert 0 < pct < 100
    line = results(res.stdout)[0][1]
    assert "(saved %d%%)" % pct in line, line
    assert not re.search(r"\(\d+%\)", line)
    assert "sizes: %d -> %d bytes, saved %d%%" % (src, chd, pct) in run.last_log()
    assert re.search(r"Size: .* \(saved .*, %d%%\)" % pct, res.stdout)


@needs_chdman
def test_result_line_no_savings(run, games):
    # All-random content does not compress; the CHD ends up slightly larger.
    iso = games / "Noise.iso"
    iso.write_bytes(random.Random(9).randbytes(2048 * 2048))
    res = run(cwd=games)
    assert res.returncode == 0, res.stdout
    assert (games / "Noise.chd").stat().st_size >= iso.stat().st_size
    assert statuses(res.stdout) == ["OK"]
    assert "(no savings)" in results(res.stdout)[0][1]
    assert "bytes, no savings" in run.last_log()
    assert "saved" not in res.stdout
