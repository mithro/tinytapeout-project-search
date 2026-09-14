# SPDX-License-Identifier: Apache-2.0
"""Review pass: a stronger model checks every design's tags and summary.

    uv run tt-ai-review --model deepseek/deepseek-v4-pro-0813

For each design the reviewer sees the source text, deterministic facts (pin
names, detected pinouts, language, silicon status) and the pass-two tags and
summary, and returns either an approval or corrections: tags to remove (with a
reason), tags to add from the vocabulary, and a corrected summary when a
sentence is unsupported. Anything the reviewer is unsure about is marked
"escalate" for a second opinion. Output per batch under data/ai/review/<model>/.
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
from .pass2 import load_pass1, load_taxonomy, mapped_tags
from .pilot_report import interface_mismatches
from .taxonomy import normalise

BATCH = 6
DEFAULT_MODEL = "deepseek/deepseek-v4-pro-0813"

SYSTEM_TMPL = """You are the reviewer of an automatically generated catalogue of open-source chip
designs (Tiny Tapeout projects: small digital or analog blocks with 8 inputs, 8 outputs
and 8 bidirectional pins). A cheaper model chose tags from a fixed vocabulary and wrote a
four-sentence summary for each design. Your job is to find mistakes and incorrect
attribution, not to restyle good output.

For each project you get the source documentation, pin names, deterministic facts
(interfaces detected from the pins, language, silicon test status) and the proposed
tags and summary. Check:
- Every tag is supported by the source. Interface and output tags need evidence in the
  pins or text. A CPU is only "risc-v" if the text says so. "silicon-tested" style
  claims must match the silicon status given.
- Nothing important and clearly stated is missing (add canonical tags only).
- Every sentence of the summary is supported by the source; no invented hardware,
  performance figures, test results or purposes. Exactly four sentences.

Return for each project:
- "verdict": "approve" (no change), "fix" (you supply corrections), or "escalate" (the
  source is ambiguous or contradictory and a second opinion is needed; still supply your
  best corrections).
- "remove": tags to remove, each with a short reason.
- "add": canonical tags to add, each with a short reason (from the vocabulary only).
- "summary": the corrected four-sentence summary, or the original if unchanged.
- "notes": one short sentence for the log when verdict is not "approve", else "".

Canonical vocabulary (tag (category): meaning):
{vocab}

