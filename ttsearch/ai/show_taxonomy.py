# SPDX-License-Identifier: Apache-2.0
"""Print data/ai/taxonomy.json compactly for review.

    uv run python -m ttsearch.ai.show_taxonomy            # tags by category with use counts
    uv run python -m ttsearch.ai.show_taxonomy --dropped  # also the most-used dropped raw tags
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from .taxonomy import TAXONOMY_JSON, load_raw_tags


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dropped", action="store_true")
    ap.add_argument("--sub", default="full")
    args = ap.parse_args(argv)
    tax = json.loads(TAXONOMY_JSON.read_text())
    st = tax["stats"]
    print(f"{st['canonical']} canonical tags; {st['raw_distinct']:,} raw tags, {st['raw_uses']:,} uses, "
          f"{st['dropped_uses']:,} uses dropped ({100 * st['dropped_uses'] / st['raw_uses']:.0f}%)\n")
    by_cat: dict[str, list] = {}
    for t in tax["tags"]:
        by_cat.setdefault(t["category"], []).append(t)
    for cat, desc in tax["categories"].items():
        tags = by_cat.get(cat, [])
        print(f"== {cat} ({len(tags)}): {desc}")
        line = "  "
        for t in tags:
            item = f"{t['tag']}({t['uses']})"
            if len(line) + len(item) > 110:
                print(line)
                line = "  "
            line += item + "  "
        print(line.rstrip())
        print()
    if args.dropped:
        counts = load_raw_tags(args.sub)
        dropped = Counter({r: counts[r] for r, c in tax["aliases"].items() if c is None and r in counts})
        print("most-used dropped raw tags:")
        print("  " + ", ".join(f"{t}({n})" for t, n in dropped.most_common(60)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
