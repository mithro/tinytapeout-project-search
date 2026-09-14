# SPDX-License-Identifier: Apache-2.0
"""Local web UI for the Tiny Tapeout project database.

    uv run tt-serve              # http://127.0.0.1:8765/
    uv run tt-serve --port 9000 --host 0.0.0.0

Uses only the standard library. Endpoints:

    GET /                      the single-page UI
    GET /api/shuttles          shuttle list in official order
    GET /api/search?q=...      hits plus per-shuttle counts (also &shuttle=ID, &raw=1)
    GET /api/project/<id>      full record for one project
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import DB_PATH
from .search import PROJECT_URL, SHUTTLE_URL, build_match, connect, search, shuttle_order

INDEX_HTML = Path(__file__).with_name("index.html")


def project_record(con: sqlite3.Connection, pid: int) -> dict | None:
    row = con.execute(
        """SELECT p.*, s.name AS shuttle_name, s.pdk FROM projects p
           JOIN shuttles s ON s.id = p.shuttle WHERE p.id = ?""", (pid,)
    ).fetchone()
    if row is None:
        return None
    rec = dict(row)
    rec["pins"] = [dict(r) for r in con.execute(
        "SELECT pin, name FROM pins WHERE project_id = ? ORDER BY rowid", (pid,))]
    rec["tag_list"] = [r["tag"] for r in con.execute(
        "SELECT tag FROM tags WHERE project_id = ? ORDER BY tag", (pid,))]
    rec["url"] = PROJECT_URL.format(shuttle=rec["shuttle"], macro=rec["macro"])
    return rec


def shuttle_list(con: sqlite3.Connection) -> list[dict]:
    return [
        {**dict(s), "url": SHUTTLE_URL.format(shuttle=s["id"])}
        for s in shuttle_order(con)
    ]


def do_search(con: sqlite3.Connection, params: dict[str, list[str]]) -> dict:
    query = (params.get("q") or [""])[0].strip()
    raw = (params.get("raw") or ["0"])[0] in ("1", "true", "yes")
    shuttles = [s for s in params.get("shuttle", []) if s]
    limit = int((params.get("limit") or ["0"])[0]) or None
    match = query if raw else build_match(query)
    out: dict = {"query": query, "match": match, "hits": [], "shuttles": [], "error": None}
    hits = []
    if match:
        try:
            hits = search(con, match, raw=True, limit=limit)
        except sqlite3.OperationalError as e:
            out["error"] = f"Could not understand that query: {e}"
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
            "snippet": h.snippet, "url": h.url,
        }
        for h in hits
    ]
    return out


class Handler(BaseHTTPRequestHandler):
    db_path: Path = DB_PATH
    server_version = "tt-serve/0.1"

    def log_message(self, fmt, *args):  # quieter than the default
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        con = connect(self.db_path)
        try:
            if url.path == "/":
                body = INDEX_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/favicon.ico":
                # The page carries an inline SVG icon; browsers may still probe here.
                self.send_response(204)
                self.end_headers()
            elif url.path == "/api/shuttles":
                self.send_json({"shuttles": shuttle_list(con)})
            elif url.path == "/api/search":
                self.send_json(do_search(con, params))
            elif url.path.startswith("/api/project/"):
                try:
                    pid = int(url.path.rsplit("/", 1)[1])
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
        finally:
            con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args(argv)
    if not args.db.exists():
        sys.exit(f"{args.db} not found; run `uv run tt-build-db` first")
    Handler.db_path = args.db
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving on http://{args.host}:{args.port}/  (Ctrl-C to stop)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
