# Tiny Tapeout project search

Keyword search across every project on every [Tiny Tapeout](https://tinytapeout.com)
shuttle, and see which chips carry the projects you are interested in.

Examples of the kind of question this answers:

- Which chips have projects with VGA video output?
- Which chips carry a RISC-V CPU?
- Which projects implement an I2C controller, and on which shuttles?
- Which chips have crypto accelerators?

## Where the data comes from

Tiny Tapeout publishes a machine-readable index at
<https://index.tinytapeout.com/> (the same one the
[Tiny Tapeout Commander](https://commander.tinytapeout.com) app reads):

- `https://index.tinytapeout.com/` lists every shuttle.
- `https://index.tinytapeout.com/<shuttle>.json` lists every project on that
  shuttle with title, author, one-line description, pinout, clock, tiles,
  source repository and commit.

The longer documentation (how it works, how to test, external hardware, tags)
is not in that index. It lives in each shuttle's GitHub repository, either as
`info.yaml` (tt02 to tt05) or `docs/info.md` (tt06 onward). The fetcher does a
sparse partial git clone of each shuttle repository so only those small text
files are downloaded, not the multi-hundred-megabyte GDS layouts.

The whole fetch is about 30 HTTP requests to `index.tinytapeout.com` (one per
shuttle, spaced one second apart) plus one sparse clone per shuttle from
GitHub. It is deliberately gentle on the Tiny Tapeout site.

## Hosted version

<https://mith.ro/tinytapeout-project-search/> is a static build published by
GitHub Actions on every push to `main`. There is no server: the page loads the
SQLite database in the browser through [sql.js-httpvfs](https://github.com/phiresky/sql.js-httpvfs)
and HTTP Range requests, so a search fetches only the database pages it touches.

## Usage

```
uv run tt-fetch        # download everything into data/projects.json
uv run tt-build-db     # load data/projects.json into tt_projects.db (SQLite + FTS5)
uv run tt-search vga   # search from the command line
uv run tt-serve        # local web UI at http://127.0.0.1:8765/
```

The data files under `data/` are committed, so only `tt-build-db` is needed to
get a working database. Re-run `tt-fetch --refresh` to pick up new shuttles
or updated documentation.

### Command line

```
uv run tt-search "risc-v" --summary        # how many matches on each chip
uv run tt-search i2c --shuttle tt06        # only one shuttle
uv run tt-search --raw 'title:vga NOT game'  # raw FTS5 syntax
```

Words are combined with AND. Common terms are expanded with synonyms from
`ttsearch/synonyms.json` (risc-v/riscv/rv32, i2c/iic, crypto/aes/sha, ...).

### Static site

```
uv run tt-build-site           # writes ./site/ (slimmed database, vendored sql.js-httpvfs)
uv run tt-serve --site site    # preview at http://127.0.0.1:8766/ with Range support
```

The same page works in both modes: with `tt-serve` it talks to a small JSON
API and the search runs in Python; in the static build it runs the same SQL
against the same database inside the browser.

## Layout

| Path | Purpose |
| --- | --- |
| `ttsearch/fetch.py` | Download index JSON and sparse-clone the shuttle repos into `data/` |
| `ttsearch/build_db.py` | Build `tt_projects.db` (tables plus an FTS5 index) |
| `ttsearch/search.py` | Query building, synonyms, CLI |
| `ttsearch/serve.py` | Local server: JSON API, or static preview with Range requests |
| `ttsearch/build_site.py` | Assemble `site/` for GitHub Pages |
| `ttsearch/index.html` | The single-page UI (API and in-browser SQLite backends) |
| `ttsearch/static/vendor/` | sql.js-httpvfs 0.8.12 (Apache 2.0) |
| `data/projects.json` | One record per project, all shuttles |
| `data/shuttles.json` | Shuttle list from the index |
| `.github/workflows/pages.yml` | Test, build database and site, deploy to Pages |

## Known gaps

- tt01 is not in the official index and is not included.
- `tt_um_pad_test` on ttgf0p1 has no `info.yaml` upstream, so it has no
  documentation here.

## Licence

Apache 2.0. See [LICENSE](LICENSE). The project data itself belongs to the
individual project authors and Tiny Tapeout; see each project's repository for
its licence.
