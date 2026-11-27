"""The optional semantic reranker: what it changes, and what it must never break.

The reranker (fastembed's ONNX cross-encoder, off by default) re-scores BM25's candidate pool
by whether a passage actually *answers* the question. These tests use a stand-in scorer, so
they run everywhere including CI; the real model is exercised in a separate test that skips
when fastembed is not installed.
"""

from __future__ import annotations

import os
from types import ModuleType, SimpleNamespace

import pytest
from conftest import write_index
from fastapi.testclient import TestClient
from sample_corpus import PASSAGES

from mmdesk import rerank, retrieval_shim, service, tenants

RULE_TEXT = next(p["text"] for p in PASSAGES if p["text"].startswith("I call it the three point rule"))


def _hits() -> list:
    """Scored-hit stand-ins with the fields `_serve` reads (ids as ingest writes them)."""
    return [SimpleNamespace(
        id=f"{p['video_id']}-{p['start_s']}", video_id=p["video_id"],
        video_title=p.get("video_title", p["video_id"]), start_s=p["start_s"],
        stamp=f"{p['start_s'] // 3600:02d}:{p['start_s'] % 3600 // 60:02d}:{p['start_s'] % 60:02d}",
        url=p.get("url", ""), text=p["text"], score=12.0,
        synthetic=p.get("synthetic", False))
        for p in PASSAGES]


class _FakeReranker(ModuleType):
    """Stands in for mmdesk.rerank with a scripted scorer, hooked through the parent package —
    the same attribute the shim's in-function import reads."""

    def __init__(self, script: dict[str, float], calls: list,
                 env_aware: bool = False) -> None:
        super().__init__("mmdesk.rerank")
        self.script = script
        self.calls = calls
        self.RELEVANT_ABOVE = rerank.RELEVANT_ABOVE
        if env_aware:
            # Follows the real flag, so the disabled path can be tested without a second fake.
            self.enabled = lambda: os.environ.get("MMDESK_RERANKER") == "1"
        else:
            self.enabled = lambda: True
        self.installed = lambda: True

    def rerank(self, query, texts):
        self.calls.append(list(texts))
        return [self.script.get(t[:60], -5.0) for t in texts]

    def status(self):
        return {"installed": True, "enabled": True, "active": True,
                "model": "fake", "engine": "test"}


@pytest.fixture
def mm_corpus_tenant(tmp_path):
    index = write_index(tmp_path / "corpus", PASSAGES)
    if tenants.get("rrt") is None:
        tenants.create("rrt", "Rerank lens")
    conn = tenants._connect()
    try:
        conn.execute("UPDATE tenants SET corpus_dir = ? WHERE id = ?", (str(index.parent), "rrt"))
        conn.commit()
    finally:
        conn.close()
    retrieval_shim.reset_cache()
    return "rrt"


# _serve imports `from . import rerank`, which reads the attribute off the parent package —
# so the fake replaces that attribute, not just the sys.modules entry.
def _install_fake(monkeypatch, fake) -> None:
    import mmdesk

    monkeypatch.setattr(mmdesk, "rerank", fake)


def test_reranker_reorders_and_serves_only_what_answers(mm_corpus_tenant, monkeypatch):
    """BM25 ranked several passages for the question; the scripted cross-encoder scores only
    the real rule as answering it, and that is the only one served."""
    calls: list = []
    _install_fake(monkeypatch, _FakeReranker({RULE_TEXT[:60]: 6.0}, calls))

    served = retrieval_shim.search("what is the three point rule", tenant_id=mm_corpus_tenant)

    assert calls, "the scripted reranker must be called"
    assert [s["text"] for s in served] == [RULE_TEXT]
    corpus = retrieval_shim._corpus(mm_corpus_tenant)
    assert corpus.last_refusal is None


