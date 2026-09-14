# SPDX-License-Identifier: Apache-2.0
"""Download project metadata for every Tiny Tapeout shuttle into data/projects.json.

Two sources are combined:

1. The official index at https://index.tinytapeout.com/ (the same API the
   Tiny Tapeout Commander app reads). One request for the shuttle list plus one
   request per shuttle. Responses are cached under cache/index/ so re-running
   the script does not hit the site again unless --refresh is given.

2. A sparse partial git clone of each shuttle's GitHub repository, fetching only
   the small per-project text files (info.yaml and docs/info.md). This is where
   the long-form documentation lives. Clones are kept under cache/shuttles/.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import CACHE_DIR, DATA_DIR, PROJECTS_JSON, SHUTTLES_JSON

INDEX_URL = "https://index.tinytapeout.com/"
USER_AGENT = (
    "tinytapeout-project-search/0.1 "
    "(+https://github.com/mithro/tinytapeout-project-search)"
)
REQUEST_DELAY_S = 1.0

# Files to pull out of each shuttle repository. Patterns are git sparse-checkout
# non-cone (gitignore style) patterns and are deliberately unanchored because
# the shuttle repos have used several layouts over time: project_info/<name>/
# (tt02, tt03), projects/<macro>/ (tt04 onward), projects/<group>/docs/<macro>/
# for sub-tile projects (info.md beside info.yaml rather than under docs/),
# and main/src/projects/<macro>/ (ttgf0p1). Note that a gitignore pattern
# containing a slash is anchored to the root unless it starts with "**/".
SPARSE_PATTERNS = [
    "**/info.yaml",
    "**/info.md",
]

INDEX_CACHE = CACHE_DIR / "index"
SHUTTLE_CACHE = CACHE_DIR / "shuttles"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP index
# --------------------------------------------------------------------------


def fetch_url(url: str, retries: int = 3) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise          # not found / forbidden: retrying will not help
            last_error = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
            wait = 5 * (attempt + 1)
            log(f"  request failed ({e}); retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}: {last_error}")


def fetch_index_json(name: str, url: str, refresh: bool) -> dict:
    """Fetch a JSON document from the index, caching it on disk."""
    INDEX_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = INDEX_CACHE / f"{name}.json"
    if cache_file.exists() and not refresh:
        log(f"  using cached {cache_file.relative_to(CACHE_DIR.parent)}")
        return json.loads(cache_file.read_text())
    log(f"  GET {url}")
    body = fetch_url(url)
    cache_file.write_bytes(body)
    time.sleep(REQUEST_DELAY_S)
    return json.loads(body)


# --------------------------------------------------------------------------
# Sparse git clones
# --------------------------------------------------------------------------


def run_git(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)


def sparse_clone(repo_url: str, dest: Path, refresh: bool) -> None:
    """Clone only the info files from a shuttle repository."""
    if dest.exists():
        if not refresh:
            log(f"  using existing clone {dest.relative_to(CACHE_DIR.parent)}")
            return
        log(f"  updating clone {dest.relative_to(CACHE_DIR.parent)}")
        run_git(["fetch", "--quiet", "--depth", "1", "origin"], cwd=dest)
        run_git(["sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS], cwd=dest)
        run_git(["reset", "--quiet", "--hard", "FETCH_HEAD"], cwd=dest)
        return
    log(f"  sparse clone {repo_url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        [
            "clone",
            "--quiet",
            "--filter=blob:none",
            "--depth",
            "1",
            "--no-checkout",
            "--sparse",
            repo_url,
            str(dest),
        ]
    )
    run_git(["sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS], cwd=dest)
    run_git(["checkout", "--quiet"], cwd=dest)


# --------------------------------------------------------------------------
# Parsing the per-project documentation files
# --------------------------------------------------------------------------

# Headings used in docs/info.md (yaml_version 6 onward), mapped to record keys.
MD_SECTIONS = {
    "how it works": "how_it_works",
    "how to test": "how_to_test",
    "external hardware": "external_hw",
}


def parse_info_md(text: str) -> dict:
    """Split docs/info.md into its standard sections.

    Returns a dict with how_it_works / how_to_test / external_hw (when present)
    plus docs_md holding the full markdown with the template comment removed.
    """
    # Drop the HTML comment block that the template ships with.
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
    out: dict = {"docs_md": cleaned}
    current_key: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current_key is not None:
            body = "\n".join(buf).strip()
            if body:
                out[current_key] = body

    for line in cleaned.splitlines():
        m = re.match(r"^\s*#{1,3}\s+(.*?)\s*#*\s*$", line)
        if m:
            flush()
            heading = m.group(1).strip().lower()
            current_key = None
            for prefix, key in MD_SECTIONS.items():
                if heading.startswith(prefix):
                    current_key = key
                    break
            buf = []
            continue
        buf.append(line)
    flush()
    return out


def split_tags(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = str(value).split(",")
    return sorted({t.strip().lower() for t in items if t and t.strip()})


def str_or_none(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_info_yaml(text: str) -> dict:
    """Normalise the three generations of info.yaml into one flat dict."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return {"yaml_error": str(e)}
    if not isinstance(data, dict):
        return {"yaml_error": "top level is not a mapping"}

    project = data.get("project") or {}
    docs = data.get("documentation") or {}
    if not isinstance(project, dict):
        project = {}
    if not isinstance(docs, dict):
        docs = {}

    # yaml_version <= 4 keeps title/author/etc under `documentation:`;
    # yaml_version 6 moves them under `project:` and drops the prose.
    meta = {**docs, **{k: v for k, v in project.items() if v not in (None, "")}}

    out: dict = {
        "yaml_version": data.get("yaml_version"),
        "language": str_or_none(meta.get("language")),
        "tags": split_tags(meta.get("tag")),
        "doc_link": str_or_none(meta.get("doc_link")),
        "wokwi_id": meta.get("wokwi_id") or None,
        "source_files": [str(s) for s in (project.get("source_files") or []) if s],
        "top_module": str_or_none(project.get("top_module")),
    }
    for key in ("how_it_works", "how_to_test", "external_hw", "title", "author",
                "description"):
        val = str_or_none(docs.get(key))
        if val:
            out[key] = val

    # Old-style pin lists: inputs / outputs / bidirectional as 8-element lists.
    pins: dict[str, str] = {}
    for prefix, key in (("ui", "inputs"), ("uo", "outputs"), ("uio", "bidirectional")):
        values = docs.get(key) or []
        if isinstance(values, list):
            for i, name in enumerate(values[:8]):
                name = str_or_none(name)
                if name and name.lower() != "none":
                    pins[f"{prefix}[{i}]"] = name
    if pins:
        out["pinout_from_yaml"] = pins
    return out


