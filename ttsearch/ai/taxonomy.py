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
DESIGN_CACHE = AI_DIR / "taxonomy.design.json"     # the designer's answer, kept so re-runs are free
MAP_CACHE = AI_DIR / "taxonomy.map"                # one file per mapping chunk
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
- Return ONLY the canonical tags with a one-line meaning each. Do not list aliases or raw
  spellings; a separate step maps raw tags onto your vocabulary.
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
                },
                "required": ["tag", "category", "meaning"],
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
    if len(t) > 4 and t.endswith("s") and not t.endswith(("ss", "us", "is", "os", "ics")):
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
    ap.add_argument("--model", default=DEFAULT_MODEL, help="model that designs the vocabulary")
    ap.add_argument("--map-model", default="deepseek/deepseek-v4.1-flash",
                    help="model that maps raw tags onto the vocabulary (easy, high volume)")
    ap.add_argument("--min-count", type=int, default=2,
                    help="raw tags used fewer times than this are only mapped, not shown to the designer")
    ap.add_argument("--out", type=Path, default=TAXONOMY_JSON)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reasoning", choices=["off", "low", "medium", "high"], default="off",
                    help="hidden reasoning effort; DeepSeek Pro spends its whole output budget thinking otherwise")
    ap.add_argument("--redesign", action="store_true", help="ignore the cached design and ask again")
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
    if DESIGN_CACHE.exists() and not args.redesign:
        cached = json.loads(DESIGN_CACHE.read_text())
        tax, design_model = cached["tags"], cached["model"]
        print(f"using cached design from {design_model}: {len(tax)} tags", file=sys.stderr)
        res = None
    else:
        res = chat(args.model, SYSTEM, user, schema=SCHEMA, tag="taxonomy/design", max_tokens=12000,
                   temperature=0.1, reasoning=args.reasoning)
        design_model = args.model
    try:
        if res is not None:
            tax = json.loads(res.text)["tags"]
    except (json.JSONDecodeError, KeyError) as e:
        # Salvage every complete {"tag","category","meaning"} object from a truncated reply.
        dump = AI_DIR / "taxonomy.raw.txt"
        dump.write_text(res.text)
        tax = []
        for m in re.finditer(r"\{[^{}]*\}", res.text):
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
            if {"tag", "category", "meaning"} <= obj.keys() and obj["category"] in dict(CATEGORIES):
                tax.append(obj)
        print(f"warning: design response was not valid JSON ({e}); salvaged {len(tax)} entries; "
              f"raw text in {dump}", file=sys.stderr)
        if len(tax) < 60:
            sys.exit("too few entries salvaged; aborting")
    if len(tax) > 250:
        print(f"warning: designer returned {len(tax)} tags, more than asked for", file=sys.stderr)
    if res is not None:
        AI_DIR.mkdir(parents=True, exist_ok=True)
        DESIGN_CACHE.write_text(json.dumps({"model": design_model, "tags": tax}, indent=1) + "\n")
        print(f"designer returned {len(tax)} canonical tags (${res.cost:.4f})", file=sys.stderr)
    canon: dict[str, dict] = {}
    aliases: dict[str, str | None] = {}
    for t in tax:
        name = normalise(t["tag"])
        if name in canon:
            continue
        canon[name] = {"tag": name, "category": t["category"], "meaning": t["meaning"].strip()}
        aliases[name] = name

    # Step 3: map every raw tag not yet covered, in chunks.
    unmapped = [t for t in counts if t not in aliases]
    unmapped.sort(key=lambda t: -counts[t])
    print(f"{len(unmapped)} raw tags still unmapped; asking the model to map them", file=sys.stderr)
    canon_list = "\n".join(f"{c['tag']} ({c['category']}): {c['meaning']}" for c in canon.values())
    canon_names = set(canon)

    def map_chunk(chunk: list[str], label: str) -> dict[str, str | None]:
        """Ask the mapping model for one chunk; salvage what parses; cache on disk."""
        cache_file = MAP_CACHE / f"{label}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())
        user = ("Canonical tags:\n" + canon_list + "\n\nRaw tags to map (tag count):\n"
                + "\n".join(f"{t} {counts[t]}" for t in chunk))
        res = chat(args.map_model, MAP_SYSTEM, user, schema=MAP_SCHEMA, tag="taxonomy/map",
                   max_tokens=16000, temperature=0.0, reasoning=args.reasoning)
        got: dict[str, str | None] = {}
        try:
            items = json.loads(res.text)["mappings"]
        except (json.JSONDecodeError, KeyError):
            items = []
            for m in re.finditer(r"\{[^{}]*\}", res.text):
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    continue
                if "raw" in obj and "canonical" in obj:
                    items.append(obj)
            print(f"    {label}: reply not valid JSON, salvaged {len(items)} of {len(chunk)}", file=sys.stderr)
        for m in items:
            raw = normalise(str(m.get("raw", "")))
            c = normalise(m["canonical"]) if m.get("canonical") else None
            if raw:
                got[raw] = c if c in canon_names else None
        print(f"    {label}: mapped {len(got)}/{len(chunk)} (${res.cost:.4f})", file=sys.stderr)
        MAP_CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(got, indent=0) + "\n")
        return got

    CHUNK = 250
    for i in range(0, len(unmapped), CHUNK):
        chunk = unmapped[i:i + CHUNK]
        aliases.update(map_chunk(chunk, f"c{i // CHUNK:03d}"))
    # Anything a truncated reply skipped gets one more try in smaller chunks.
    leftovers = [t for t in unmapped if t not in aliases]
    for j in range(0, len(leftovers), 100):
        chunk = leftovers[j:j + 100]
        aliases.update(map_chunk(chunk, f"retry{j // 100:03d}"))
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
        "model": design_model, "map_model": args.map_model,
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
