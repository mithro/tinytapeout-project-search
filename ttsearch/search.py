# SPDX-License-Identifier: Apache-2.0
"""Keyword search over the Tiny Tapeout project database.

    uv run tt-search vga
    uv run tt-search "risc-v" --summary
    uv run tt-search i2c --shuttle tt06 --shuttle tt07
    uv run tt-search --raw 'title:vga AND NOT game'

Plain queries are turned into an FTS5 MATCH expression: every word must match
(implicit AND), words containing punctuation are quoted so "risc-v" works, and
a few common terms are expanded with synonyms (see SYNONYMS). Use --raw to pass
FTS5 syntax through unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import DB_PATH
from .pmods import PMOD_BY_ID

# Groups of terms that mean the same thing for our purposes. A query word that
# appears in a group is replaced by an OR of the whole group. Entries may use
# FTS5 syntax (prefix "*" and quoted phrases). The list lives in synonyms.json
# so the browser-side search (static site) uses exactly the same table.
SYNONYMS_JSON = Path(__file__).with_name("synonyms.json")
SYNONYMS: list[list[str]] = json.loads(SYNONYMS_JSON.read_text())

_SYNONYM_LOOKUP: dict[str, list[str]] = {}
for _group in SYNONYMS:
    for _term in _group:
        _SYNONYM_LOOKUP[_term.strip('"').lower()] = _group

# Link to a project page on tinytapeout.com. The site's Cloudflare function
# (functions/chips/[shuttle]/[project]/index.ts in the tinytapeout_www repo)
# serves /chips/<shuttle>/<macro>; a trailing slash 404s and numeric
# addresses only redirect here.
PROJECT_URL = "https://tinytapeout.com/chips/{shuttle}/{macro}"
SHUTTLE_URL = "https://tinytapeout.com/chips/{shuttle}/"

# A bare word for FTS5 purposes: letters, digits and underscore only.
_BAREWORD = re.compile(r"^[\w]+\*?$")


def fts_term(term: str) -> str:
    """Quote a single query term so FTS5 treats it as a literal."""
    if _BAREWORD.match(term):
        return term
    prefix = term.endswith("*")
    core = term[:-1] if prefix else term
    quoted = '"' + core.replace('"', '""') + '"'
    return quoted + ("*" if prefix else "")


def build_match(query: str, synonyms: bool = True) -> str:
    """Turn a human query into an FTS5 MATCH expression."""
    terms: list[str] = []
    for raw in query.split():
        raw = raw.strip()
        if not raw:
            continue
        key = raw.lower().rstrip("*")
        group = _SYNONYM_LOOKUP.get(key) if synonyms else None
        if group:
            alts = [t if t.startswith('"') else fts_term(t) for t in group]
            if raw.lower() not in {g.lower() for g in group}:
                alts.insert(0, fts_term(raw))
            terms.append("(" + " OR ".join(alts) + ")")
        else:
            terms.append(fts_term(raw))
    return " AND ".join(terms)


# Markers wrapped around matched words inside Hit.snippet. Control characters
# so that literal brackets in pin names such as ui[0] are never confused with
# highlighting.
SNIPPET_START = "\x01"
SNIPPET_END = "\x02"


@dataclass
class Hit:
    id: int
    shuttle: str
    shuttle_name: str
    macro: str
    address: int | None
    subtile_addr: int | None
    type: str | None
    title: str
    author: str | None
    description: str | None
    language: str | None
    tiles: str | None
    repo: str | None
    snippet: str
    rank: float
    pmods: list[str] = field(default_factory=list)   # inferred PMOD ids

    @property
    def url(self) -> str:
        return PROJECT_URL.format(shuttle=self.shuttle, macro=self.macro)

    @property
    def address_str(self) -> str:
        if self.address is None:
            return ""
        if self.subtile_addr is not None:
            return f"{self.address}/{self.subtile_addr}"
        return str(self.address)


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"{db_path} not found; run `uv run tt-build-db` first")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def search(con: sqlite3.Connection, query: str, *, raw: bool = False,
           shuttles: list[str] | None = None, limit: int | None = None,
           include_groups: bool = False, pmod: str | None = None) -> list[Hit]:
    """Run a search and return hits ordered by relevance.

    With a query the FTS index is used and hits are ranked by bm25. With an
    empty query and a pmod filter, every project whose pinout matches that
    PMOD is listed in shuttle/address order instead.
    """
    match = query if raw else build_match(query)
    if not match and not pmod:
        return []
    if pmod and pmod not in PMOD_BY_ID:
        raise ValueError(f"unknown pmod id {pmod!r}; one of {', '.join(PMOD_BY_ID)}")
    params: list = []
    if match:
        sql = """
            SELECT p.id, p.shuttle, s.name AS shuttle_name, p.macro, p.address,
                   p.subtile_addr, p.type, p.title, p.author, p.description,
                   p.language, p.tiles, p.repo, p.pmods,
                   snippet(projects_fts, -1, char(1), char(2), ' … ', 24) AS snip,
                   bm25(projects_fts, 10.0, 5.0, 1.0, 1.0, 1.0, 5.0, 3.0, 4.0, 1.0, 2.0) AS rank
            FROM projects_fts f
            JOIN projects p ON p.id = f.rowid
            JOIN shuttles s ON s.id = p.shuttle
            WHERE projects_fts MATCH ?
        """
        params.append(match)
    else:
        sql = """
            SELECT p.id, p.shuttle, s.name AS shuttle_name, p.macro, p.address,
                   p.subtile_addr, p.type, p.title, p.author, p.description,
                   p.language, p.tiles, p.repo, p.pmods,
                   p.description AS snip, 0.0 AS rank
            FROM projects p
            JOIN shuttles s ON s.id = p.shuttle
            WHERE 1
        """
    if pmod:
        sql += " AND p.id IN (SELECT project_id FROM project_pmods WHERE pmod = ?)"
        params.append(pmod)
    if shuttles:
        sql += " AND p.shuttle IN (%s)" % ",".join("?" * len(shuttles))
        params.extend(shuttles)
    if not include_groups:
        sql += " AND (p.type IS NULL OR p.type != 'group')"
    sql += " ORDER BY rank, s.sort_order, p.address, p.subtile_addr" if match else \
           " ORDER BY s.sort_order, p.address, p.subtile_addr"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = con.execute(sql, params).fetchall()
    return [
        Hit(
            id=r["id"], shuttle=r["shuttle"], shuttle_name=r["shuttle_name"],
            macro=r["macro"], address=r["address"], subtile_addr=r["subtile_addr"],
            type=r["type"], title=r["title"] or r["macro"], author=r["author"],
            description=r["description"], language=r["language"], tiles=r["tiles"],
            repo=r["repo"], snippet=r["snip"] or "", rank=r["rank"],
            pmods=(r["pmods"] or "").split(),
        )
        for r in rows
    ]


def shuttle_order(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT id, name, pdk, project_count, experimental FROM shuttles ORDER BY sort_order"
    ).fetchall()


def summarise(con: sqlite3.Connection, hits: list[Hit]) -> list[tuple[sqlite3.Row, int]]:
    """Return (shuttle row, hit count) for every shuttle, in official order."""
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.shuttle] = counts.get(h.shuttle, 0) + 1
    return [(s, counts.get(s["id"], 0)) for s in shuttle_order(con)]


def print_summary(con: sqlite3.Connection, hits: list[Hit], query: str) -> None:
    rows = summarise(con, hits)
    print(f"{len(hits)} projects match {query!r}\n")
    print(f"{'shuttle':10} {'pdk':14} {'hits':>5} {'of':>5}  name")
    for s, n in rows:
        if n == 0:
            continue
        print(f"{s['id']:10} {s['pdk'] or '':14} {n:5} {s['project_count'] or 0:5}  {s['name']}")
    none = [s["id"] for s, n in rows if n == 0]
    if none:
        print(f"\nno matches on: {', '.join(none)}")


def print_hits(con: sqlite3.Connection, hits: list[Hit], query: str,
               show_snippets: bool = True) -> None:
    print(f"{len(hits)} projects match {query!r}")
    by_shuttle: dict[str, list[Hit]] = {}
    for h in hits:
        by_shuttle.setdefault(h.shuttle, []).append(h)
    for s in shuttle_order(con):
        group = by_shuttle.get(s["id"])
        if not group:
            continue
        print(f"\n== {s['id']}  {s['name']}  ({len(group)} of {s['project_count']} projects)")
        for h in group:
            author = f" — {h.author}" if h.author else ""
            print(f"  [{h.address_str:>5}] {h.title}{author}")
            pm = f"  pmods: {', '.join(h.pmods)}" if h.pmods else ""
            print(f"          {h.macro}  {h.url}{pm}")
            if show_snippets and h.snippet:
                snippet = " ".join(h.snippet.split())
                snippet = snippet.replace(SNIPPET_START, "[").replace(SNIPPET_END, "]")
                print(f"          {snippet}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("query", nargs="*", help="search words (may be empty with --pmod)")
    ap.add_argument("--pmod", metavar="ID",
                    help="only projects whose pinout matches this PMOD (see --list-pmods)")
    ap.add_argument("--list-pmods", action="store_true", help="list the known PMOD ids and exit")
    ap.add_argument("--summary", "-s", action="store_true",
                    help="only show how many matches each shuttle has")
    ap.add_argument("--shuttle", action="append", metavar="ID",
                    help="restrict to this shuttle (may be repeated)")
    ap.add_argument("--raw", action="store_true",
                    help="treat the query as raw FTS5 syntax")
    ap.add_argument("--no-synonyms", action="store_true",
                    help="do not expand synonyms (risc-v/riscv, i2c/iic, ...)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-snippets", action="store_true")
    ap.add_argument("--show-match", action="store_true",
                    help="print the FTS5 expression that was used")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args(argv)

    if args.list_pmods:
        for pm in PMOD_BY_ID.values():
            print(f"{pm.id:12} {pm.name:26} {pm.pins}")
        return 0
    query = " ".join(args.query)
    if not query and not args.pmod:
        ap.error("give search words, --pmod ID, or --list-pmods")
    con = connect(args.db)
    if args.raw:
        match = query
    else:
        match = build_match(query, synonyms=not args.no_synonyms)
    if args.show_match:
        print(f"MATCH {match}", file=sys.stderr)
    try:
        hits = search(con, match, raw=True, shuttles=args.shuttle, limit=args.limit,
                      pmod=args.pmod)
    except sqlite3.OperationalError as e:
        sys.exit(f"query error: {e}\n(expression was: {match})")
    except ValueError as e:
        sys.exit(str(e))
    if args.pmod:
        query = f"{query} [pmod {args.pmod}]".strip()
    if args.summary:
        print_summary(con, hits, query)
    else:
        print_hits(con, hits, query, show_snippets=not args.no_snippets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
