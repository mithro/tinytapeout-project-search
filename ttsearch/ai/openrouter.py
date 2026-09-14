# SPDX-License-Identifier: Apache-2.0
"""Minimal OpenRouter chat-completions client with a usage ledger.

Every call appends one line to data/ai/usage.jsonl with the model, token
counts and the cost OpenRouter reports, so spend can be totalled at any time.
The API key is read from ~/.config/tinytapeout-project-search/openrouter.key
(or OPENROUTER_API_KEY) and never enters the repository.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import AI_DIR, USAGE_LOG

API_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_FILE = Path.home() / ".config/tinytapeout-project-search/openrouter.key"
_lock = threading.Lock()


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "")
    if not key:
        sys.exit(f"no OpenRouter key: set OPENROUTER_API_KEY or write it to {KEY_FILE}")
    return key


@dataclass
class Result:
    text: str
    usage: dict
    cost: float
    model: str
    elapsed: float


class RateLimited(Exception):
    pass


def chat(model: str, system: str, user: str, *, schema: dict | None = None,
         max_tokens: int = 8000, temperature: float = 0.2, tag: str = "",
         retries: int = 5, timeout: float = 300.0, reasoning: str | None = None) -> Result:
    """One chat completion. Retries on 429/5xx/network errors with backoff."""
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "usage": {"include": True},
    }
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema.get("title", "result"), "strict": True, "schema": schema}}
    if reasoning == "off":
        body["reasoning"] = {"enabled": False}
    elif reasoning:
        body["reasoning"] = {"effort": reasoning}
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/mithro/tinytapeout-project-search",
        "X-Title": "tinytapeout-project-search",
    }
    last: Exception | None = None
    for attempt in range(retries):
        t0 = time.time()
        try:
            req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code in (429, 500, 502, 503, 504, 524) and attempt < retries - 1:
                wait = min(60, 5 * 2 ** attempt)
                print(f"    {model}: HTTP {e.code}, retry in {wait}s ({detail[:120]})", file=sys.stderr)
                time.sleep(wait)
                last = e
                continue
            raise RuntimeError(f"{model}: HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries - 1:
                wait = min(60, 5 * 2 ** attempt)
                print(f"    {model}: {e}, retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                last = e
                continue
            raise
        elapsed = time.time() - t0
        if "error" in data and not data.get("choices"):
            # OpenRouter returns 200 with an error body for some provider failures.
            msg = data["error"].get("message", str(data["error"]))
            if attempt < retries - 1 and ("rate" in msg.lower() or "overload" in msg.lower() or "timeout" in msg.lower()):
                wait = min(60, 5 * 2 ** attempt)
                print(f"    {model}: provider error, retry in {wait}s ({msg[:120]})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"{model}: {msg}")
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        record_usage(model, usage, cost, tag, elapsed, choice.get("finish_reason"))
        return Result(text=text, usage=usage, cost=cost, model=data.get("model", model), elapsed=elapsed)
    raise RuntimeError(f"{model}: gave up after {retries} attempts: {last}")


def record_usage(model: str, usage: dict, cost: float, tag: str, elapsed: float, finish: str | None) -> None:
    AI_DIR.mkdir(parents=True, exist_ok=True)
    line = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "tag": tag,
        "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "cost_usd": cost, "elapsed_s": round(elapsed, 1), "finish": finish,
    }
    with _lock, USAGE_LOG.open("a") as f:
        f.write(json.dumps(line) + "\n")


def usage_summary(tag_prefix: str | None = None) -> dict:
    """Totals from the ledger, per model."""
    out: dict[str, dict] = {}
    if not USAGE_LOG.exists():
        return out
    for line in USAGE_LOG.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if tag_prefix and not (r.get("tag") or "").startswith(tag_prefix):
            continue
        m = out.setdefault(r["model"], {"calls": 0, "prompt": 0, "completion": 0, "cost": 0.0})
        m["calls"] += 1
        m["prompt"] += r.get("prompt_tokens") or 0
        m["completion"] += r.get("completion_tokens") or 0
        m["cost"] += r.get("cost_usd") or 0.0
    return out


def print_usage(tag_prefix: str | None = None) -> None:
    total = 0.0
    for model, m in sorted(usage_summary(tag_prefix).items()):
        print(f"  {model:40} calls={m['calls']:4} in={m['prompt']:>9,} out={m['completion']:>8,} ${m['cost']:.4f}")
        total += m["cost"]
    print(f"  {'total':40} ${total:.4f}")
