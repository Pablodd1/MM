"""Test isolation.

Each run gets its own SQLite registry, its own journal directory and no model configuration.
Without this, a test that creates a tenant would persist into the developer's desk — and a key
sitting in the developer's environment or key file turned one test into a real, billed call.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retrieval import tokens  # noqa: E402

# Everything a test could otherwise pick up from the machine it runs on.
_ENV = (
    "MMDESK_DB", "MMDESK_DATA_DIR", "MMDESK_DATA", "MMDESK_EPHEMERAL", "VERCEL",
    "MMDESK_ADMIN_TOKEN", "MMDESK_ALLOW_CLOUD_LLM", "MMDESK_LLM_KEY", "MMDESK_LLM_BASE",
    "MMDESK_LLM_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
)


@pytest.fixture(autouse=True, scope="session")
def _isolated_state(tmp_path_factory):
    base = tmp_path_factory.mktemp("desk")
    saved = {name: os.environ.get(name) for name in _ENV}
    for name in _ENV:
        os.environ.pop(name, None)
    os.environ["MMDESK_DB"] = str(base / "desk.db")
    os.environ["MMDESK_DATA_DIR"] = str(base / "data")
    from mmdesk import llm

    key_files = llm.KEY_FILES
    llm.KEY_FILES = (base / "no-such-key",)
    yield
    llm.KEY_FILES = key_files
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(autouse=True)
def _clean_process_state():
    """Rate buckets, cached indexes and cached bars are process state; no test may inherit
    another's."""
    from mmdesk import data, retrieval_shim, service

    service._hits.clear()
    retrieval_shim.reset_cache()
    data._SHARED_CACHE.clear()
    yield
    service._hits.clear()
    retrieval_shim.reset_cache()
    data._SHARED_CACHE.clear()


def _stamp(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class StubGrounding:
    """A year of gently rising daily bars. No provider, no network."""

    def __init__(self, ticker: str = "STUB") -> None:
        from datetime import date, timedelta

        from mmdesk.standard import Bars

        n = 260
        dates = [str(date(2025, 2, 17) + timedelta(days=i)) for i in range(n)]
        closes = [100.0 + 0.5 * i for i in range(n)]
        self.ticker = ticker.upper()
        self.bars = Bars(close=closes, open=[c - 0.2 for c in closes],
                         high=[c + 1.0 for c in closes], low=[c - 1.0 for c in closes],
                         dates=dates, source="stub")
        self.fundamentals: dict = {}
        self.sources = [{"field": "daily_bars", "provider": "stub", "rows": n,
                         "last_bar": dates[-1]}]
        self.missing: list = []
        self.errors: list = []
        self.as_of = "2026-03-01T00:00:00+00:00"
        self.last_bar = dates[-1]


class StubGrounder:
    def __init__(self, provider=None, ttl: float | None = None) -> None:
        pass

    def ground(self, ticker: str, fundamentals: bool = True) -> StubGrounding:
        return StubGrounding(ticker)


@pytest.fixture
def stub_market(monkeypatch):
    """Every endpoint that would otherwise fetch bars gets `StubGrounding` instead."""
    from mmdesk import service

    monkeypatch.setattr(service, "Grounder", StubGrounder)


def write_index(index_dir: Path, passages: list[dict], meta: dict | None = None) -> Path:
    """Write a tenant index in the format `scripts/ingest_corpus.py` produces."""
    index_dir.mkdir(parents=True, exist_ok=True)
    path = index_dir / "passages.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for i, p in enumerate(passages):
            text = p["text"]
            row = {
                "id": f"{p['video_id']}-{int(p['start_s'])}-{i}",
                "video_id": p["video_id"],
                "video_title": p.get("video_title", p["video_id"]),
                "start_s": p["start_s"],
                "stamp": _stamp(int(p["start_s"])),
                "url": p.get("url", f"https://example.invalid/{p['video_id']}?t={int(p['start_s'])}"),
                "text": text,
                "tf": dict(Counter(tokens(text))),
            }
            if p.get("synthetic"):
                row["synthetic"] = True
            fh.write(json.dumps(row) + "\n")
    if meta is not None:
        (index_dir / "video-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path
