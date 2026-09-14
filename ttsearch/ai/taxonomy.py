# SPDX-License-Identifier: Apache-2.0
"""Consolidate the free-form pass-one tags into a canonical tag set.

    uv run tt-ai-taxonomy --sub full --model deepseek/deepseek-v4-pro-0813

Step 1 (deterministic): normalise spelling (lowercase, hyphens, simple
plurals) and count how often each raw tag was used.
Step 2 (one model call): given the frequency table, propose canonical tags in
categories, each with a one-line meaning and the raw spellings it absorbs.
Step 3 (deterministic + one small call): map every raw tag to a canonical tag
or "drop"; raw tags the first call did not cover are sent back in a second
call with the canonical list, so the alias map is complete.

Output: data/ai/taxonomy.json with "tags" (canonical, grouped) and "aliases"
(raw -> canonical or null for dropped). Review this file before pass two.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from . import AI_DIR
from .openrouter import chat, print_usage

TAXONOMY_JSON = AI_DIR / "taxonomy.json"
DEFAULT_MODEL = "deepseek/deepseek-v4-pro-0813"

CATEGORIES = [
    ("type", "what the design is: cpu, alu, game, counter, oscillator, neural-network, ..."),
    ("interface", "what it talks to: uart, spi, i2c, vga, ps2, gamepad, ..."),
    ("output", "what it drives: 7-segment, led, vga, audio, pwm, ..."),
    ("domain", "what it is for: crypto, dsp, education, robotics, communication, ..."),
    ("impl", "how it was made or what is notable: analog, wokwi, transistor-level, multi-tile, silicon-tested, ..."),
]

SYSTEM = """You curate a tag vocabulary for a catalogue of about 4,800 small open-source chip
designs (Tiny Tapeout projects). Cheap models tagged every design with free-form tags; you
receive the frequency table of those raw tags. Design a canonical tag set that a person
browsing the catalogue would find useful for filtering.

Rules:
- Aim for roughly 120 to 200 canonical tags. Merge synonyms and spelling variants
  (risc-v / riscv / rv32 -> risc-v; 7-segment / seven-segment / 7seg -> 7-segment).
- Drop tags that carry no information for browsing (verilog, digital, hardware, chip,
  design, project, test, demo, experiment, simple, basic, custom) unless they name a
  real category such as factory-test or process-experiment.
- Keep tags that are rare but meaningful (e.g. bandgap, cordic, sigma-delta), since a
  rare specific tag is more useful than a common vague one.
- Every canonical tag is lowercase, single word or hyphenated, unique across categories.
- Assign each canonical tag to exactly one category: type, interface, output, domain, impl.
- For each canonical tag list ALL raw spellings from the table that should map to it.
  Raw tags not listed anywhere are treated as dropped, so be thorough with variants.
