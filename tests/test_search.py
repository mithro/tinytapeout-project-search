# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from ttsearch.build_db import build
from ttsearch.search import build_match, fts_term, search, summarise


def test_fts_term_barewords_pass_through():
    assert fts_term("vga") == "vga"
    assert fts_term("tt_um_x") == "tt_um_x"
    assert fts_term("spi*") == "spi*"


def test_fts_term_quotes_punctuation():
    assert fts_term("risc-v") == '"risc-v"'
    assert fts_term("ps/2") == '"ps/2"'
    assert fts_term('say"hi') == '"say""hi"'
    assert fts_term("sha-*") == '"sha-"*'


def test_build_match_implicit_and():
    assert build_match("vga game", synonyms=False) == "vga AND game"
    assert build_match("  vga   ", synonyms=False) == "vga"
    assert build_match("", synonyms=False) == ""


def test_build_match_synonyms():
    m = build_match("risc-v")
    assert m.startswith("(")
    assert '"risc-v"' in m and "riscv" in m and "rv32" in m
    # A member of the group other than the canonical spelling expands the same way.
    assert build_match("riscv") == build_match("RISC-V".lower())
    # Non-synonym words are left alone.
    assert build_match("vga_out", synonyms=False) == "vga_out"


def test_build_match_no_synonyms_flag():
    assert build_match("i2c", synonyms=False) == "i2c"


SHUTTLES = {"shuttles": [
    {"id": "tt06", "name": "TT6", "pdk": "sky130A", "projects": 2},
    {"id": "tt07", "name": "TT7", "pdk": "sky130A", "projects": 1},
]}
TINY_VGA_PINS = {"uo[0]": "R1", "uo[1]": "G1", "uo[2]": "B1", "uo[3]": "VSync",
                 "uo[4]": "R0", "uo[5]": "G0", "uo[6]": "B0", "uo[7]": "HSync"}
PROJECTS = {"projects": [
    {"shuttle": "tt06", "macro": "a", "address": 1, "title": "VGA pong", "author": "x",
     "description": "A game on a VGA display", "pinout": TINY_VGA_PINS},
    {"shuttle": "tt06", "macro": "b", "address": 2, "title": "I2C thing", "author": "y",
     "description": "IIC peripheral", "pinout": {}},
    {"shuttle": "tt07", "macro": "c", "address": 3, "title": "Group", "type": "group",
     "description": "vga group container", "pinout": {}},
    {"shuttle": "tt07", "macro": "d", "address": 3, "subtile_addr": 1,
     "title": "RISC-V core", "description": "an rv32i cpu", "pinout": {}},
]}


FEEDBACK = {"reports": [
    {"shuttle": "tt06", "macro": "a", "status": "working", "user": "u1", "owner": False,
     "feedback": "Bounces nicely on my monitor", "link": ""},
    {"shuttle": "tt06", "macro": "a", "status": "working", "user": "u2", "owner": True,
     "feedback": "", "link": ""},
    {"shuttle": "tt06", "macro": "b", "status": "broken", "user": "u1", "owner": False,
     "feedback": "No ACK on the bus", "link": ""},
    {"shuttle": "tt07", "macro": "d", "status": "partial", "user": "u3", "owner": False,
     "feedback": "Boots but hangs", "link": ""},
    {"shuttle": "tt07", "macro": "d", "status": "working", "user": "u4", "owner": False,
     "feedback": "", "link": ""},
    {"shuttle": "tt07", "macro": "nope", "status": "working", "user": "u5", "owner": False,
     "feedback": "unmatched project", "link": ""},
]}


AI_TAXONOMY = {"tags": [
    {"tag": "game", "category": "domain", "meaning": "interactive game"},
    {"tag": "vga", "category": "interface", "meaning": "VGA output"},
    {"tag": "i2c", "category": "interface", "meaning": "I2C bus"},
    {"tag": "risc-v", "category": "type", "meaning": "RISC-V processor"},
]}
AI_TAGS = {"projects": {
    "tt06/a": {"tags": ["game", "vga"], "summary": "A pong game on VGA. It bounces. It needs a monitor. It works.",
               "stage": "reviewed", "verdict": "approve", "confidence": "high", "insufficient_docs": False},
    "tt06/b": {"tags": ["i2c", "not-a-real-tag"], "summary": "An I2C thing. One. Two. Three.",
               "stage": "pass2", "verdict": None, "confidence": "medium", "insufficient_docs": False},
    "tt07/d": {"tags": ["risc-v"], "summary": "", "stage": "pass1", "verdict": None,
               "confidence": "low", "insufficient_docs": True},
}}


