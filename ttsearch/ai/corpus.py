# SPDX-License-Identifier: Apache-2.0
"""Build the text the tagging models read, one document per unique design.

Many designs are resubmitted on later shuttles with identical documentation.
Those are grouped so the model reads each design once and the result is
copied to every member. Projects with almost no documentation are flagged so
the model is not asked to invent a summary for them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .. import PROJECTS_JSON
from ..feedback import FEEDBACK_JSON
from ..pmods import PMOD_BY_ID, detect_pmods

THIN_DOC_CHARS = 200          # below this, docs are too thin to summarise honestly
MAX_FIELD_CHARS = 6000        # cap very long sections (a few projects paste whole datasheets)


@dataclass
class Doc:
    key: str                  # hash of the documentation; members share it
    members: list[str]        # "shuttle/macro" ids sharing this documentation
    title: str
    text: str                 # what the model reads
    chars: int
    thin: bool
    signals: dict = field(default_factory=dict)   # deterministic facts for the reviewer

    @property
    def id(self) -> str:
        return self.members[0]


def _clip(s: str | None, n: int = MAX_FIELD_CHARS) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …[truncated]"


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def render(p: dict, reports: list[dict]) -> tuple[str, dict]:
    """Return (text for the model, deterministic signals)."""
    pinout = p.get("pinout") or {}
    pins = [f"{k}: {v.strip()}" for k, v in pinout.items() if isinstance(v, str) and v.strip()]
    pmods = detect_pmods(pinout)
    lines = [f"Title: {p.get('title') or p['macro']}"]
    if p.get("description"):
        lines.append(f"One-line description: {_clip(p['description'], 500)}")
    if p.get("language"):
        lines.append(f"Language: {p['language']}")
    if p.get("tags"):
        lines.append(f"Author's tags: {', '.join(p['tags'])}")
    if p.get("clock_hz"):
        lines.append(f"Clock: {p['clock_hz']} Hz")
    if p.get("tiles"):
        lines.append(f"Tiles: {p['tiles']}")
    if p.get("analog_pins"):
        lines.append(f"Analog pins: {len(p['analog_pins'])}")
    if pins:
        lines.append("Pins: " + "; ".join(pins))
    if pmods:
        lines.append("Recommended pinouts the pins match: " + ", ".join(PMOD_BY_ID[m].name for m in pmods))
    for label, key in (("How it works", "how_it_works"), ("How to test", "how_to_test"),
                       ("External hardware", "external_hw")):
        if p.get(key):
            lines.append(f"{label}:\n{_clip(p[key])}")
    if reports:
        lines.append("Silicon test reports:")
        for r in reports[:6]:
            who = "the author" if r.get("owner") else "a tester"
            note = _clip(r.get("feedback"), 300)
            lines.append(f"- {r['status']} ({who}){': ' + note if note else ''}")
    signals = {
        "language": p.get("language"),
        "pmods": pmods,
        "analog": bool(p.get("analog_pins")),
        "pin_names": [v.strip() for v in pinout.values() if isinstance(v, str) and v.strip()],
        "author_tags": p.get("tags") or [],
        "test_status": None,
    }
    if reports:
        st = {r["status"] for r in reports}
        signals["test_status"] = "working" if "working" in st and "broken" not in st else \
            "broken" if st == {"broken"} else "partial"
    return "\n".join(lines), signals


def load_docs() -> list[Doc]:
    projects = json.loads(PROJECTS_JSON.read_text())["projects"]
    reports_by: dict[tuple[str, str], list[dict]] = {}
    if FEEDBACK_JSON.exists():
        for r in json.loads(FEEDBACK_JSON.read_text())["reports"]:
            reports_by.setdefault((r["shuttle"], r["macro"]), []).append(r)

    groups: dict[str, Doc] = {}
    for p in projects:
        if p.get("type") == "group":
            continue
        pid = f"{p['shuttle']}/{p['macro']}"
        reports = reports_by.get((p["shuttle"], p["macro"]), [])
        # Dedup on the documentation only (not shuttle, pins or reports), so a
        # resubmitted design is read once; its reports are merged below.
        raw = "|".join(_norm(p.get(k)) for k in ("title", "description", "how_it_works", "how_to_test", "external_hw"))
        key = hashlib.sha1(raw.encode()).hexdigest()[:12]
        if key in groups:
            groups[key].members.append(pid)
            continue
        text, signals = render(p, reports)
        doc_chars = sum(len(p.get(k) or "") for k in ("description", "how_it_works", "how_to_test", "external_hw"))
        groups[key] = Doc(key=key, members=[pid], title=p.get("title") or p["macro"], text=text,
                          chars=doc_chars, thin=doc_chars < THIN_DOC_CHARS, signals=signals)
    return list(groups.values())


def main() -> int:
    docs = load_docs()
    n_members = sum(len(d.members) for d in docs)
    thin = sum(1 for d in docs if d.thin)
    chars = sum(len(d.text) for d in docs)
    print(f"{len(docs)} unique documents covering {n_members} projects; {thin} thin; "
          f"{chars:,} chars of model input (~{chars // 4:,} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