Return only the JSON object described by the schema, one entry per input project, in the
same order as the input, with "index" set to the project's number (1 for the first project)
and "key" copied exactly as given."""

SCHEMA = {
    "title": "review_batch",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "index": {"type": "integer"},
                    "key": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["approve", "fix", "escalate"]},
                    "remove": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                               "properties": {"tag": {"type": "string"}, "reason": {"type": "string"}},
                               "required": ["tag", "reason"]}},
                    "add": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                            "properties": {"tag": {"type": "string"}, "reason": {"type": "string"}},
                            "required": ["tag", "reason"]}},
                    "summary": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["index", "key", "verdict", "remove", "add", "summary", "notes"],
            },
        }
    },
    "required": ["results"],
}


def load_pass2(sub: str, model_slug: str | None = None) -> dict[str, dict]:
    base = AI_DIR / "pass2" / sub
    out: dict[str, dict] = {}
    for f in sorted(base.rglob("*.json")):
        if model_slug and f.parent.name != model_slug:
            continue
        rec = json.loads(f.read_text())
        batch_keys = rec.get("keys") or []
        for r in rec.get("results", []):
            key = r.get("key")
            idx = r.get("index")
            # Earlier runs stored index-matched results under the model's garbled
            # key; the batch's key list plus the index recovers the real one.
            if key not in batch_keys and isinstance(idx, int) and 1 <= idx <= len(batch_keys):
                key = batch_keys[idx - 1]
            if key in batch_keys and len(r.get("tags", [])) >= 3:
                out[key] = {**r, "key": key}
    return out


def facts(d: Doc) -> str:
    s = d.signals
    from ..pmods import PMOD_BY_ID
    bits = []
    if s.get("language"):
        bits.append(f"language={s['language']}")
    bits.append("pinouts_detected=" + (", ".join(PMOD_BY_ID[m].name for m in s.get("pmods", [])) or "none"))
    bits.append(f"analog_pins={'yes' if s.get('analog') else 'no'}")
    bits.append(f"silicon_status={s.get('test_status') or 'no reports'}")
    bits.append(f"documentation={'very thin' if d.thin else 'present'}")
    bits.append(f"resubmitted_on={len(d.members)} shuttle(s)")
    return "; ".join(bits)


def risk_score(d: Doc, p1: dict, p2: dict, aliases: dict, canon: dict) -> float:
    """How likely this entry is to contain a mistake; higher first for review.

    Disagreement between the two cheap passes, low self-reported confidence,
    interface tags without evidence in the pins, changed or malformed
    summaries: these are where the pilot showed errors cluster.
    """
    r1 = p1.get(d.key, {})
    r2 = p2[d.key]
    t1 = set(mapped_tags(r1.get("tags", []), aliases, canon))
    t2 = set(r2["tags"])
    jaccard = len(t1 & t2) / len(t1 | t2) if t1 | t2 else 1.0
    score = 1.0 - jaccard
    score += {"high": 0.0, "medium": 0.4, "low": 0.8}.get(r1.get("confidence"), 0.4)
    score += 0.6 * len(interface_mismatches({"tags": list(t2)}, d))
    score += 0.3 if r2.get("changed") else 0.0
    score += 0.3 if r2.get("sentences") != 4 else 0.0
    score += 0.2 * (len(d.members) - 1) ** 0.5     # resubmitted designs: a mistake is copied
    return score


def batch_prompt(docs: list[Doc], p2: dict[str, dict]) -> str:
    parts = []
    for i, d in enumerate(docs, 1):
        r = p2[d.key]
        parts.append(
            f'=== PROJECT index={i} key="{d.key}" ===\n{d.text}\n'
            f"--- facts: {facts(d)}\n"
            f"--- proposed tags: {', '.join(r['tags'])}\n"
            f"--- proposed summary: {r['summary']}\n"
        )
    return f"{len(docs)} projects follow.\n\n" + "\n".join(parts)


def run_batch(model: str, docs: list[Doc], p2: dict, canon: dict, aliases: dict, system: str,
              out_file: Path, tag: str, reasoning: str | None) -> dict:
    res = chat(model, system, batch_prompt(docs, p2), schema=SCHEMA, tag=tag,
               max_tokens=900 * len(docs), reasoning=reasoning)
    problems: list[str] = []
    results: list[dict] = []
    try:
        data = json.loads(res.text)
        raw = data["results"] if isinstance(data, dict) else data
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        problems.append(f"unparseable response: {e}: {res.text[:200]!r}")
        raw = []
    want = {d.key: d for d in docs}
    by_index = {i: d.key for i, d in enumerate(docs, 1)}
    seen = set()
    for r in raw:
        k = r.get("key")
        idx = r.get("index")
        if isinstance(idx, int) and idx in by_index and by_index[idx] not in seen:
            if k != by_index[idx]:
                problems.append(f"{by_index[idx]}: key echoed as {k!r}, matched by index")
            k = by_index[idx]
        if k not in want or k in seen:
            problems.append(f"unknown key {k!r}")
            continue
        before = list(p2[k]["tags"])
        tags = list(before)
        for item in r.get("remove", []):
            t = aliases.get(normalise(item["tag"]), normalise(item["tag"]))
            if t in tags:
                tags.remove(t)
        for item in r.get("add", []):
            t = aliases.get(normalise(item["tag"]), normalise(item["tag"]))
            if t in canon and t not in tags:
                tags.append(t)
            elif t not in canon:
                problems.append(f"{k}: reviewer added non-canonical tag {item['tag']!r}")
        summary = (r.get("summary") or p2[k]["summary"]).strip()
        n_sent = len([s for s in _SENT.split(summary) if s.strip()])
        if n_sent != 4:
            problems.append(f"{k}: {n_sent} sentences after review")
        if len(tags) < 2:
            problems.append(f"{k}: only {len(tags)} tags after review; keeping pass-two tags")
            tags = before
        results.append({
            "key": k, "verdict": r.get("verdict", "approve"), "remove": r.get("remove", []),
            "add": r.get("add", []), "notes": r.get("notes", ""),
            "tags_before": before, "tags": tags, "summary_before": p2[k]["summary"],
            "summary": summary, "summary_changed": summary != p2[k]["summary"].strip(),
        })
        seen.add(k)
    for k in want.keys() - seen:
        problems.append(f"{k}: missing")
    record = {"model": res.model, "requested_model": model, "keys": list(want),
              "results": results, "problems": problems, "usage": res.usage,
              "cost_usd": res.cost, "elapsed_s": round(res.elapsed, 1)}
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--pass2-sub", default="full")
    ap.add_argument("--sub", default="full")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit-usd", type=float, default=2.5)
    ap.add_argument("--reasoning", choices=["off", "low", "medium", "high"], default="low")
    ap.add_argument("--limit-docs", type=int, default=None)
    ap.add_argument("--repair", action="store_true",
                    help="re-run batches whose saved record has missing, unknown-key or unparseable results")
    ap.add_argument("--skip-reviewed", action="store_true",
                    help="leave out documents that already have a review from any model (for a second, cheaper reviewer)")
    ap.add_argument("--order", choices=["risk", "corpus"], default="risk",
                    help="risk: most error-prone entries first (so a spend limit reviews the ones that matter)")
    args = ap.parse_args(argv)

    canon, aliases = load_taxonomy()
    p1 = load_pass1(args.pass2_sub)
    p2 = load_pass2(args.pass2_sub)
    docs = [d for d in load_docs() if d.key in p2]
    if args.skip_reviewed:
        done: set[str] = set()
        for f in (AI_DIR / "review" / args.sub).rglob("*.json"):
            for r in json.loads(f.read_text()).get("results", []):
                done.add(r.get("key"))
        docs = [d for d in docs if d.key not in done]
        print(f"skipping {len(done)} already-reviewed documents", file=sys.stderr)
    if args.order == "risk":
        docs.sort(key=lambda d: -risk_score(d, p1, p2, aliases, canon))
    if args.limit_docs:
        docs = docs[: args.limit_docs]
    vocab = "\n".join(f"{t['tag']} ({t['category']}): {t['meaning']}" for t in canon.values())
    system = SYSTEM_TMPL.format(vocab=vocab)
    batches = [docs[i:i + args.batch] for i in range(0, len(docs), args.batch)]
    out_dir = AI_DIR / "review" / args.sub / slug(args.model)

    def needs_run(i: int) -> bool:
        f = out_dir / f"{i:04d}.json"
        if not f.exists():
            return True
        if not args.repair:
            return False
        probs = json.loads(f.read_text()).get("problems", [])
        return any(("missing" in p or "unparseable" in p or "unknown key" in p) for p in probs)

    todo = [(i, b) for i, b in enumerate(batches) if needs_run(i)]
    print(f"{len(docs)} documents; {len(todo)} of {len(batches)} batches to run", file=sys.stderr)

    spent = 0.0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(run_batch, args.model, b, p2, canon, aliases, system,
                             out_dir / f"{i:04d}.json", f"review/{args.sub}/{slug(args.model)}",
                             args.reasoning): i for i, b in todo}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i:04d} FAILED: {e}", file=sys.stderr)
                continue
            spent += rec["cost_usd"]
            verdicts = [r["verdict"] for r in rec["results"]]
            print(f"  batch {i:04d} ok ${rec['cost_usd']:.4f} {rec['elapsed_s']}s "
                  f"approve={verdicts.count('approve')} fix={verdicts.count('fix')} "
                  f"escalate={verdicts.count('escalate')}"
                  + (f" problems={len(rec['problems'])}" if rec["problems"] else ""), file=sys.stderr)
            if spent > args.limit_usd:
                print(f"  spend limit ${args.limit_usd} reached, stopping", file=sys.stderr)
                for f in futures:
                    f.cancel()
                break
    print_usage("review/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
