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
from .feedback import FEEDBACK_JSON, STATUSES
from .pmods import PMODS, detect_pmods

AI_TAGS_JSON = Path(__file__).resolve().parent.parent / "data" / "ai" / "tags.json"
AI_TAXONOMY_JSON = Path(__file__).resolve().parent.parent / "data" / "ai" / "taxonomy.json"

# Derived from the silicon reports for a project:
#   untested  no reports
#   working   at least one "working" report and no "broken" ones
#   broken    only "broken" reports
#   partial   "partial" reports, or a mix of working and broken
TEST_STATUSES = ("working", "partial", "broken", "untested")


def test_status(working: int, partial: int, broken: int) -> str:
    if working == 0 and partial == 0 and broken == 0:
        return "untested"
    if broken > 0 and working == 0 and partial == 0:
        return "broken"
    if working > 0 and broken == 0:
        return "working"
    return "partial"

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
    fb_working     INTEGER NOT NULL DEFAULT 0,   -- silicon reports, see feedback table
    fb_partial     INTEGER NOT NULL DEFAULT 0,
    fb_broken      INTEGER NOT NULL DEFAULT 0,
    test_status    TEXT NOT NULL DEFAULT 'untested',
    feedback_text  TEXT,      -- all report texts joined, for full-text search
    ai_summary     TEXT,      -- model-written four-sentence summary (data/ai/tags.json)
    ai_tags        TEXT,      -- space separated canonical tag ids, also in project_ai_tags
    ai_stage       TEXT,      -- reviewed | pass2 | pass1
    ai_verdict     TEXT,      -- reviewer verdict when reviewed
    ai_confidence  TEXT,
    ai_insufficient_docs INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX projects_test_status ON projects(test_status);

-- Model-generated canonical tags (ttsearch.ai; see data/ai/taxonomy.json).
CREATE TABLE ai_tags (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,   -- type | interface | output | domain | impl
    meaning     TEXT NOT NULL,
    sort_order  INTEGER NOT NULL
);
CREATE TABLE project_ai_tags (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    tag         TEXT NOT NULL REFERENCES ai_tags(id)
);
CREATE INDEX project_ai_tags_tag ON project_ai_tags(tag);
CREATE INDEX project_ai_tags_project ON project_ai_tags(project_id);

-- Silicon test reports people filed on tinytapeout.com (tt-fetch-feedback).
CREATE TABLE feedback (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    status      TEXT NOT NULL,      -- working | partial | broken
    user        TEXT NOT NULL,      -- GitHub user name of the reporter
    owner       INTEGER NOT NULL,   -- 1 if the reporter is the project author
    feedback    TEXT NOT NULL,
    link        TEXT
);
CREATE INDEX feedback_project ON feedback(project_id);

CREATE VIRTUAL TABLE projects_fts USING fts5(
    title, description, how_it_works, how_to_test, external_hw,
    tags, pin_names, macro, author, pmods, feedback_text, ai_summary, ai_tags,
    content='projects', content_rowid='id',
    tokenize='porter unicode61'
);

-- Keep the FTS index in sync with the content table.
CREATE TRIGGER projects_ai AFTER INSERT ON projects BEGIN
  INSERT INTO projects_fts(rowid, title, description, how_it_works, how_to_test,
                           external_hw, tags, pin_names, macro, author, pmods, feedback_text,
                           ai_summary, ai_tags)
  VALUES (new.id, new.title, new.description, new.how_it_works, new.how_to_test,
          new.external_hw, new.tags, new.pin_names, new.macro, new.author, new.pmods,
          new.feedback_text, new.ai_summary, new.ai_tags);
END;
CREATE TRIGGER projects_au AFTER UPDATE ON projects BEGIN
  INSERT INTO projects_fts(projects_fts, rowid, title, description, how_it_works,
                           how_to_test, external_hw, tags, pin_names, macro, author, pmods,
                           feedback_text, ai_summary, ai_tags)
  VALUES ('delete', old.id, old.title, old.description, old.how_it_works,
          old.how_to_test, old.external_hw, old.tags, old.pin_names, old.macro,
          old.author, old.pmods, old.feedback_text, old.ai_summary, old.ai_tags);
  INSERT INTO projects_fts(rowid, title, description, how_it_works, how_to_test,
                           external_hw, tags, pin_names, macro, author, pmods, feedback_text,
                           ai_summary, ai_tags)
  VALUES (new.id, new.title, new.description, new.how_it_works, new.how_to_test,
          new.external_hw, new.tags, new.pin_names, new.macro, new.author, new.pmods,
          new.feedback_text, new.ai_summary, new.ai_tags);
END;
CREATE TRIGGER projects_ad AFTER DELETE ON projects BEGIN
  INSERT INTO projects_fts(projects_fts, rowid, title, description, how_it_works,
                           how_to_test, external_hw, tags, pin_names, macro, author, pmods,
                           feedback_text, ai_summary, ai_tags)
  VALUES ('delete', old.id, old.title, old.description, old.how_it_works,
          old.how_to_test, old.external_hw, old.tags, old.pin_names, old.macro,
          old.author, old.pmods, old.feedback_text, old.ai_summary, old.ai_tags);
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