def index_project_dirs(clone: Path) -> dict[str, Path]:
    """Map project directory name -> directory, for every info.yaml in the clone.

    The directory name is the project's macro (top module name), except on
    tt02/tt03 where it is a free-form name that the index also uses as `macro`.
    """
    dirs: dict[str, Path] = {}
    for info in clone.rglob("info.yaml"):
        if ".git" in info.parts:
            continue
        dirs.setdefault(info.parent.name, info.parent)
    return dirs


def read_project_docs(project_dirs: dict[str, Path], macro: str) -> dict:
    """Read info.yaml and docs/info.md for one project, if present."""
    out: dict = {"docs_source": []}
    pdir = project_dirs.get(macro)
    if pdir is None:
        return out
    info_yaml = pdir / "info.yaml"
    if info_yaml.is_file():
        out.update(parse_info_yaml(info_yaml.read_text(errors="replace")))
        out["docs_source"].append("info.yaml")
    # Normal projects keep the markdown under docs/; sub-tile projects keep it
    # beside info.yaml.
    for rel in ("docs/info.md", "info.md"):
        info_md = pdir / rel
        if info_md.is_file():
            out.update(parse_info_md(info_md.read_text(errors="replace")))
            out["docs_source"].append(rel)
            break
    return out


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

# Fields copied verbatim from the index entry when present.
INDEX_FIELDS = (
    "macro", "address", "subtile_addr", "type", "title", "author", "description",
    "clock_hz", "tiles", "analog_pins", "repo", "commit", "pinout",
    "danger_level", "danger_reason",
)


