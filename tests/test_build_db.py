# SPDX-License-Identifier: Apache-2.0
import json

import pytest

from ttsearch.build_db import build

SHUTTLES = {
    "index_updated": "2026-09-09T00:00:00Z",
    "shuttles": [
        {"id": "tt06", "name": "Tiny Tapeout 6", "pdk": "sky130A", "projects": 2,
         "tiles": 512, "repo": "https://example.invalid/tt06"},
        {"id": "ttihp0p2", "name": "IHP 0.2", "pdk": "ihp-sg13g2", "projects": 1,
         "experimental": True},
    ],
}

PROJECTS = {
    "source": "test",
    "projects": [
        {"shuttle": "tt06", "macro": "tt_um_vga", "address": 10, "title": "VGA demo",
         "author": "A", "description": "Draws a bouncing ball on a VGA monitor",
         "pinout": {"uo[0]": "R1", "uo[1]": "", "uo[2]": "hsync"},
         "tags": ["video", "vga"], "how_it_works": "Counts pixels.",
         "clock_hz": "25000000", "analog_pins": []},
        {"shuttle": "tt06", "macro": "tt_um_i2c", "address": 12, "title": "I2C controller",
         "author": "B", "description": "An I2C master", "pinout": {},
         "clock_hz": None, "analog_pins": ["ua[0]"]},
        {"shuttle": "ttihp0p2", "macro": "tt_um_vga2", "address": 3,
         "title": "Another VGA thing", "author": "C", "description": "", "pinout": {}},
    ],
}


@pytest.fixture
def db(tmp_path):
    pj = tmp_path / "projects.json"
    sj = tmp_path / "shuttles.json"
    pj.write_text(json.dumps(PROJECTS))
    sj.write_text(json.dumps(SHUTTLES))
    con = build(pj, sj, tmp_path / "t.db")
    yield con
    con.close()


def test_counts(db):
    assert db.execute("SELECT count(*) FROM shuttles").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 3
    # Empty pin names are dropped.
    assert db.execute("SELECT count(*) FROM pins").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM tags").fetchone()[0] == 2


def test_types_and_flags(db):
    row = db.execute(
        "SELECT clock_hz, analog_pin_count, pin_names, tags FROM projects WHERE macro='tt_um_vga'"
    ).fetchone()
    assert row == (25_000_000, 0, "R1 hsync", "video, vga")
    assert db.execute("SELECT analog_pin_count FROM projects WHERE macro='tt_um_i2c'").fetchone()[0] == 1
    assert db.execute("SELECT experimental FROM shuttles WHERE id='ttihp0p2'").fetchone()[0] == 1
    assert db.execute("SELECT experimental FROM shuttles WHERE id='tt06'").fetchone()[0] == 0


def test_fts_search(db):
    rows = db.execute(
        "SELECT p.shuttle, p.macro FROM projects_fts f JOIN projects p ON p.id = f.rowid "
        "WHERE projects_fts MATCH 'vga' ORDER BY p.shuttle, p.macro"
    ).fetchall()
    assert rows == [("tt06", "tt_um_vga"), ("ttihp0p2", "tt_um_vga2")]
    # Pin names are searchable.
    rows = db.execute(
        "SELECT p.macro FROM projects_fts f JOIN projects p ON p.id = f.rowid "
        "WHERE projects_fts MATCH 'hsync'"
    ).fetchall()
    assert rows == [("tt_um_vga",)]
    # Porter stemming: 'controllers' finds 'controller'.
    rows = db.execute(
        "SELECT p.macro FROM projects_fts f JOIN projects p ON p.id = f.rowid "
        "WHERE projects_fts MATCH 'controllers'"
    ).fetchall()
    assert rows == [("tt_um_i2c",)]
