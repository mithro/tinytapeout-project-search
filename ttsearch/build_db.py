# SPDX-License-Identifier: Apache-2.0
"""Load data/projects.json and data/shuttles.json into a SQLite database.

The database has three ordinary tables (shuttles, projects, pins) and one FTS5
full-text index (projects_fts) over the searchable text of each project. The
FTS table is an "external content" table backed by projects, so snippet() and
highlight() work and the text is stored only once.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import DB_PATH, PROJECTS_JSON, SHUTTLES_JSON
from .pmods import PMODS, detect_pmods

SCHEMA = """
CREATE TABLE shuttles (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    pdk            TEXT,
    repo           TEXT,
    tiles          INTEGER,
    project_count  INTEGER,
    experimental   INTEGER NOT NULL DEFAULT 0,
    index_updated  TEXT,
    index_commit   TEXT,
    sort_order     INTEGER NOT NULL
);

CREATE TABLE projects (
    id             INTEGER PRIMARY KEY,
    shuttle        TEXT NOT NULL REFERENCES shuttles(id),
    macro          TEXT NOT NULL,
    address        INTEGER,
    subtile_addr   INTEGER,
    type           TEXT,
    title          TEXT,
    author         TEXT,
    description    TEXT,
    language       TEXT,
    clock_hz       INTEGER,
    tiles          TEXT,
    repo           TEXT,
    commit_hash    TEXT,
    doc_link       TEXT,
    danger_level   TEXT,
    danger_reason  TEXT,
    how_it_works   TEXT,
    how_to_test    TEXT,
    external_hw    TEXT,
    docs_md        TEXT,
    tags           TEXT,      -- comma separated, also in the tags table
    pin_names      TEXT,      -- space separated, also in the pins table
    pmods          TEXT,      -- space separated ids, also in project_pmods (inferred)
    analog_pin_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (shuttle, macro, subtile_addr)
);
CREATE INDEX projects_shuttle ON projects(shuttle);
CREATE INDEX projects_macro ON projects(macro);

CREATE TABLE pins (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    pin         TEXT NOT NULL,   -- e.g. ui[3], uo[0], uio[7], ua[0]
    name        TEXT NOT NULL
);
CREATE INDEX pins_project ON pins(project_id);

CREATE TABLE tags (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    tag         TEXT NOT NULL
);
CREATE INDEX tags_tag ON tags(tag);

-- Recommended pinouts (PMODs) a project's pin names line up with. Inferred by
-- ttsearch.pmods from the declared pinout, never declared by the project.
CREATE TABLE pmods (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    pins        TEXT NOT NULL,
    url         TEXT NOT NULL,
    sort_order  INTEGER NOT NULL
);
CREATE TABLE project_pmods (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    pmod        TEXT NOT NULL REFERENCES pmods(id)
);
CREATE INDEX project_pmods_pmod ON project_pmods(pmod);
CREATE INDEX project_pmods_project ON project_pmods(project_id);

