# SPDX-License-Identifier: Apache-2.0
"""Pass one: free-form tags and a four-sentence summary for every unique design.

    uv run tt-ai-pass1 --pilot 100 --model deepseek/deepseek-v4-flash --model google/gemini-2.5-flash-lite
    uv run tt-ai-pass1 --model deepseek/deepseek-v4-flash          # full run, resumable

Designs are sent in batches of BATCH docs per request. Output is written per
batch under data/ai/pass1/<model-slug>/<batch>.json, so an interrupted run
resumes where it stopped and nothing is paid for twice. Results are keyed by
the document key; resubmitted designs (same docs on later shuttles) share one
key and therefore one result.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import AI_DIR
from .corpus import Doc, load_docs
from .openrouter import chat, print_usage

BATCH = 10
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"

# Starter vocabulary: Tiny Tapeout's own suggested tags from the info.yaml
# template, plus the interface/output words this corpus uses constantly. The
# model may use other tags too; this just keeps spelling consistent.
SEED_TAGS = (
    "alu, cpu, risc-v, microcontroller, soc, fpga, cpld, lut, memory, sram, rom, fifo, register-file, "
    "vga, video, hdmi, display, 7-segment, led, ws2812, oled, lcd, "
    "uart, spi, i2c, qspi, ps2, usb, can, gpio, serial, "
    "audio, pwm, i2s, pdm, synthesizer, music, sound, "
    "adc, dac, analog, comparator, opamp, bandgap, ldo, oscillator, ring-oscillator, pll, vco, temperature-sensor, "
    "crypto, aes, sha, hash, encryption, prng, lfsr, trng, random-number-generator, "
    "neural-network, machine-learning, matrix-multiply, dsp, fft, filter, cordic, multiplier, divider, floating-point, "
    "game, pong, tetris, snake, puzzle, animation, demo, "
    "counter, timer, clock, rtc, stopwatch, calculator, bcd, decoder, encoder, state-machine, sequence-detector, "
    "test, experiment, education, wokwi, template, factory-test, "
    "motor-control, servo, robotics, sensor, industrial, communication, protocol, debug, scan-chain, "
    "power-gating, radiation, standard-cells, analog-frontend, transistor-level"
)

SYSTEM = f"""You tag and summarise open-source chip designs from Tiny Tapeout, a community
shuttle service where each project is a small digital or analog block on a shared chip with
8 inputs, 8 outputs and 8 bidirectional pins.

For each project you receive its title, description, pin names, documentation and any
silicon test reports. Produce, for each project:

1. "tags": exactly 10 lowercase tags, most specific first. Use single words or
   hyphenated phrases (no spaces). Prefer these spellings when they apply:
   {SEED_TAGS}.
   Cover what the design IS (e.g. cpu, alu, game), what it TALKS TO (interfaces such as
   uart, spi, i2c, vga), what it is FOR (e.g. education, crypto, dsp), and how it was
   made when notable (e.g. wokwi, analog, transistor-level). Do not tag the shuttle,
   process node or author. Do not repeat a tag. Do not invent capabilities that the
   text and pins do not support.
2. "summary": exactly 4 sentences of plain English, in the third person, that say what the
   design does, how it is used or tested, what external hardware it needs (if any), and
   what is notable about it (silicon results, limitations, origin such as a class project).
   No marketing tone. If the documentation is too thin to say something, say so in the
   summary instead of guessing.
3. "confidence": "high" if the documentation clearly supports the tags and summary,
   "medium" if partly inferred from pin names or the title, "low" if mostly guessed.
4. "insufficient_docs": true when the project has essentially no documentation.

Return only the JSON object described by the schema, with one entry per input project,
keyed by the project's "key" field exactly as given."""

RESULT_SCHEMA = {
    "title": "pass1_batch",
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
                    "tags": {"type": "array", "items": {"type": "string"}, "minItems": 10, "maxItems": 10},
                    "summary": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "insufficient_docs": {"type": "boolean"},
                },
                "required": ["key", "tags", "summary", "confidence", "insufficient_docs"],
            },
        }
    },
    "required": ["results"],
}


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def batch_prompt(docs: list[Doc]) -> str:
    parts = []
    for d in docs:
        parts.append(f'=== PROJECT key="{d.key}" ===\n{d.text}\n')
    return f"{len(docs)} projects follow.\n\n" + "\n".join(parts)


_SENT = re.compile(r"[.!?]+(?:\s|$)")