def build(projects_json: Path, shuttles_json: Path, db_path: Path,
          feedback_json: Path | None = None, ai_tags_json: Path | None = None,
          ai_taxonomy_json: Path | None = None) -> sqlite3.Connection:
    projects_doc = json.loads(projects_json.read_text())
    shuttles_doc = json.loads(shuttles_json.read_text())
    reports: list[dict] = []
    if feedback_json is not None and feedback_json.exists():
        reports = json.loads(feedback_json.read_text())["reports"]
    ai_entries: dict[str, dict] = {}
    ai_taxonomy: list[dict] = []
    if ai_tags_json is not None and ai_tags_json.exists():
        ai_entries = json.loads(ai_tags_json.read_text())["projects"]
    if ai_taxonomy_json is not None and ai_taxonomy_json.exists():
        ai_taxonomy = json.loads(ai_taxonomy_json.read_text())["tags"]

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

    load_feedback(con, reports)
    load_ai_tags(con, ai_entries, ai_taxonomy)

    con.execute("INSERT INTO meta VALUES ('source', ?)", (projects_doc.get("source"),))
    con.execute("INSERT INTO meta VALUES ('index_updated', ?)",
                (shuttles_doc.get("index_updated"),))
    con.execute("INSERT INTO projects_fts(projects_fts) VALUES ('optimize')")
    con.commit()
    return con


def load_feedback(con: sqlite3.Connection, reports: list[dict]) -> int:
    """Attach silicon reports to projects and fill in the per-project summary."""
    ids = {(shuttle, macro): pid for pid, shuttle, macro in con.execute(
        "SELECT id, shuttle, macro FROM projects WHERE subtile_addr IS NULL")}
    # Sub-tile projects share a macro name only within their group; match on
    # (shuttle, macro) alone is enough for the top-level ones the site reports on.
    unmatched = 0
    per_project: dict[int, list[dict]] = {}
    for r in reports:
        pid = ids.get((r["shuttle"], r["macro"]))
        if pid is None or r["status"] not in STATUSES:
            unmatched += 1
            continue
        per_project.setdefault(pid, []).append(r)
        con.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?)",
                    (pid, r["status"], r["user"], 1 if r.get("owner") else 0,
                     r["feedback"], r.get("link") or None))
    for pid, items in per_project.items():
        counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
        text = "\n".join(i["feedback"] for i in items if i["feedback"])
        con.execute(
            """UPDATE projects SET fb_working = ?, fb_partial = ?, fb_broken = ?,
               test_status = ?, feedback_text = ? WHERE id = ?""",
            (counts["working"], counts["partial"], counts["broken"],
             test_status(counts["working"], counts["partial"], counts["broken"]),
             text or None, pid),
        )
    if unmatched:
        print(f"warning: {unmatched} feedback reports did not match a project", file=sys.stderr)
    return len(reports) - unmatched


def load_ai_tags(con: sqlite3.Connection, entries: dict[str, dict], taxonomy: list[dict]) -> int:
    """Attach the model-generated tags and summaries (data/ai/tags.json)."""
    if not entries:
        return 0
    known = set()
    for order, t in enumerate(taxonomy):
        con.execute("INSERT OR IGNORE INTO ai_tags VALUES (?,?,?,?)",
                    (t["tag"], t["category"], t.get("meaning", ""), order))
        known.add(t["tag"])
    n = 0
    for pid, shuttle, macro in con.execute("SELECT id, shuttle, macro FROM projects").fetchall():
        e = entries.get(f"{shuttle}/{macro}")
        if not e or not e.get("tags") and not e.get("summary"):
            continue
        tags = [t for t in e.get("tags", []) if t in known]
        con.execute(
            """UPDATE projects SET ai_summary = ?, ai_tags = ?, ai_stage = ?, ai_verdict = ?,
               ai_confidence = ?, ai_insufficient_docs = ? WHERE id = ?""",
            (e.get("summary") or None, " ".join(tags) or None, e.get("stage"), e.get("verdict"),
             e.get("confidence"), 1 if e.get("insufficient_docs") else 0, pid),
        )
        con.executemany("INSERT INTO project_ai_tags VALUES (?,?)", [(pid, t) for t in tags])
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--projects", type=Path, default=PROJECTS_JSON)
    ap.add_argument("--shuttles", type=Path, default=SHUTTLES_JSON)
    ap.add_argument("--feedback", type=Path, default=FEEDBACK_JSON,
                    help="silicon reports from tt-fetch-feedback (optional)")
    ap.add_argument("--ai-tags", type=Path, default=AI_TAGS_JSON,
                    help="model-generated tags and summaries from tt-ai-finalize (optional)")
    ap.add_argument("--ai-taxonomy", type=Path, default=AI_TAXONOMY_JSON)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args(argv)

    con = build(args.projects, args.shuttles, args.db, args.feedback, args.ai_tags, args.ai_taxonomy)
    n_shuttles = con.execute("SELECT count(*) FROM shuttles").fetchone()[0]
    n_projects = con.execute("SELECT count(*) FROM projects").fetchone()[0]
    n_pins = con.execute("SELECT count(*) FROM pins").fetchone()[0]
    n_pmods = con.execute("SELECT count(DISTINCT project_id) FROM project_pmods").fetchone()[0]
    n_fb = con.execute("SELECT count(*) FROM feedback").fetchone()[0]
    n_tested = con.execute("SELECT count(*) FROM projects WHERE test_status != 'untested'").fetchone()[0]
    n_ai = con.execute("SELECT count(*) FROM projects WHERE ai_summary IS NOT NULL").fetchone()[0]
    n_ai_tags = con.execute("SELECT count(*) FROM ai_tags").fetchone()[0]
    con.close()
    print(f"{args.db}: {n_shuttles} shuttles, {n_projects} projects, {n_pins} named pins, "
          f"{n_pmods} projects with an inferred PMOD pinout, {n_fb} silicon reports on "
          f"{n_tested} projects, AI summaries on {n_ai} projects using {n_ai_tags} tags", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
