"""install.sh and uninstall.sh, each test with a temporary HOME (tests 30, 31)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import BASH, FISH, REPO, ZSH, base_env, needs_fish, needs_zsh

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")

MARK_BEGIN = "# >>> disc2chd >>>"
MARK_END = "# <<< disc2chd <<<"
FOUND = "Installed. Open a new terminal and run: disc2chd"
LOGIN_NOTE = "Note: login shells (SSH sessions, text consoles) do not find disc2chd yet."


@pytest.fixture
def sh(home, tools_path, fake_os_release):
    """Run a script with only the given environment."""
    def run(cmd, shell=BASH, extra=None):
        env = base_env(home, tools_path, fake_os_release)
        if shell:
            env["SHELL"] = shell
        if extra:
            env.update(extra)
        return subprocess.run([str(c) for c in cmd], cwd="/", env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=120)
    return run


def normal_home(home: Path) -> None:
    """Login bash shells read ~/.bashrc, as with the usual distro skeleton files."""
    (home / ".bash_profile").write_text("[ -f ~/.bashrc ] && . ~/.bashrc\n")


def bin_path(home: Path) -> Path:
    return home / ".local" / "bin" / "disc2chd"


def blocks(path: Path) -> int:
    text = path.read_text()
    assert text.count(MARK_BEGIN) == text.count(MARK_END)
    return text.count(MARK_BEGIN)


def install(sh, **kw):
    return sh([REPO / "install.sh"], **kw)


def uninstall(sh, **kw):
    return sh([REPO / "uninstall.sh"], **kw)


# --------------------------------------------------------------------------
# install.sh
# --------------------------------------------------------------------------

def test_30_install_found_by_new_shell(sh, home, tools_path):
    normal_home(home)
    res = install(sh)
    assert res.returncode == 0, res.stdout
    assert bin_path(home).exists()
    assert "Checking dependencies" in res.stdout
    assert blocks(home / ".bashrc") == 1
    assert str(bin_path(home).parent) in (home / ".bashrc").read_text()
    assert FOUND in res.stdout
    assert LOGIN_NOTE not in res.stdout
    assert "full path" not in res.stdout
    # A new login shell runs it through PATH, with no python3 prefix.
    login = subprocess.run([BASH, "-lic", "disc2chd --version"], cwd="/",
                           env={"HOME": str(home), "PATH": tools_path},
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=60)
    assert login.stdout.startswith("disc2chd "), login.stdout


def test_30_install_found_only_by_interactive_shell(sh, home):
    # Login shells read a ~/.bash_profile that neither loads ~/.bashrc nor keeps PATH.
    (home / ".bash_profile").write_text("export PATH=/usr/bin:/bin\n")
    res = install(sh)
    assert res.returncode == 0, res.stdout
    assert FOUND in res.stdout
    assert LOGIN_NOTE in res.stdout
    assert "[ -f ~/.bashrc ] && . ~/.bashrc" in res.stdout
    assert 'export PATH="%s:$PATH"' % bin_path(home).parent in res.stdout


def test_30_install_not_found_prints_manual_line(sh, home):
    # PATH is read-only in every new shell, so the added line cannot take effect.
    (home / ".bashrc").write_text("readonly PATH\n")
    (home / ".bash_profile").write_text(". ~/.bashrc\n")
    res = install(sh)
    assert res.returncode == 0, res.stdout
    assert FOUND not in res.stdout
    assert blocks(home / ".bashrc") == 1
    assert 'export PATH="%s:$PATH"' % bin_path(home).parent in res.stdout
    assert "full path: %s" % bin_path(home) in res.stdout


def test_30_install_twice_upgrades_with_one_block(sh, home):
    normal_home(home)
    assert install(sh).returncode == 0
    bin_path(home).write_text("#!/bin/sh\necho old\n")
    res = install(sh)
    assert res.returncode == 0, res.stdout
    assert "Upgraded" in res.stdout
    assert bin_path(home).read_bytes() == (REPO / "src" / "disc2chd.py").read_bytes()
    assert blocks(home / ".bashrc") == 1


def test_30_rc_without_trailing_newline(sh, home):
    normal_home(home)
    (home / ".bashrc").write_text("alias ll='ls -l'")
    res = install(sh)
    assert res.returncode == 0, res.stdout
    lines = (home / ".bashrc").read_text().splitlines()
    assert lines[0] == "alias ll='ls -l'"
    assert lines[1] == MARK_BEGIN
    check = subprocess.run([BASH, "-n", str(home / ".bashrc")], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
    assert check.returncode == 0, check.stdout


def test_30_rc_with_begin_marker_only_is_untouched(sh, home):
    rc = home / ".bashrc"
    rc.write_bytes(b"x=1\n" + MARK_BEGIN.encode() + b"\nexport FOO=1\n")
    before = rc.read_bytes()
    res = install(sh)
    assert res.returncode != 0
    assert "without a matching" in res.stdout
    assert rc.read_bytes() == before


def test_30_rc_symlink_stays_a_symlink(sh, home):
    normal_home(home)
    target = home / "dotfiles" / "bashrc"
    target.parent.mkdir()
    target.write_text("export A=1\n")
    (home / ".bashrc").symlink_to(target)
    assert install(sh).returncode == 0
    assert (home / ".bashrc").is_symlink()
    assert blocks(target) == 1
    assert uninstall(sh).returncode == 0
    assert (home / ".bashrc").is_symlink()
    assert target.read_text() == "export A=1\n"


@pytest.mark.parametrize("arg", ["--add-to-path", "--help", "x"])
def test_30_install_rejects_arguments(sh, home, arg):
    res = sh([REPO / "install.sh", arg])
    assert res.returncode == 2
    assert res.stdout.count("\n") == 1 and "usage" in res.stdout
    assert not bin_path(home).exists()


def test_30_dir_already_in_path_adds_no_block(sh, home, tools_path):
    res = install(sh, extra={"PATH": "%s:%s" % (bin_path(home).parent, tools_path)})
    assert res.returncode == 0, res.stdout
    assert "already in PATH" in res.stdout
    assert not (home / ".bashrc").exists()


def test_30_existing_zshrc_gets_a_block(sh, home):
    normal_home(home)
    (home / ".zshrc").write_text("setopt nobeep\n")
    assert install(sh).returncode == 0
    assert blocks(home / ".zshrc") == 1
    assert blocks(home / ".bashrc") == 1


def test_30_shell_unset_falls_back_to_bash(sh, home):
    normal_home(home)
    res = install(sh, shell=None)
    assert res.returncode == 0, res.stdout
    assert FOUND in res.stdout


def test_30_other_shell_gets_manual_instruction(sh, home):
    res = install(sh, shell="/nonexistent/tcsh")
    assert res.returncode == 0, res.stdout
    assert "not configured automatically" in res.stdout
    assert 'set path = ("%s" $path)' % bin_path(home).parent in res.stdout


@needs_zsh
def test_30_zsh_user(sh, home):
    res = install(sh, shell=ZSH)
    assert res.returncode == 0, res.stdout
    assert blocks(home / ".zshrc") == 1
    assert FOUND in res.stdout


@needs_fish
def test_30_fish_user(sh, home):
    res = install(sh, shell=FISH)
    assert res.returncode == 0, res.stdout
    assert blocks(home / ".config" / "fish" / "config.fish") == 1
    assert FOUND in res.stdout


# --------------------------------------------------------------------------
# uninstall.sh
# --------------------------------------------------------------------------

def test_31_uninstall_removes_everything(sh, home):
    normal_home(home)
    (home / ".bashrc").write_text("alias ll='ls -l'\n")
    (home / ".zshrc").write_text("setopt nobeep\n")
    assert install(sh).returncode == 0
    logs = home / ".local" / "state" / "disc2chd" / "logs"
    logs.mkdir(parents=True)
    (logs / "disc2chd-20260101-000000.log").write_text("x")
    res = uninstall(sh)
    assert res.returncode == 0, res.stdout
    assert not bin_path(home).exists()
    assert (home / ".bashrc").read_text() == "alias ll='ls -l'\n"
    assert (home / ".zshrc").read_text() == "setopt nobeep\n"
    assert not (home / ".local" / "state" / "disc2chd").exists()
    assert "Removed %s" % bin_path(home) in res.stdout
    assert res.stdout.count("Removed the PATH block") == 2
    assert "Removed logs" in res.stdout


def test_31_uninstall_keeps_rc_with_begin_marker_only(sh, home):
    normal_home(home)
    assert install(sh).returncode == 0
    zshrc = home / ".zshrc"
    zshrc.write_bytes(MARK_BEGIN.encode() + b"\nexport FOO=1\n")
    before = zshrc.read_bytes()
    (home / ".local" / "state" / "disc2chd").mkdir(parents=True)
    res = uninstall(sh)
    assert res.returncode != 0
    assert "Warning" in res.stdout and str(zshrc) in res.stdout
    assert zshrc.read_bytes() == before
    assert not bin_path(home).exists()
    assert blocks(home / ".bashrc") == 0
    assert not (home / ".local" / "state" / "disc2chd").exists()


def test_31_uninstall_on_clean_home(sh, home):
    res = uninstall(sh)
    assert res.returncode == 0, res.stdout
    assert "nothing to remove" in res.stdout
    assert os.listdir(home) == []


def test_31_uninstall_rejects_arguments(sh, home):
    res = sh([REPO / "uninstall.sh", "--purge"])
    assert res.returncode == 2
