# SPDX-License-Identifier: Apache-2.0
"""Keyword search across every project on every Tiny Tapeout shuttle."""

from pathlib import Path

# Repository root (this file lives in <root>/ttsearch/__init__.py).
ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache"
PROJECTS_JSON = DATA_DIR / "projects.json"
SHUTTLES_JSON = DATA_DIR / "shuttles.json"
DB_PATH = ROOT / "tt_projects.db"
