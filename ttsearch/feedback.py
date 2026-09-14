# SPDX-License-Identifier: Apache-2.0
"""Download the silicon test reports ("feedback") for every shuttle.

After chips come back, people report on tinytapeout.com whether a project
works on real silicon. Each report has a status (working, partial or broken),
the reporter's GitHub user name, whether they are the project's author, free
text and an optional link. The website reads them from

    https://app.tinytapeout.com/api/shuttles/<shuttle>/feedback

which returns every report for that shuttle in one response, so the whole
download is one request per shuttle (28 as of 2026-09), one second apart, and
responses are cached under cache/feedback/ unless --refresh is given.

Output: data/feedback.json, a flat list of reports keyed by shuttle and macro.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
from pathlib import Path

from . import CACHE_DIR, DATA_DIR, SHUTTLES_JSON
from .fetch import REQUEST_DELAY_S, fetch_url, log

FEEDBACK_URL = "https://app.tinytapeout.com/api/shuttles/{shuttle}/feedback"
FEEDBACK_JSON = DATA_DIR / "feedback.json"
FEEDBACK_CACHE = CACHE_DIR / "feedback"
STATUSES = ("working", "partial", "broken")


def fetch_shuttle_feedback(shuttle: str, refresh: bool) -> list[dict]:
    FEEDBACK_CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = FEEDBACK_CACHE / f"{shuttle}.json"
    if cache_file.exists() and not refresh:
        log(f"  using cached {cache_file.relative_to(CACHE_DIR.parent)}")
        return json.loads(cache_file.read_text())
    url = FEEDBACK_URL.format(shuttle=shuttle)
    log(f"  GET {url}")
    try:
        body = fetch_url(url)
    except urllib.error.HTTPError as e:
        # A shuttle with no reports may 404; treat that as an empty list but
        # do not cache it, so it is retried next time.
        if e.code == 404:
            log(f"  no feedback for {shuttle} (404)")
            return []
        raise
    cache_file.write_bytes(body)
    time.sleep(REQUEST_DELAY_S)
    return json.loads(body)


def normalise(shuttle: str, item: dict) -> dict | None:
    status = str(item.get("status") or "").lower()
    macro = item.get("macro")
    if status not in STATUSES or not macro:
        return None
    return {
        "shuttle": shuttle,
        "macro": macro,
        "status": status,
        "user": item.get("user") or "",
        "owner": bool(item.get("owner")),
        "feedback": (item.get("feedback") or "").strip(),
        "link": (item.get("link") or "").strip(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shuttle", action="append", metavar="ID",
                    help="only fetch this shuttle id (may be repeated)")
    ap.add_argument("--refresh", action="store_true", help="re-download cached responses")
    args = ap.parse_args(argv)

    if not SHUTTLES_JSON.exists():
        sys.exit(f"{SHUTTLES_JSON} not found; run `uv run tt-fetch` first")
    shuttles = [s["id"] for s in json.loads(SHUTTLES_JSON.read_text())["shuttles"]]
    if args.shuttle:
        unknown = set(args.shuttle) - set(shuttles)
        if unknown:
            sys.exit(f"unknown shuttle id(s): {', '.join(sorted(unknown))}")
        shuttles = [s for s in shuttles if s in set(args.shuttle)]

    reports: list[dict] = []
    skipped = 0
    for sid in shuttles:
        log(f"[{sid}]")
        items = fetch_shuttle_feedback(sid, args.refresh)
        n = 0
        for item in items:
            rec = normalise(sid, item)
            if rec is None:
                skipped += 1
                continue
            reports.append(rec)
            n += 1
        log(f"  {n} reports")

    if args.shuttle and FEEDBACK_JSON.exists():
        fetched = set(shuttles)
        old = json.loads(FEEDBACK_JSON.read_text())["reports"]
        reports = [r for r in old if r["shuttle"] not in fetched] + reports

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FEEDBACK_JSON.write_text(json.dumps({
        "source": FEEDBACK_URL.format(shuttle="<shuttle>"),
        "reports": reports,
    }, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    log(f"Wrote {len(reports)} reports ({skipped} skipped as malformed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