def merge_project(shuttle_id: str, entry: dict, docs: dict) -> dict:
    rec: dict = {"shuttle": shuttle_id}
    for f in INDEX_FIELDS:
        if f in entry:
            rec[f] = entry[f]
    # Anything the index has that we did not anticipate is kept too, so a
    # future index version does not silently lose data.
    for k, v in entry.items():
        if k not in rec:
            rec[k] = v

    # Pinout: prefer the index; fall back to the old yaml pin lists.
    pinout = rec.get("pinout") or {}
    if not any(v for v in pinout.values()) and docs.get("pinout_from_yaml"):
        rec["pinout"] = docs["pinout_from_yaml"]
    docs.pop("pinout_from_yaml", None)

    # The index's one-line description wins; the yaml's is only a fallback.
    for k, v in docs.items():
        if k in ("title", "author", "description"):
            if not rec.get(k):
                rec[k] = v
        else:
            rec[k] = v
    return rec


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shuttle", action="append", metavar="ID",
                    help="only fetch this shuttle id (may be repeated)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download index JSON and update git clones")
    ap.add_argument("--no-clone", action="store_true",
                    help="skip the git clones (index data only)")
    args = ap.parse_args(argv)

    log("Fetching shuttle list")
    root = fetch_index_json("_root", INDEX_URL, args.refresh)
    shuttles = root["shuttles"]
    if args.shuttle:
        wanted = set(args.shuttle)
        shuttles = [s for s in shuttles if s["id"] in wanted]
        missing = wanted - {s["id"] for s in shuttles}
        if missing:
            log(f"unknown shuttle id(s): {', '.join(sorted(missing))}")
            return 1

    all_projects: list[dict] = []
    shuttle_records: list[dict] = []
    for s in shuttles:
        sid = s["id"]
        log(f"[{sid}] {s['name']}")
        index = fetch_index_json(sid, f"{INDEX_URL}{sid}.json", args.refresh)
        project_dirs: dict[str, Path] = {}
        if not args.no_clone:
            clone = SHUTTLE_CACHE / sid
            sparse_clone(s["repo"], clone, args.refresh)
            project_dirs = index_project_dirs(clone)

        n_docs = 0
        for entry in index["projects"]:
            docs = read_project_docs(project_dirs, entry["macro"])
            if docs["docs_source"]:
                n_docs += 1
            all_projects.append(merge_project(sid, entry, docs))
        log(f"  {len(index['projects'])} projects, {n_docs} with docs")
        if n_docs < len(index["projects"]):
            missing = [e["macro"] for e in index["projects"]
                       if not read_project_docs(project_dirs, e["macro"])["docs_source"]]
            log(f"  no docs found for: {', '.join(missing[:10])}"
                + (" ..." if len(missing) > 10 else ""))

        shuttle_records.append({
            **s,
            "index_version": index.get("version"),
            "index_commit": index.get("commit"),
            "index_updated": index.get("updated"),
        })

    if args.shuttle:
        fetched = {s["id"] for s in shuttle_records}
        if PROJECTS_JSON.exists():
            old = json.loads(PROJECTS_JSON.read_text())["projects"]
            all_projects = [p for p in old if p["shuttle"] not in fetched] + all_projects
        if SHUTTLES_JSON.exists():
            old = json.loads(SHUTTLES_JSON.read_text())["shuttles"]
            shuttle_records = [s for s in old if s["id"] not in fetched] + shuttle_records
        # Keep the official shuttle order.
        order = {s["id"]: i for i, s in enumerate(root["shuttles"])}
        shuttle_records.sort(key=lambda s: order.get(s["id"], 999))
        all_projects.sort(key=lambda p: (order.get(p["shuttle"], 999), p.get("address") or 0,
                                         p.get("subtile_addr") or 0, p["macro"]))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SHUTTLES_JSON.write_text(json.dumps({
        "source": INDEX_URL,
        "index_updated": root.get("updated"),
        "shuttles": shuttle_records,
    }, indent=1, sort_keys=True) + "\n")
    PROJECTS_JSON.write_text(json.dumps({
        "source": INDEX_URL,
        "projects": all_projects,
    }, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    log(f"Wrote {len(all_projects)} projects across {len(shuttle_records)} shuttles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
