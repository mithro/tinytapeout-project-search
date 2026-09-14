# SPDX-License-Identifier: Apache-2.0
"""Assemble a fully static copy of the search UI for GitHub Pages.

    uv run tt-build-site            # writes ./site/
    uv run tt-serve --site site     # preview it locally (needs Range support)

The static site has no server-side code. The page loads the SQLite database
directly in the browser through sql.js-httpvfs, which fetches only the database
pages a query touches using HTTP Range requests, so a search costs a few hundred
kilobytes rather than the whole file.

The database copy is slimmed (the raw markdown column is dropped, since the UI
only shows the parsed sections) and re-packed with a 1 KiB page size, which
keeps each range request small.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from . import DB_PATH, ROOT

PKG = Path(__file__).parent
SITE_DIR = ROOT / "site"
STATIC_PAGE_SIZE = 1024

# The database is published under a .png name. It is a plain SQLite file, not
# an image: GitHub Pages gzips anything it classifies as data (.db, .sqlite3,
# .bin, .wasm, .json ...) and then applies Range offsets to the *compressed*
# stream, which makes byte-range reads return garbage. Image types are left
# uncompressed, so the suffix is purely a way to opt out of compression.
# Verified on 2026-09-14 by deploying probe files: .png/.gz/.zip/.woff2 were
# served raw, .db/.sqlite3/.bin/.tar were gzipped.
STATIC_DB_NAME = "tt_projects.db.png"


def slim_database(src: Path, dest: Path, page_size: int = STATIC_PAGE_SIZE) -> None:
    if dest.exists():
        dest.unlink()
    shutil.copyfile(src, dest)
    con = sqlite3.connect(dest)
    con.execute("ALTER TABLE projects DROP COLUMN docs_md")
    con.execute("INSERT INTO meta VALUES ('static', 'true')")
    con.commit()
    # page_size only takes effect on VACUUM; journal_mode delete keeps the file
    # self-contained (no -wal side file for the browser to look for).
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute(f"PRAGMA page_size = {page_size}")
    con.execute("VACUUM")
    con.close()


def build(db_path: Path, site_dir: Path) -> None:
    if not db_path.exists():
        sys.exit(f"{db_path} not found; run `uv run tt-build-db` first")
    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)

    shutil.copyfile(PKG / "synonyms.json", site_dir / "synonyms.json")
    shutil.copytree(PKG / "static", site_dir / "static")
    slim_database(db_path, site_dir / STATIC_DB_NAME)
    db_length = (site_dir / STATIC_DB_NAME).stat().st_size

    # Mark the page as static so it goes straight to the in-browser database
    # instead of probing for the API first, and tell it where the database is
    # and how long it is (saves a HEAD request and works even if the host
    # hides Content-Length).
    html = (PKG / "index.html").read_text()
    marker = (
        '<meta name="tt-backend" content="static">\n'
        f'<meta name="tt-db-url" content="{STATIC_DB_NAME}">\n'
        f'<meta name="tt-db-length" content="{db_length}">'
    )
    assert "<title>" in html
    html = html.replace("<title>", marker + "\n<title>", 1)
    (site_dir / "index.html").write_text(html)
    # Tell GitHub Pages not to run Jekyll (it would ignore some paths).
    (site_dir / ".nojekyll").write_text("")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--out", type=Path, default=SITE_DIR)
    args = ap.parse_args(argv)
    build(args.db, args.out)
    total = sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file())
    db_size = (args.out / STATIC_DB_NAME).stat().st_size
    print(f"{args.out}: {total / 1e6:.1f} MB total, database {db_size / 1e6:.1f} MB",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
