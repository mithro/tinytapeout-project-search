# SPDX-License-Identifier: Apache-2.0
"""Apply hand edits from data/ai/taxonomy_overrides.json to data/ai/taxonomy.json.

    uv run tt-ai-taxonomy-fix

Idempotent: re-running applies the same overrides again. Use counts are
recomputed from the pass-one raw tags after the edits.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import AI_DIR
from .taxonomy import TAXONOMY_JSON, load_raw_tags, normalise

OVERRIDES_JSON = AI_DIR / "taxonomy_overrides.json"


def apply(tax: dict, ov: dict, counts: Counter) -> dict:
    canon = {t["tag"]: t for t in tax["tags"]}
    aliases: dict[str, str | None] = dict(tax["aliases"])

    for t in ov.get("add", []):
        name = normalise(t["tag"])
        canon[name] = {"tag": name, "category": t["category"], "meaning": t["meaning"].strip(), "uses": 0}
        aliases[name] = name
        for a in t.get("aliases", []):
            aliases[normalise(a)] = name

    for name in ov.get("remove", []):
        name = normalise(name)
        canon.pop(name, None)
        for raw, c in list(aliases.items()):
            if c == name:
                aliases[raw] = None

    for raw, target in ov.get("remap", {}).items():
        raw, target = normalise(raw), normalise(target)
        if target not in canon:
            sys.exit(f"remap target {target!r} is not a canonical tag")
        aliases[raw] = target
        for r, c in list(aliases.items()):
            if c == raw:
                aliases[r] = target

    # Any raw tag that is itself a canonical name maps to itself.
    for name in canon:
        aliases[name] = name

    canon_counts: Counter = Counter()
    for raw, c in aliases.items():
        if c:
            canon_counts[c] += counts.get(raw, 0)
    for c in canon.values():
        c["uses"] = canon_counts[c["tag"]]
    tax["tags"] = sorted(canon.values(), key=lambda c: (c["category"], -c["uses"], c["tag"]))
    tax["aliases"] = dict(sorted(aliases.items()))
    tax["stats"]["canonical"] = len(canon)
    tax["stats"]["dropped_uses"] = sum(counts[r] for r, c in aliases.items() if c is None and r in counts)
    tax["overrides_applied"] = True
    return tax


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sub", default="full")
    ap.add_argument("--taxonomy", type=Path, default=TAXONOMY_JSON)
    ap.add_argument("--overrides", type=Path, default=OVERRIDES_JSON)
    args = ap.parse_args(argv)
    tax = json.loads(args.taxonomy.read_text())
    ov = json.loads(args.overrides.read_text())
    before = len(tax["tags"])
    tax = apply(tax, ov, load_raw_tags(args.sub))
    args.taxonomy.write_text(json.dumps(tax, indent=1, ensure_ascii=False) + "\n")
    print(f"{before} -> {len(tax['tags'])} canonical tags; added {len(ov.get('add', []))}, "
          f"removed {len(ov.get('remove', []))}, remapped {len(ov.get('remap', {}))}; "
          f"{tax['stats']['dropped_uses']:,} uses now dropped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
