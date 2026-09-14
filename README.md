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

## Usage

```
uv run tt-fetch        # download everything into data/projects.json
uv run tt-build-db     # load data/projects.json into tt_projects.db (SQLite + FTS5)
uv run tt-search vga   # search from the command line
uv run tt-serve        # local web UI at http://127.0.0.1:8765/
```

## Licence

Apache 2.0. See [LICENSE](LICENSE). The project data itself belongs to the
individual project authors and Tiny Tapeout; see each project's repository for
its licence.
