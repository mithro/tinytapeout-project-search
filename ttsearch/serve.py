# SPDX-License-Identifier: Apache-2.0
"""Local web UI for the Tiny Tapeout project database.

    uv run tt-serve                    # http://127.0.0.1:8765/ (API mode)
    uv run tt-serve --site site        # preview the static build (browser-side SQLite)
    uv run tt-serve --port 9000 --host 0.0.0.0

Uses only the standard library. In the default mode the page talks to these
endpoints, and search runs in Python:

    GET /                      the single-page UI
    GET /api/shuttles          shuttle list in official order
    GET /api/pmods             PMOD definitions with match counts
    GET /api/tests             how many projects have each silicon test status
    GET /api/search?q=...      hits plus per-shuttle counts (also &pmod=ID, &status=S,
                               &sort=relevance|tested|address, &shuttle=ID, &raw=1)
    GET /api/project/<id>      full record for one project

With --site DIR the server only serves files from DIR (the output of
`tt-build-site`), honouring HTTP Range requests so that sql.js-httpvfs can read
the database in pieces, exactly as GitHub Pages does.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import DB_PATH
from .pmods import PMODS
from .search import (PROJECT_URL, SHUTTLE_URL, STATUS_FILTERS, SORTS, TEST_STATUSES,
                     build_match, connect, search, shuttle_order)

PKG = Path(__file__).parent
INDEX_HTML = PKG / "index.html"
STATIC_DIR = PKG / "static"
SYNONYMS_JSON = PKG / "synonyms.json"

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("application/vnd.sqlite3", ".db")


def project_record(con: sqlite3.Connection, pid: int) -> dict | None:
    row = con.execute(
        """SELECT p.*, s.name AS shuttle_name, s.pdk FROM projects p
           JOIN shuttles s ON s.id = p.shuttle WHERE p.id = ?""", (pid,)
    ).fetchone()
    if row is None:
        return None
    rec = dict(row)
    rec.pop("docs_md", None)
    rec["pins"] = [dict(r) for r in con.execute(
        "SELECT pin, name FROM pins WHERE project_id = ? ORDER BY rowid", (pid,))]
    rec["tag_list"] = [r["tag"] for r in con.execute(
        "SELECT tag FROM tags WHERE project_id = ? ORDER BY tag", (pid,))]
    rec["pmod_list"] = (rec.get("pmods") or "").split()
    rec["reports"] = [dict(r) for r in con.execute(
        "SELECT status, user, owner, feedback, link FROM feedback WHERE project_id = ? ORDER BY rowid",
        (pid,))]
    rec["url"] = PROJECT_URL.format(shuttle=rec["shuttle"], macro=rec["macro"])
    return rec


def shuttle_list(con: sqlite3.Connection) -> list[dict]:
    return [
        {**dict(s), "url": SHUTTLE_URL.format(shuttle=s["id"])}
        for s in shuttle_order(con)
    ]


def pmod_list(con: sqlite3.Connection) -> list[dict]:
    """PMOD definitions with how many projects match each."""
    counts = dict(con.execute(
        "SELECT pmod, count(*) FROM project_pmods GROUP BY pmod").fetchall())
    return [{"id": pm.id, "name": pm.name, "pins": pm.pins, "url": pm.url,
             "count": counts.get(pm.id, 0)} for pm in PMODS]


def test_summary(con: sqlite3.Connection) -> list[dict]:
    """How many projects fall in each test status."""
    counts = dict(con.execute(
        "SELECT test_status, count(*) FROM projects WHERE type IS NULL OR type != 'group' "
        "GROUP BY test_status").fetchall())
    reports = con.execute("SELECT count(*) FROM feedback").fetchone()[0]
    return {"statuses": [{"id": s, "count": counts.get(s, 0)} for s in TEST_STATUSES],
            "reports": reports}


def do_search(con: sqlite3.Connection, params: dict[str, list[str]]) -> dict:
    query = (params.get("q") or [""])[0].strip()
    raw = (params.get("raw") or ["0"])[0] in ("1", "true", "yes")
    shuttles = [s for s in params.get("shuttle", []) if s]
    pmod = (params.get("pmod") or [""])[0].strip() or None
    status = (params.get("status") or [""])[0].strip() or None
    sort = (params.get("sort") or ["relevance"])[0].strip() or "relevance"
    limit = int((params.get("limit") or ["0"])[0]) or None
    match = query if raw else build_match(query)
    out: dict = {"query": query, "match": match, "pmod": pmod, "status": status, "sort": sort,
                 "hits": [], "shuttles": [], "error": None}
    hits = []
    if match or pmod or status:
        try:
            hits = search(con, match, raw=True, limit=limit, pmod=pmod, status=status, sort=sort)
        except sqlite3.OperationalError as e:
            out["error"] = f"Could not understand that query: {e}"
        except ValueError as e:
            out["error"] = str(e)
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.shuttle] = counts.get(h.shuttle, 0) + 1
    out["shuttles"] = [{**s, "hits": counts.get(s["id"], 0)} for s in shuttle_list(con)]
    if shuttles:
        hits = [h for h in hits if h.shuttle in set(shuttles)]
    out["total"] = sum(counts.values())
    out["hits"] = [
        {
            "id": h.id, "shuttle": h.shuttle, "shuttle_name": h.shuttle_name,
            "macro": h.macro, "address": h.address_str, "type": h.type,
            "title": h.title, "author": h.author, "description": h.description,
            "language": h.language, "tiles": h.tiles, "repo": h.repo,
            "snippet": h.snippet, "url": h.url, "pmods": h.pmods,
            "fb_working": h.fb_working, "fb_partial": h.fb_partial, "fb_broken": h.fb_broken,
            "test_status": h.test_status,
        }
        for h in hits
    ]
    return out


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class Handler(BaseHTTPRequestHandler):
    db_path: Path = DB_PATH
    site_dir: Path | None = None      # set in --site mode
    server_version = "tt-serve/0.1"
    head_only = False                 # set while answering a HEAD request

    def log_message(self, fmt, *args):  # quieter than the default
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- helpers -----------------------------------------------------

    def send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not self.head_only:
            self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        """Send a file, honouring a single-range Range header (RFC 9110)."""
        if not path.is_file():
            self.send_json({"error": "not found"}, 404)
            return
        size = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            ctype += "; charset=utf-8"
        start, end = 0, size - 1
        status = 200
        m = _RANGE_RE.match(self.headers.get("Range", "") or "")
        if m:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):               # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.head_only:
            return
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def safe_path(self, base: Path, rel: str) -> Path | None:
        """Resolve rel under base, refusing anything that escapes it."""
        target = (base / rel.lstrip("/")).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            return None
        return target

    # ---- routing -----------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802 (http.server naming)
        # sql.js-httpvfs asks for the database size with HEAD before reading.
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        path = url.path

        if self.site_dir is not None:
            # Static preview mode: plain files only, like GitHub Pages.
            rel = "index.html" if path in ("", "/") else path
            target = self.safe_path(self.site_dir, rel)
            if target is None:
                self.send_json({"error": "not found"}, 404)
            else:
                self.send_file(target)
            return

        if path == "/":
            self.send_file(INDEX_HTML)
        elif path == "/favicon.ico":
            # The page carries an inline SVG icon; browsers may still probe here.
            self.send_response(204)
            self.end_headers()
        elif path == "/synonyms.json":
            self.send_file(SYNONYMS_JSON)
        elif path == "/tt_projects.db":
            self.send_file(self.db_path)
        elif path.startswith("/static/"):
            target = self.safe_path(STATIC_DIR, path[len("/static/"):])
            if target is None:
                self.send_json({"error": "not found"}, 404)
            else:
                self.send_file(target)
        elif path.startswith("/api/"):
            con = connect(self.db_path)
            try:
                self.do_api(path, params, con)
            finally:
                con.close()
        else:
            self.send_json({"error": "not found"}, 404)

    def do_api(self, path: str, params: dict, con: sqlite3.Connection) -> None:
        if path == "/api/shuttles":
            self.send_json({"shuttles": shuttle_list(con)})
        elif path == "/api/pmods":
            self.send_json({"pmods": pmod_list(con)})
        elif path == "/api/tests":
            self.send_json(test_summary(con))
        elif path == "/api/search":
            self.send_json(do_search(con, params))
        elif path.startswith("/api/project/"):
            try:
                pid = int(path.rsplit("/", 1)[1])
            except ValueError:
                self.send_json({"error": "bad project id"}, 400)
                return
            rec = project_record(con, pid)
            if rec is None:
                self.send_json({"error": "no such project"}, 404)
            else:
                self.send_json(rec)
        else:
            self.send_json({"error": "not found"}, 404)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--site", type=Path, default=None, metavar="DIR",
                    help="serve a static build from DIR instead of the API")
    args = ap.parse_args(argv)
    if args.site is not None:
        if not (args.site / "index.html").is_file():
            sys.exit(f"{args.site}/index.html not found; run `uv run tt-build-site` first")
        Handler.site_dir = args.site
        mode = f"static files from {args.site}"
    else:
        if not args.db.exists():
            sys.exit(f"{args.db} not found; run `uv run tt-build-db` first")
        Handler.db_path = args.db
        mode = "API"
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving {mode} on http://{args.host}:{args.port}/  (Ctrl-C to stop)",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
