"""Missing dependencies (tests 26, 27)."""
from __future__ import annotations

import os
import shutil

import pytest

from conftest import CHDMAN, OS_RELEASE, needs_chdman, statuses, write_os_release

FALLBACK_CHDMAN = "/usr/bin/mame/chdman"


@pytest.mark.skipif(os.access(FALLBACK_CHDMAN, os.X_OK),
                    reason="%s exists, so chdman cannot be made missing" % FALLBACK_CHDMAN)
@pytest.mark.parametrize("family", sorted(OS_RELEASE))
def test_26_chdman_missing(run, games, assets, tmp_path, family):
    shutil.copy(assets["iso"], games / "Need.iso")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    osr = write_os_release(tmp_path, family)
    res = run(cwd=games, env={"PATH": str(empty), "DISC2CHD_OS_RELEASE": str(osr)})
    assert res.returncode == 3, res.stdout
    assert OS_RELEASE[family][1] in res.stdout
    assert not (games / "Need.chd").exists()
    check = run("--check", cwd=games, env={"PATH": str(empty), "DISC2CHD_OS_RELEASE": str(osr)})
    assert check.returncode == 3
    assert OS_RELEASE[family][1] in check.stdout
    assert OS_RELEASE[family][2] in check.stdout


@needs_chdman
def test_27_7zip_missing(run, games, assets, tmp_path):
    only_chdman = tmp_path / "bin"
    only_chdman.mkdir()
    os.symlink(CHDMAN, only_chdman / "chdman")
    osr = write_os_release(tmp_path, "fedora")
    env = {"PATH": str(only_chdman), "DISC2CHD_OS_RELEASE": str(osr)}

    shutil.copy(assets["iso"], games / "Loose.iso")
    res = run(cwd=games, env=env)
    assert res.returncode == 0, res.stdout
    assert statuses(res.stdout) == ["OK"]

    check = run("--check", cwd=games, env=env)
    assert check.returncode == 0
    assert "7-Zip    MISSING" in check.stdout

    # Only the extension matters: the tool stops before opening the archive.
    (games / "Packed.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 64)
    res = run(cwd=games, env=env)
    assert res.returncode == 3, res.stdout
    assert "sudo dnf install 7zip" in res.stdout
    assert not (games / "Packed.chd").exists()