CREATE VIRTUAL TABLE projects_fts USING fts5(
    title, description, how_it_works, how_to_test, external_hw,
    tags, pin_names, macro, author, pmods,
    content='projects', content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep the FTS index in sync with the content table.
CREATE TRIGGER projects_ai AFTER INSERT ON projects BEGIN
  INSERT INTO projects_fts(rowid, title, description, how_it_works, how_to_test,
                           external_hw, tags, pin_names, macro, author, pmods)
  VALUES (new.id, new.title, new.description, new.how_it_works, new.how_to_test,
          new.external_hw, new.tags, new.pin_names, new.macro, new.author, new.pmods);
END;
CREATE TRIGGER projects_ad AFTER DELETE ON projects BEGIN
  INSERT INTO projects_fts(projects_fts, rowid, title, description, how_it_works,
                           how_to_test, external_hw, tags, pin_names, macro, author, pmods)
  VALUES ('delete', old.id, old.title, old.description, old.how_it_works,
          old.how_to_test, old.external_hw, old.tags, old.pin_names, old.macro,
          old.author, old.pmods);
END;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def as_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def build(projects_json: Path, shuttles_json: Path, db_path: Path) -> sqlite3.Connection:
    projects_doc = json.loads(projects_json.read_text())
    shuttles_doc = json.loads(shuttles_json.read_text())

    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    for order, s in enumerate(shuttles_doc["shuttles"]):
        con.execute(
            "INSERT INTO shuttles VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                s["id"], s["name"], s.get("pdk"), s.get("repo"), as_int(s.get("tiles")),
                as_int(s.get("projects")), 1 if s.get("experimental") else 0,
                s.get("index_updated"), s.get("index_commit"), order,
            ),
        )

    for order, pm in enumerate(PMODS):
        con.execute("INSERT INTO pmods VALUES (?,?,?,?,?)", (pm.id, pm.name, pm.pins, pm.url, order))

    for p in projects_doc["projects"]:
        pinout = p.get("pinout") or {}
        pins = [(k, v.strip()) for k, v in pinout.items() if isinstance(v, str) and v.strip()]
        tags = p.get("tags") or []
        pmod_ids = detect_pmods(pinout) if p.get("type") != "group" else []
        cur = con.execute(
            """INSERT INTO projects (shuttle, macro, address, subtile_addr, type, title,
               author, description, language, clock_hz, tiles, repo, commit_hash,
               doc_link, danger_level, danger_reason, how_it_works, how_to_test,
               external_hw, docs_md, tags, pin_names, pmods, analog_pin_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["shuttle"], p["macro"], as_int(p.get("address")),
                as_int(p.get("subtile_addr")), p.get("type"), p.get("title"),
                p.get("author"), p.get("description"), p.get("language"),
                as_int(p.get("clock_hz")), p.get("tiles"), p.get("repo"),
                p.get("commit"), p.get("doc_link"), p.get("danger_level"),
                p.get("danger_reason"), p.get("how_it_works"), p.get("how_to_test"),
                p.get("external_hw"), p.get("docs_md"),
                ", ".join(tags) if tags else None,
                " ".join(name for _, name in pins) if pins else None,
                " ".join(pmod_ids) if pmod_ids else None,
                len(p.get("analog_pins") or []),
            ),
        )
        pid = cur.lastrowid
        con.executemany("INSERT INTO pins VALUES (?,?,?)", [(pid, k, v) for k, v in pins])
        con.executemany("INSERT INTO tags VALUES (?,?)", [(pid, t) for t in tags])
        con.executemany("INSERT INTO project_pmods VALUES (?,?)", [(pid, m) for m in pmod_ids])

    con.execute("INSERT INTO meta VALUES ('source', ?)", (projects_doc.get("source"),))
    con.execute("INSERT INTO meta VALUES ('index_updated', ?)",
                (shuttles_doc.get("index_updated"),))
    con.execute("INSERT INTO projects_fts(projects_fts) VALUES ('optimize')")
    con.commit()
    return con


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--projects", type=Path, default=PROJECTS_JSON)
    ap.add_argument("--shuttles", type=Path, default=SHUTTLES_JSON)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args(argv)

    con = build(args.projects, args.shuttles, args.db)
    n_shuttles = con.execute("SELECT count(*) FROM shuttles").fetchone()[0]
    n_projects = con.execute("SELECT count(*) FROM projects").fetchone()[0]
    n_pins = con.execute("SELECT count(*) FROM pins").fetchone()[0]
    n_pmods = con.execute("SELECT count(DISTINCT project_id) FROM project_pmods").fetchone()[0]
    con.close()
    print(f"{args.db}: {n_shuttles} shuttles, {n_projects} projects, {n_pins} named pins, "
          f"{n_pmods} projects with an inferred PMOD pinout", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