def test_all_below_threshold_is_a_refusal_not_a_quote(mm_corpus_tenant, monkeypatch):
    """The old floor could still serve passages that merely shared vocabulary. When the
    reranker is active and nothing actually answers, the lens refuses instead."""
    _install_fake(monkeypatch, _FakeReranker({}, []))

    served = retrieval_shim.search("what is the three point rule", tenant_id=mm_corpus_tenant)

    assert served == []
    assert retrieval_shim.refusal_reason(mm_corpus_tenant).startswith("rerank_below_threshold")


def test_disabled_reranker_leaves_the_bm25_path_untouched(mm_corpus_tenant, monkeypatch):
    monkeypatch.delenv("MMDESK_RERANKER", raising=False)
    calls: list = []
    _install_fake(monkeypatch, _FakeReranker({RULE_TEXT[:60]: 6.0}, calls, env_aware=True))

    served = retrieval_shim.search("what is the three point rule", tenant_id=mm_corpus_tenant)

    assert calls == [], "with the layer off, nothing may be sent for reranking"
    assert served, "the calibrated BM25 path keeps working"
    assert len(served) <= 5


def test_a_reranker_failure_degrades_to_bm25_order(tmp_path, monkeypatch):
    """Defence in depth: even if a future scorer implementation raises where the shipped one
    catches, `_serve` must keep the BM25 order rather than take the card down."""
    index = write_index(tmp_path / "corpus", PASSAGES)
    from retrieval import Corpus

    corpus = Corpus(index)
    fake = _FakeReranker({RULE_TEXT[:60]: 6.0}, [])

    def explode(query, texts):
        raise RuntimeError("onnx session failed")

    fake.rerank = explode
    _install_fake(monkeypatch, fake)

    served = retrieval_shim._serve(corpus, _hits(), "what is the three point rule")
    assert served, "BM25 results survive a reranker crash"
    assert corpus.last_refusal is None


def test_the_module_refuses_to_activate_without_the_flag(monkeypatch):
    monkeypatch.setenv("MMDESK_RERANKER", "0")
    calls: list = []

    class Never:
        def rerank(self, query, texts):
            calls.append(texts)
            return [1.0] * len(texts)

    monkeypatch.setattr(rerank, "_load", lambda: Never())
    assert rerank.rerank("q", ["text"]) is None
    assert calls == []


def test_health_reports_the_reranker_state(monkeypatch):
    monkeypatch.delenv("MMDESK_RERANKER", raising=False)
    body = TestClient(service.app).get("/health").json()
    r = body["reranker"]
    assert set(r) == {"installed", "enabled", "active", "model", "engine"}
    assert r["enabled"] is False and r["active"] is False


def test_status_reports_a_failed_load(monkeypatch):
    """A broken install must be visible in the status, not discovered per request."""
    monkeypatch.setattr(rerank, "_model", "failed")
    monkeypatch.setattr(rerank, "_load_error", "onnxruntime is not installed")
    monkeypatch.setenv("MMDESK_RERANKER", "1")
    status = rerank.status()
    assert status["active"] is False and "onnxruntime" in status["error"]


# ------------------------------------------------------------------ the real model

def test_the_real_cross_encoder_ranks_the_true_rule_above_decoys(monkeypatch):
    """Runs only where fastembed is installed (CI skips it). The scenario that motivated the
    layer: the true sell-half rule against passages BM25 ranked first."""
    if not rerank.installed():
        pytest.skip("fastembed not installed in this environment")
    monkeypatch.setenv("MMDESK_RERANKER", "1")
    scores = rerank.rerank(
        "when should I sell half of my position",
        ["If your position has doubled you can sell half and put that money in your pocket. "
         "You have guaranteed yourself a win on the trade.",
         "I think they should take half of their energy assets and sell it to somebody today.",
         "Talk Live, tickers, risk reward, position sizing, NYSE, NASDAQ."])
    assert scores is not None, "installed and scriptable, so scores must come back"
    assert scores[0] > rerank.RELEVANT_ABOVE, "the true rule must score as relevant"
    assert scores[0] > scores[1] and scores[0] > scores[2], "the decoys must score below it"
