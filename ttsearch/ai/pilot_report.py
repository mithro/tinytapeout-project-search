# SPDX-License-Identifier: Apache-2.0
"""Compare pilot outputs from several models on the same sample.

    uv run python -m ttsearch.ai.pilot_report            # summary table + checks
    uv run python -m ttsearch.ai.pilot_report --show 5   # also print 5 docs side by side
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from . import AI_DIR
from .corpus import load_docs
from .openrouter import usage_summary

# Interface tags that should be backed by pin names or detected pinouts.
INTERFACE_WORDS = {
    "vga": ("vga", "hsync", "vsync", "rgb"), "uart": ("uart", "tx", "rx", "txd", "rxd", "serial"),
    "spi": ("spi", "mosi", "miso", "sck", "sclk", "cs"), "i2c": ("i2c", "sda", "scl"),
    "ps2": ("ps2", "ps/2"), "audio": ("audio", "pwm", "sound", "speaker", "i2s", "pdm", "dac"),
    "7-segment": ("seg", "7seg", "segment"), "gamepad": ("gamepad", "latch", "controller"),
}


def load_model(dir_: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(dir_.glob("*.json")):
        rec = json.loads(f.read_text())
        for r in rec["results"]:
            out[r["key"]] = r
    return out


def interface_mismatches(r: dict, doc) -> list[str]:
    hay = " ".join(doc.signals["pin_names"] + [doc.text[:400]]).lower()
    bad = []
    for tag in r["tags"]:
        for iface, words in INTERFACE_WORDS.items():
            if tag == iface or tag.startswith(iface + "-"):
                if not any(w in hay for w in words):
                    bad.append(tag)
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--sub", default="pilot")
    args = ap.parse_args(argv)

    docs = {d.key: d for d in load_docs()}
    base = AI_DIR / "pass1" / args.sub
    models = {p.name: load_model(p) for p in sorted(base.iterdir()) if p.is_dir()}
    usage = usage_summary(f"pass1/{args.sub}/")

    keys = sorted(set.intersection(*(set(m) for m in models.values()))) if models else []
    print(f"{len(models)} models, {len(keys)} documents scored by all\n")
    print(f"{'model':36} {'docs':>4} {'10tags':>6} {'4sent':>5} {'low':>4} {'thin✓':>5} {'iface?':>6} {'vocab':>5} {'$':>7} {'s/doc':>5}")
    for name, res in models.items():
        n = len(res)
        ten = sum(1 for r in res.values() if len(r["tags"]) == 10)
        four = sum(1 for r in res.values() if len([s for s in re.split(r"[.!?]+(?:\s|$)", r["summary"].strip()) if s.strip()]) == 4)
        low = sum(1 for r in res.values() if r.get("confidence") == "low")
        thin_ok = sum(1 for k, r in res.items() if k in docs and docs[k].thin and r.get("insufficient_docs"))
        thin_n = sum(1 for k in res if k in docs and docs[k].thin)
        iface = sum(len(interface_mismatches(r, docs[k])) for k, r in res.items() if k in docs)
        vocab = len({t for r in res.values() for t in r["tags"]})
        u = next((v for m, v in usage.items() if re.sub(r"[^a-z0-9]+", "-", m.lower()).strip("-") == name), {})
        cost = u.get("cost", 0.0)
        secs = 0.0
        for f in (base / name).glob("*.json"):
            secs += json.loads(f.read_text()).get("elapsed_s", 0)
        print(f"{name:36} {n:4} {ten:6} {four:5} {low:4} {thin_ok:3}/{thin_n:<2} {iface:6} {vocab:5} {cost:7.4f} {secs / max(n, 1):5.1f}")

    # Agreement between models: mean Jaccard of tag sets per doc.
    names = list(models)
    if len(names) > 1:
        print("\ntag overlap (mean Jaccard) between models:")
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                js = []
                for k in keys:
                    ta, tb = set(models[a][k]["tags"]), set(models[b][k]["tags"])
                    js.append(len(ta & tb) / len(ta | tb) if ta | tb else 1.0)
                print(f"  {a:36} vs {b:36} {sum(js) / len(js):.2f}")

    # Most common tags per model (spelling consistency check).
    print("\ntop tags per model:")
    for name, res in models.items():
        c = Counter(t for r in res.values() for t in r["tags"])
        print(f"  {name}: " + ", ".join(f"{t}({n})" for t, n in c.most_common(18)))

    if args.show:
        print()
        for k in keys[: args.show]:
            d = docs[k]
            print("=" * 100)
            print(f"{d.id}  ({len(d.members)} members, {d.chars} doc chars{', THIN' if d.thin else ''})  {d.title}")
            print(d.text[:600].replace("\n", " | "))
            for name, res in models.items():
                r = res[k]
                print(f"\n  [{name}] conf={r['confidence']} insufficient={r['insufficient_docs']}")
                print(f"  tags: {', '.join(r['tags'])}")
                print(f"  {r['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