def validate(results: list[dict], docs: list[Doc]) -> tuple[dict[str, dict], list[str]]:
    """Return (results by key, problems)."""
    want = {d.key for d in docs}
    out: dict[str, dict] = {}
    problems: list[str] = []
    for r in results:
        k = r.get("key")
        if k not in want:
            problems.append(f"unknown key {k!r}")
            continue
        tags = [t.strip().lower() for t in r.get("tags", []) if isinstance(t, str) and t.strip()]
        tags = list(dict.fromkeys(tags))          # dedupe, keep order
        if len(tags) != 10:
            problems.append(f"{k}: {len(tags)} unique tags")
        n_sent = len([s for s in _SENT.split(r.get("summary", "").strip()) if s.strip()])
        if n_sent != 4:
            problems.append(f"{k}: {n_sent} sentences")
        out[k] = {**r, "tags": tags, "sentences": n_sent}
    for k in want - out.keys():
        problems.append(f"{k}: missing")
    return out, problems


def run_batch(model: str, docs: list[Doc], out_file: Path, tag: str,
              reasoning: str | None = None) -> dict:
    res = chat(model, SYSTEM, batch_prompt(docs), schema=RESULT_SCHEMA, tag=tag,
               max_tokens=1200 * len(docs), reasoning=reasoning)
    try:
        data = json.loads(res.text)
        results = data["results"] if isinstance(data, dict) else data
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        results, problems = [], [f"unparseable response: {e}: {res.text[:200]!r}"]
    else:
        _, problems = validate(results, docs)
    record = {
        "model": res.model, "requested_model": model, "keys": [d.key for d in docs],
        "results": results, "problems": problems, "usage": res.usage, "cost_usd": res.cost,
        "elapsed_s": round(res.elapsed, 1),
    }
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    return record


def pick_pilot(docs: list[Doc], n: int, seed: int = 42) -> list[Doc]:
    """A reproducible sample: ~10% thin docs, ~20% resubmitted designs, the rest random."""
    rng = random.Random(seed)
    thin = [d for d in docs if d.thin]
    multi = [d for d in docs if len(d.members) > 1 and not d.thin]
    rest = [d for d in docs if not d.thin and len(d.members) == 1]
    chosen = rng.sample(thin, min(len(thin), n // 10)) + rng.sample(multi, min(len(multi), n // 5))
    chosen += rng.sample(rest, n - len(chosen))
    rng.shuffle(chosen)
    return chosen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", action="append", help="OpenRouter model id (repeatable for pilots)")
    ap.add_argument("--pilot", type=int, metavar="N", help="only run a fixed sample of N designs")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--limit-usd", type=float, default=1.0, help="stop when this run's spend exceeds this")
    ap.add_argument("--dry-run", action="store_true", help="print batch plan and token estimate only")
    ap.add_argument("--reasoning", choices=["off", "low", "medium", "high"], default=None,
                    help="reasoning effort for models that think (off saves most of their output cost)")
    ap.add_argument("--sub", default=None, help="output sub-directory name (default: pilot or full)")
    args = ap.parse_args(argv)
    models = args.model or [DEFAULT_MODEL]

    docs = load_docs()
    if args.pilot:
        docs = pick_pilot(docs, args.pilot)
    batches = [docs[i:i + args.batch] for i in range(0, len(docs), args.batch)]
    chars = sum(len(d.text) for d in docs)
    print(f"{len(docs)} documents in {len(batches)} batches of {args.batch}; "
          f"~{chars // 4:,} input tokens per model", file=sys.stderr)
    if args.dry_run:
        return 0

    spent = 0.0
    for model in models:
        sub = args.sub or ("pilot" if args.pilot else "full")
        out_dir = AI_DIR / "pass1" / sub / slug(model)
        todo = [(i, b) for i, b in enumerate(batches) if not (out_dir / f"{i:04d}.json").exists()]
        print(f"[{model}] {len(todo)} batches to run ({len(batches) - len(todo)} already done)", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futures = {ex.submit(run_batch, model, b, out_dir / f"{i:04d}.json",
                                 f"pass1/{sub}/{slug(model)}", args.reasoning): i
                       for i, b in todo}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001 - report and keep going
                    print(f"  batch {i:04d} FAILED: {e}", file=sys.stderr)
                    continue
                spent += rec["cost_usd"]
                probs = rec["problems"]
                print(f"  batch {i:04d} ok ${rec['cost_usd']:.4f} {rec['elapsed_s']}s"
                      + (f" problems={len(probs)}" if probs else ""), file=sys.stderr)
                if spent > args.limit_usd:
                    print(f"  spend limit ${args.limit_usd} reached, stopping", file=sys.stderr)
                    for f in futures:
                        f.cancel()
                    break
    print("usage this run:", file=sys.stderr)
    print_usage()
    return 0


if __name__ == "__main__":
    sys.exit(main())