- Return only JSON matching the schema."""

SCHEMA = {
    "title": "taxonomy",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tag": {"type": "string"},
                    "category": {"type": "string", "enum": [c for c, _ in CATEGORIES]},
                    "meaning": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["tag", "category", "meaning", "aliases"],
            },
        }
    },
    "required": ["tags"],
}

MAP_SYSTEM = """You map raw tags onto a fixed canonical tag vocabulary. For each raw tag return the
best canonical tag, or null if none fits or the raw tag carries no useful information.
Do not invent new canonical tags. Return only JSON matching the schema."""

MAP_SCHEMA = {
    "title": "alias_map",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "raw": {"type": "string"},
                    "canonical": {"type": ["string", "null"]},
                },
                "required": ["raw", "canonical"],
            },
        }
    },
    "required": ["mappings"],
}


def normalise(tag: str) -> str:
    t = tag.strip().lower()
    t = re.sub(r"[\s_/]+", "-", t)
    t = re.sub(r"[^a-z0-9+#.-]", "", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    # Simple plurals: counters -> counter, but not "bus", "analysis", "ss".
    if len(t) > 4 and t.endswith("s") and not t.endswith(("ss", "us", "is", "os")):
        t = t[:-1]
    return t


def load_raw_tags(sub: str) -> Counter:
    base = AI_DIR / "pass1" / sub
    counts: Counter = Counter()
    for f in base.rglob("*.json"):
        rec = json.loads(f.read_text())
        for r in rec.get("results", []):
            for t in r.get("tags", []):
                if isinstance(t, str) and t.strip():
                    counts[normalise(t)] += 1
    counts.pop("", None)
    return counts


def freq_table(counts: Counter, min_count: int) -> str:
    rows = [f"{t} {n}" for t, n in counts.most_common() if n >= min_count]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sub", default="full", help="pass-one output sub-directory to read")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--min-count", type=int, default=2,
                    help="raw tags used fewer times than this are only mapped, not shown to the designer")
    ap.add_argument("--out", type=Path, default=TAXONOMY_JSON)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    counts = load_raw_tags(args.sub)
    shown = {t for t, n in counts.items() if n >= args.min_count}
    print(f"{sum(counts.values()):,} tag uses, {len(counts):,} distinct raw tags, "
          f"{len(shown):,} used >= {args.min_count} times", file=sys.stderr)
    if args.dry_run:
        print(freq_table(counts, args.min_count)[:3000])
        return 0

    # Step 2: design the canonical set from the frequent tags.
    user = ("Raw tag frequency table (tag count), most common first:\n\n"
            + freq_table(counts, args.min_count)
            + "\n\nCategories: " + "; ".join(f"{c}: {d}" for c, d in CATEGORIES))
    res = chat(args.model, SYSTEM, user, schema=SCHEMA, tag="taxonomy/design", max_tokens=32000,
               temperature=0.1)
    tax = json.loads(res.text)["tags"]
    canon: dict[str, dict] = {}
    aliases: dict[str, str | None] = {}
    for t in tax:
        name = normalise(t["tag"])
        if name in canon:
            continue
        canon[name] = {"tag": name, "category": t["category"], "meaning": t["meaning"].strip()}
        aliases[name] = name
        for a in t.get("aliases", []):
            aliases.setdefault(normalise(a), name)
    print(f"designer returned {len(canon)} canonical tags covering {len(aliases)} raw spellings "
          f"(${res.cost:.4f})", file=sys.stderr)

    # Step 3: map every raw tag not yet covered, in chunks.
    unmapped = [t for t in counts if t not in aliases]
    unmapped.sort(key=lambda t: -counts[t])
    print(f"{len(unmapped)} raw tags still unmapped; asking the model to map them", file=sys.stderr)
    canon_list = "\n".join(f"{c['tag']} ({c['category']}): {c['meaning']}" for c in canon.values())
    for i in range(0, len(unmapped), 300):
        chunk = unmapped[i:i + 300]
        user = ("Canonical tags:\n" + canon_list + "\n\nRaw tags to map (tag count):\n"
                + "\n".join(f"{t} {counts[t]}" for t in chunk))
        res = chat(args.model, MAP_SYSTEM, user, schema=MAP_SCHEMA, tag="taxonomy/map",
                   max_tokens=16000, temperature=0.0)
        for m in json.loads(res.text)["mappings"]:
            raw = normalise(m["raw"])
            c = normalise(m["canonical"]) if m.get("canonical") else None
            aliases[raw] = c if c in canon else None
        print(f"  mapped {min(i + 300, len(unmapped))}/{len(unmapped)} (${res.cost:.4f})", file=sys.stderr)
    for t in unmapped:
        aliases.setdefault(t, None)

    # Usage stats per canonical tag, for review.
    canon_counts: Counter = Counter()
    for raw, c in aliases.items():
        if c:
            canon_counts[c] += counts.get(raw, 0)
    for c in canon.values():
        c["uses"] = canon_counts[c["tag"]]
    dropped = sum(counts[r] for r, c in aliases.items() if c is None)
    out = {
        "model": args.model,
        "source": f"data/ai/pass1/{args.sub}",
        "categories": dict(CATEGORIES),
        "tags": sorted(canon.values(), key=lambda c: (c["category"], -c["uses"], c["tag"])),
        "aliases": dict(sorted(aliases.items())),
        "stats": {"raw_distinct": len(counts), "raw_uses": sum(counts.values()),
                  "canonical": len(canon), "dropped_uses": dropped},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}: {len(canon)} canonical tags; {dropped:,} of {sum(counts.values()):,} "
          f"tag uses dropped as uninformative", file=sys.stderr)
    print_usage("taxonomy/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
