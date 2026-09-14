# SPDX-License-Identifier: Apache-2.0
"""Pass two: classify every design with the canonical tags, keeping the summary.

    uv run tt-ai-pass2 --model deepseek/deepseek-v4-flash --reasoning off

Reads data/ai/taxonomy.json and the pass-one results. For each design the model
sees the source text, the pass-one summary and the alias-mapped pass-one tags
as a starting point, and must return 3 to 10 canonical tags (nothing outside
the vocabulary is accepted; stray tags are dropped) plus the summary, revised
only where it was wrong or not four sentences. Output per batch under
data/ai/pass2/<model-slug>/, resumable.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import AI_DIR
from .corpus import Doc, load_docs
from .openrouter import chat, print_usage
from .pass1 import _SENT, slug
from .taxonomy import TAXONOMY_JSON, normalise

BATCH = 10
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

SYSTEM_TMPL = """You classify open-source chip designs from Tiny Tapeout (small digital or analog
blocks on a shared chip with 8 inputs, 8 outputs and 8 bidirectional pins) using a FIXED
vocabulary of canonical tags. For each project you receive its documentation, pin names,
silicon test reports, a draft summary and draft tags produced earlier.

Return, for each project:
1. "tags": between 3 and 10 tags taken ONLY from the canonical vocabulary below, most
   specific first. Choose a tag only when the documentation or pin names support it. Do
   not tag an interface (uart, spi, i2c, vga, ...) unless the pins or text show it is
   actually used. Drop draft tags that are wrong or unsupported; add canonical tags the
   draft missed.
2. "summary": the draft summary, kept as is when it is accurate and exactly four
   sentences; otherwise rewritten as exactly four plain-English sentences in the third
   person covering what it does, how it is used or tested, external hardware, and what is
   notable. Never add facts the source does not support.
3. "changed": true if you changed the summary.

Canonical vocabulary (tag (category): meaning):
{vocab}

Return only the JSON object described by the schema, one entry per input project,
keyed by the project's "key" field exactly as given."""

SCHEMA = {
    "title": "pass2_batch",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 10},
                    "summary": {"type": "string"},
                    "changed": {"type": "boolean"},
                },
                "required": ["key", "tags", "summary", "changed"],
            },
        }
    },
    "required": ["results"],
}


def load_pass1(sub: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in sorted((AI_DIR / "pass1" / sub).rglob("*.json")):
        for r in json.loads(f.read_text()).get("results", []):
            if r.get("key"):
                out[r["key"]] = r
    return out


def load_taxonomy() -> tuple[dict[str, dict], dict[str, str | None]]:
    tax = json.loads(TAXONOMY_JSON.read_text())
    canon = {t["tag"]: t for t in tax["tags"]}
    return canon, tax["aliases"]


def mapped_tags(raw_tags: list[str], aliases: dict, canon: dict) -> list[str]:
    out = []
    for t in raw_tags:
        c = aliases.get(normalise(t))
        if c and c in canon and c not in out:
            out.append(c)
    return out


def batch_prompt(docs: list[Doc], p1: dict[str, dict], aliases: dict, canon: dict) -> str:
    parts = []
    for d in docs:
        r = p1.get(d.key, {})
        draft_tags = mapped_tags(r.get("tags", []), aliases, canon)
        parts.append(
            f'=== PROJECT key="{d.key}" ===\n{d.text}\n'
            f"--- draft tags (already mapped to the vocabulary): {', '.join(draft_tags) or '(none)'}\n"
            f"--- draft summary: {r.get('summary', '(none)')}\n"
        )
    return f"{len(docs)} projects follow.\n\n" + "\n".join(parts)


def run_batch(model: str, docs: list[Doc], p1: dict, aliases: dict, canon: dict, system: str,
              out_file: Path, tag: str, reasoning: str | None) -> dict:
    res = chat(model, system, batch_prompt(docs, p1, aliases, canon), schema=SCHEMA, tag=tag,
               max_tokens=900 * len(docs), reasoning=reasoning)
    problems: list[str] = []
    results: list[dict] = []
    try:
        data = json.loads(res.text)
        raw_results = data["results"] if isinstance(data, dict) else data
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        problems.append(f"unparseable response: {e}: {res.text[:200]!r}")
        raw_results = []
    want = {d.key for d in docs}
    seen = set()
    for r in raw_results:
        k = r.get("key")
        if k not in want:
            problems.append(f"unknown key {k!r}")
            continue
        tags = []
        for t in r.get("tags", []):
            c = normalise(str(t))
            c = aliases.get(c, c)
            if c in canon and c not in tags:
                tags.append(c)
            else:
                problems.append(f"{k}: dropped non-canonical tag {t!r}")
        n_sent = len([s for s in _SENT.split(r.get("summary", "").strip()) if s.strip()])
        if n_sent != 4:
            problems.append(f"{k}: {n_sent} sentences")
        if len(tags) < 3:
            problems.append(f"{k}: only {len(tags)} canonical tags")
        results.append({**r, "tags": tags, "sentences": n_sent})
        seen.add(k)
    for k in want - seen:
        problems.append(f"{k}: missing")
    record = {"model": res.model, "requested_model": model, "keys": [d.key for d in docs],
              "results": results, "problems": problems, "usage": res.usage,
              "cost_usd": res.cost, "elapsed_s": round(res.elapsed, 1)}
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--pass1-sub", default="full")
    ap.add_argument("--sub", default="full")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit-usd", type=float, default=0.8)
    ap.add_argument("--reasoning", choices=["off", "low", "medium", "high"], default="off")
    ap.add_argument("--limit-docs", type=int, default=None, help="only the first N documents (testing)")
    args = ap.parse_args(argv)

    canon, aliases = load_taxonomy()
    p1 = load_pass1(args.pass1_sub)
    docs = [d for d in load_docs() if d.key in p1]
    if args.limit_docs:
        docs = docs[: args.limit_docs]
    vocab = "\n".join(f"{t['tag']} ({t['category']}): {t['meaning']}" for t in canon.values())
    system = SYSTEM_TMPL.format(vocab=vocab)
    batches = [docs[i:i + args.batch] for i in range(0, len(docs), args.batch)]
    out_dir = AI_DIR / "pass2" / args.sub / slug(args.model)
    todo = [(i, b) for i, b in enumerate(batches) if not (out_dir / f"{i:04d}.json").exists()]
    print(f"{len(docs)} documents ({len(canon)} canonical tags, system prompt ~{len(system) // 4:,} tokens); "
          f"{len(todo)} of {len(batches)} batches to run", file=sys.stderr)

    spent = 0.0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(run_batch, args.model, b, p1, aliases, canon, system,
                             out_dir / f"{i:04d}.json", f"pass2/{args.sub}/{slug(args.model)}",
                             args.reasoning): i for i, b in todo}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i:04d} FAILED: {e}", file=sys.stderr)
                continue
            spent += rec["cost_usd"]
            print(f"  batch {i:04d} ok ${rec['cost_usd']:.4f} {rec['elapsed_s']}s"
                  + (f" problems={len(rec['problems'])}" if rec["problems"] else ""), file=sys.stderr)
            if spent > args.limit_usd:
                print(f"  spend limit ${args.limit_usd} reached, stopping", file=sys.stderr)
                for f in futures:
                    f.cancel()
                break
    print_usage("pass2/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
