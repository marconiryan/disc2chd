"""Unit tests on the imported module."""
from __future__ import annotations

import ast

import pytest

from conftest import SCRIPT, load_module

m = load_module()


def test_parses_with_old_python_grammar():
    ast.parse(SCRIPT.read_text(), feature_version=(3, 7))


def test_cue_parser_quoted_unquoted_crlf_bom():
    data = (b"\xef\xbb\xbfFILE \"a b.bin\" BINARY\r\n  TRACK 01 MODE2/2352\r\n    INDEX 01 00:00:00\r\n"
            b"FILE c.bin BINARY\r\n  TRACK 02 AUDIO\r\n    PREGAP 00:02:00\r\n    INDEX 01 00:00:00\r\n")
    sheet = m.parse_cue(data)
    assert sheet.files == ["a b.bin", "c.bin"]
    assert sheet.tracks == 2
    assert sheet.has_gaps
    assert sheet.problem is None


def test_cue_parser_rejects_non_binary():
    sheet = m.parse_cue(b'FILE "x.wav" WAVE\n  TRACK 01 AUDIO\n')
    assert sheet.problem and "WAVE" in sheet.problem


@pytest.mark.parametrize("os_id,like,family", [
    ("ubuntu", ["debian"], "debian"), ("linuxmint", ["ubuntu", "debian"], "debian"),
    ("pop", ["ubuntu", "debian"], "debian"), ("fedora", [], "fedora"),
    ("endeavouros", ["arch"], "arch"), ("opensuse-leap", ["suse", "opensuse"], "opensuse"),
    ("batocera", [], "batocera"), ("haiku", [], "unknown"),
])
def test_os_family(os_id, like, family):
    assert m.os_family(os_id, like) == family


def test_never_passes_bin_to_chdman():
    class A:
        threads = compression = hunk_size = None
    with pytest.raises(AssertionError):
        m.chdman_create_cmd("chdman", m.CD, "/x/game.bin", "/x/game.chd.part", A())
    with pytest.raises(AssertionError):
        m.chdman_create_cmd("chdman", m.DVD, "/x/game.bin", "/x/game.chd.part", A())
    cmd = m.chdman_create_cmd("chdman", m.DVD, "/x/g.ISO", "/x/g.chd.part", A())
    assert cmd[:2] == ["chdman", "createdvd"]


def test_options_are_passed_to_chdman():
    class A:
        threads, compression, hunk_size = 2, "lzma,zlib", 2048
    cmd = m.chdman_create_cmd("chdman", m.CD, "/x/g.cue", "/x/g.chd.part", A())
    assert cmd[-6:] == ["-np", "2", "-c", "lzma,zlib", "-hs", "2048"]


def test_unsafe_archive_members():
    assert m.unsafe_member("/etc/passwd")
    assert m.unsafe_member("a/../../b.iso")
    assert not m.unsafe_member("dir/game..iso")


def test_max_chdman_memory_parsing():
    assert m.parse_size("4G") == 4 << 30
    assert m.parse_size("512M") == 512 << 20
    assert m.parse_size("1.5GiB") == 3 << 29
    for bad in ("", "abc", "4X", "100"):
        with pytest.raises(Exception):
            m.parse_size(bad)
    assert m.default_memory_limit(1) <= 4 << 30
    assert m.default_memory_limit(64) <= m.default_memory_limit(1)
