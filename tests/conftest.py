import json
from pathlib import Path

import pytest

from ingest.parsers import parse_file

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
MANIFEST = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))["documents"]


@pytest.fixture(scope="session")
def parsed():
    """Parse every document once per test session (OCR is the slow part)."""
    cache = {}

    def get(name):
        if name not in cache:
            cache[name] = parse_file(RAW / name, MANIFEST[name])
        return cache[name]

    return get