@pytest.fixture
def con(tmp_path):
    (tmp_path / "p.json").write_text(json.dumps(PROJECTS))
    (tmp_path / "s.json").write_text(json.dumps(SHUTTLES))
    (tmp_path / "f.json").write_text(json.dumps(FEEDBACK))
    (tmp_path / "ai.json").write_text(json.dumps(AI_TAGS))
    (tmp_path / "tax.json").write_text(json.dumps(AI_TAXONOMY))
    c = build(tmp_path / "p.json", tmp_path / "s.json", tmp_path / "t.db", tmp_path / "f.json",
              tmp_path / "ai.json", tmp_path / "tax.json")
    c.row_factory = __import__("sqlite3").Row
    yield c
    c.close()


def test_search_groups_excluded_by_default(con):
    hits = search(con, "vga")
    assert [h.macro for h in hits] == ["a"]
    hits = search(con, "vga", include_groups=True)
    assert sorted(h.macro for h in hits) == ["a", "c"]


def test_search_synonyms_and_shuttle_filter(con):
    assert [h.macro for h in search(con, "i2c")] == ["b"]        # matches "IIC"
    assert [h.macro for h in search(con, "riscv")] == ["d"]      # matches "RISC-V"/"rv32i"
    assert search(con, "riscv", shuttles=["tt06"]) == []
    d = search(con, "riscv")[0]
    assert d.address_str == "3/1"
    assert d.url == "https://tinytapeout.com/chips/tt07/d"


def test_pmod_filter(con):
    import pytest as _pytest
    # Listing by PMOD alone, no keywords.
    hits = search(con, "", pmod="tiny-vga")
    assert [h.macro for h in hits] == ["a"]
    assert hits[0].pmods == ["tiny-vga"]
    assert hits[0].snippet == "A game on a VGA display"   # description stands in for a snippet
    # Combined with keywords: the keyword must also match.
    assert [h.macro for h in search(con, "pong", pmod="tiny-vga")] == ["a"]
    assert search(con, "i2c", pmod="tiny-vga") == []
    # The pmods FTS column is searchable too.
    assert [h.macro for h in search(con, 'pmods:"tiny-vga"', raw=True)] == ["a"]
    with _pytest.raises(ValueError):
        search(con, "", pmod="no-such-pmod")


def test_status_filter_and_tested_sort(con):
    import pytest as _pytest
    # Reports on sub-tile "d" and unknown "nope" do not match a top-level project.
    assert [h.macro for h in search(con, "", status="working")] == ["a"]
    assert [h.macro for h in search(con, "", status="broken")] == ["b"]
    assert [h.macro for h in search(con, "", status="untested")] == ["d"]
    assert [h.macro for h in search(con, "", status="tested")] == ["a", "b"]
    a = search(con, "", status="working")[0]
    assert (a.fb_working, a.fb_partial, a.fb_broken, a.test_status) == (2, 0, 0, "working")
    # Report text is searchable.
    assert [h.macro for h in search(con, "bounces")] == ["a"]
    # Sorting: most working reports first; the listing order otherwise.
    assert [h.macro for h in search(con, "", status="tested", sort="tested")] == ["a", "b"]
    assert [h.macro for h in search(con, "a OR b OR d", raw=True, sort="address")] == ["a", "b", "d"]
    with _pytest.raises(ValueError):
        search(con, "", status="flaky")
    with _pytest.raises(ValueError):
        search(con, "vga", sort="random")


def test_ai_tag_filter(con):
    # Listing by tag alone; unknown tags in the input file are ignored.
    assert [h.macro for h in search(con, "", tags=["game"])] == ["a"]
    assert [h.macro for h in search(con, "", tags=["i2c"])] == ["b"]
    b = search(con, "", tags=["i2c"])[0]
    assert b.ai_tags == ["i2c"]
    assert b.ai_summary.startswith("An I2C thing")
    # Several tags combine with AND.
    assert [h.macro for h in search(con, "", tags=["game", "vga"])] == ["a"]
    assert search(con, "", tags=["game", "i2c"]) == []
    # Keywords plus a tag; AI summaries are searchable.
    assert [h.macro for h in search(con, "pong", tags=["vga"])] == ["a"]
    assert [h.macro for h in search(con, "bounces")] == ["a"]
    # Sub-tile project "d" is keyed tt07/d in the AI file and gets its tag too.
    assert [h.macro for h in search(con, "", tags=["risc-v"])] == ["d"]


def test_summarise_keeps_official_order(con):
    hits = search(con, "vga OR rv32i", raw=True)   # raw: no synonym expansion
    rows = summarise(con, hits)
    assert [(s["id"], n) for s, n in rows] == [("tt06", 1), ("tt07", 1)]
