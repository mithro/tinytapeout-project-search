# SPDX-License-Identifier: Apache-2.0
"""Merge the passes into data/ai/tags.json, one entry per project.

    uv run tt-ai-finalize

For each project: the reviewed tags and summary when a review exists, else
the pass-two result, else the alias-mapped pass-one result. Resubmitted
designs get the same entry as the document they share. Also writes a short
stats block and the total spend from the usage ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import AI_DIR
from .corpus import load_docs
from .openrouter import usage_summary
from .pass2 import load_pass1, load_taxonomy, mapped_tags
from .review import load_pass2

TAGS_JSON = AI_DIR / "tags.json"


def load_review(sub: str) -> dict[str, dict]:
    base = AI_DIR / "review" / sub
    out: dict[str, dict] = {}
    if not base.exists():
        return out
    for f in sorted(base.rglob("*.json")):
        for r in json.loads(f.read_text()).get("results", []):
            if r.get("key"):
                out[r["key"]] = r
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sub", default="full")
    ap.add_argument("--out", type=Path, default=TAGS_JSON)
    args = ap.parse_args(argv)

    canon, aliases = load_taxonomy()
    p1 = load_pass1(args.sub)
    p2 = load_pass2(args.sub)
    rv = load_review(args.sub)
    docs = load_docs()

    projects: dict[str, dict] = {}
    stage_counts: Counter = Counter()
    verdicts: Counter = Counter()
    tag_counts: Counter = Counter()
    for d in docs:
        if d.key in rv:
            r = rv[d.key]
            entry = {"tags": r["tags"], "summary": r["summary"], "stage": "reviewed",
                     "verdict": r["verdict"], "review_notes": r.get("notes") or ""}
            verdicts[r["verdict"]] += 1
        elif d.key in p2:
            r = p2[d.key]
            entry = {"tags": r["tags"], "summary": r["summary"], "stage": "pass2",
                     "verdict": None, "review_notes": ""}
        elif d.key in p1:
            r = p1[d.key]
            entry = {"tags": mapped_tags(r.get("tags", []), aliases, canon), "summary": r.get("summary", ""),
                     "stage": "pass1", "verdict": None, "review_notes": ""}
        else:
            entry = {"tags": [], "summary": "", "stage": "none", "verdict": None, "review_notes": ""}
        p1r = p1.get(d.key, {})
        entry.update({
            "confidence": p1r.get("confidence"),
            "insufficient_docs": bool(p1r.get("insufficient_docs")) or d.thin,
            "doc_key": d.key,
            "shared_with": [m for m in d.members if m != d.id] if len(d.members) > 1 else [],
        })
        stage_counts[entry["stage"]] += 1
        for t in entry["tags"]:
            tag_counts[t] += 1
        for pid in d.members:
            projects[pid] = {**entry, "shared_with": [m for m in d.members if m != pid]}

    usage = usage_summary()
    spend = sum(m["cost"] for m in usage.values())
    out = {
        "generated_by": "ttsearch.ai (see ttsearch/ai/README.md)",
        "taxonomy": "data/ai/taxonomy.json",
        "stats": {
            "projects": len(projects), "unique_documents": len(docs),
            "documents_by_stage": dict(stage_counts), "review_verdicts": dict(verdicts),
            "tags_in_use": len(tag_counts), "total_spend_usd": round(spend, 4),
        },
        "tag_counts": dict(tag_counts.most_common()),
        "projects": dict(sorted(projects.items())),
    }
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}: {len(projects)} projects from {len(docs)} documents; stages={dict(stage_counts)}; "
          f"verdicts={dict(verdicts)}; total spend ${spend:.4f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
