"""install.sh and uninstall.sh with a temporary HOME (tests 30, 31)."""
from __future__ import annotations

import subprocess

from conftest import REPO

MARK_BEGIN = "# >>> disc2chd >>>"
MARK_END = "# <<< disc2chd <<<"


def sh(cmd, home, path="/usr/bin:/bin", cwd="/"):
    env = {"HOME": str(home), "PATH": path, "SHELL": "/bin/bash", "LANG": "C.UTF-8"}
    return subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=120)


def test_30_install(home):
    bin_dir = home / ".local" / "bin"
    res = sh([str(REPO / "install.sh")], home)
    assert res.returncode == 0, res.stdout
    assert (bin_dir / "disc2chd").exists()
    assert "is not in your PATH" in res.stdout
    assert 'export PATH="%s:$PATH"' % bin_dir in res.stdout
    assert "fish" in res.stdout
    assert not (home / ".bashrc").exists()

    # Runs through PATH, with no python3 prefix, from any folder.
    run = sh(["disc2chd", "--version"], home, path="%s:/usr/bin:/bin" % bin_dir)
    assert run.returncode == 0 and run.stdout.startswith("disc2chd "), run.stdout

    res = sh([str(REPO / "install.sh"), "--add-to-path"], home)
    assert res.returncode == 0, res.stdout
    assert "Upgraded" in res.stdout
    rc = (home / ".bashrc").read_text()
    assert rc.count(MARK_BEGIN) == 1 and rc.count(MARK_END) == 1
    assert str(bin_dir) in rc

    # Running again upgrades in place and keeps a single block.
    (bin_dir / "disc2chd").write_text("#!/bin/sh\necho old\n")
    res = sh([str(REPO / "install.sh"), "--add-to-path"], home)
    assert res.returncode == 0, res.stdout
    assert (bin_dir / "disc2chd").read_bytes() == (REPO / "src" / "disc2chd.py").read_bytes()
    assert (home / ".bashrc").read_text().count(MARK_BEGIN) == 1

    login = subprocess.run(["bash", "-ic", "disc2chd --version"], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60)
    assert login.stdout.startswith("disc2chd ")


def test_30_install_with_dir_in_path(home):
    bin_dir = home / ".local" / "bin"
    res = sh([str(REPO / "install.sh")], home, path="%s:/usr/bin:/bin" % bin_dir)
    assert res.returncode == 0, res.stdout
    assert "is not in your PATH" not in res.stdout


def test_31_uninstall(home):
    (home / ".bashrc").write_text("alias ll='ls -l'\n")
    assert sh([str(REPO / "install.sh"), "--add-to-path"], home).returncode == 0
    logs = home / ".local" / "state" / "disc2chd" / "logs"
    logs.mkdir(parents=True)
    (logs / "disc2chd-20260101-000000.log").write_text("x")

    res = sh([str(REPO / "uninstall.sh")], home)
    assert res.returncode == 0, res.stdout
    assert not (home / ".local" / "bin" / "disc2chd").exists()
    assert (home / ".bashrc").read_text() == "alias ll='ls -l'\n"
    assert logs.exists()
    assert "Removed" in res.stdout and "Kept logs" in res.stdout

    res = sh([str(REPO / "uninstall.sh"), "--purge"], home)
    assert res.returncode == 0, res.stdout
    assert not (home / ".local" / "state" / "disc2chd").exists()
    assert "Removed logs" in res.stdout
