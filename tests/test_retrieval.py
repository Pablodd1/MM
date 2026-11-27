"""Retrieval: the rules that ship, tested on a corpus every clone has.

The mechanism tests run against the synthetic corpus in `tests/sample_corpus.py`. The two
calibration tests need the private index that is not committed; they say so and skip rather
than fail (or silently pass) when it is absent.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import write_index
from sample_corpus import PASSAGES, UNSPOKEN, VIDEOS

from retrieval import Corpus, tokens
from mmdesk import persona, retrieval_shim, service, tenants

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "retrieval_golden.jsonl"
REAL_INDEX = ROOT / "corpus" / "passages.jsonl"

TENANT = "samplens"


@pytest.fixture(scope="module")
def sample(tmp_path_factory) -> str:
    """A tenant whose material is the synthetic corpus."""
    path = write_index(tmp_path_factory.mktemp("sample") / "corpus", PASSAGES,
                       meta={vid: {"title": title, "duration": 7000}
                             for vid, title in VIDEOS.items()})
    if tenants.get(TENANT) is None:
        tenants.create(TENANT, "Sample", "Sample lens")
    conn = tenants._connect()
    try:
        conn.execute("UPDATE tenants SET corpus_dir = ? WHERE id = ?", (str(path.parent), TENANT))
        conn.commit()
    finally:
        conn.close()
    retrieval_shim.reset_cache()
    return TENANT


@pytest.fixture(scope="module")
def corpus(sample) -> Corpus:
    return retrieval_shim._corpus(sample)


def unspoken(corpus: Corpus) -> str:
    for word in UNSPOKEN:
        if corpus._df(word) == 0:
            return word
    pytest.skip("the sample now covers every candidate word")


# ---------------------------------------------------------------- the tokenizer

def test_stop_word_with_punctuation_is_a_stop_word():
    """"now." is "now". It used to survive the stop-word test and then trip the gate."""
    assert tokens("is it time to sell now.") == ["time", "sell"]
    assert tokens("What should I do now?") == []


def test_cashtag_matches_the_bare_symbol(corpus):
    assert corpus.absent_topic("what about AAPL") is None
    assert corpus.search("AAPL", min_score=0)


# ---------------------------------------------------------------- the vocabulary gate

def test_absent_topic_finds_a_word_never_spoken(corpus):
    word = unspoken(corpus)
    assert corpus.absent_topic(f"what does he think about {word}") == word


def test_absent_topic_is_none_for_a_covered_question(corpus):
    assert corpus.absent_topic("what is the three point rule") is None


def test_refusal_names_the_missing_word(corpus):
    word = unspoken(corpus)
    assert corpus.search(f"what does he think about {word}") == []
    assert corpus.last_refusal == f"vocabulary_absent:{word}"


def test_a_question_ending_in_a_stop_word_is_not_refused(sample):
    """"...sell half of the position now." was refused as `vocabulary_absent:now`."""
    assert retrieval_shim.search("should I sell half of the position now.", tenant_id=sample)


def test_gate_can_be_disabled_for_evaluation(corpus):
    word = unspoken(corpus)
    corpus.search(f"what does he think about {word}", apply_gate=False, min_score=0)
    assert corpus.last_refusal != f"vocabulary_absent:{word}"


def test_empty_query_is_refused(corpus):
    assert corpus.search("   ") == []
    assert corpus.last_refusal == "empty query"


def test_refusal_reason_is_not_shared_between_requests(corpus):
    """Two requests in flight on one tenant's index must read their own reason."""
    seen: dict[str, str | None] = {}
    other_done = threading.Event()

    def first_request():
        corpus.search("qqqqqq")
        other_done.wait(5)
        seen["first"] = corpus.last_refusal

    worker = threading.Thread(target=first_request)
    worker.start()
    try:
        corpus.search("wwwwww")          # a second request, in this thread
    finally:
        other_done.set()
        worker.join(5)
    assert seen["first"] == "vocabulary_absent:qqqqqq"
    assert corpus.last_refusal == "vocabulary_absent:wwwwww"


# ---------------------------------------------------------------- serving passages

def test_covered_question_still_returns_citations(sample):
    hits = retrieval_shim.search("what is the three point rule", tenant_id=sample)
    assert hits, "the three-point rule is in the material and must be retrievable"
    assert hits[0]["video_id"] == "vid-rules"


def test_shim_reports_this_tenants_refusal_reason(sample, tmp_path):
    word = UNSPOKEN[0]
    question = f"what does he think about {word}"
    assert retrieval_shim.search(question, tenant_id=sample) == []
    assert retrieval_shim.refusal_reason(sample) == f"vocabulary_absent:{word}"

    # Another tenant, with material of its own, must get its own reason — not the default
    # tenant's "corpus_missing", which is what `speak()` used to report for everyone.
    tenants.create("otherlens", "Other lens")
    index = write_index(tmp_path / "other", PASSAGES[1:2])
    conn = tenants._connect()
    conn.execute("UPDATE tenants SET corpus_dir = ? WHERE id = ?",
                 (str(index.parent), "otherlens"))
    conn.commit()
    conn.close()
    retrieval_shim.reset_cache()
    hits = retrieval_shim.search(question, tenant_id="otherlens")
    utt = persona.speak(question, hits, tenant_id="otherlens")
    assert utt.refused is True
    assert utt.refusal_reason == f"vocabulary_absent:{word}"
    assert word in utt.quote and not re.search(r"he", utt.quote, re.IGNORECASE)


def test_no_material_says_no_material():
    utt = persona.speak("what is the method", [], tenant_id="default")
    assert utt.refused is True
    assert utt.refusal_reason == "corpus_missing"
    assert "No material is loaded" in utt.quote


def test_shim_caches_the_index(sample):
    first = retrieval_shim._corpus(sample)
    second = retrieval_shim._corpus(sample)
    assert first is second


def test_cache_follows_a_re_ingest(tmp_path):
    """Material that was re-ingested — or removed — must not keep being quoted."""
    tid = "reingest"
    index = write_index(tmp_path / "corpus", PASSAGES[:1])
    tenants.create(tid, "Re-ingest lens")
    conn = tenants._connect()
    conn.execute("UPDATE tenants SET corpus_dir = ? WHERE id = ?", (str(index.parent), tid))
    conn.commit()
    conn.close()
    retrieval_shim.reset_cache()
    assert retrieval_shim.search("cash is a position", tenant_id=tid) == []
    assert retrieval_shim.passage_count(tid) == 1

    # Re-ingest with more material: a different size is enough for the cache key (mtime is only
    # as fine as the filesystem makes it).
    index.unlink()
    full = write_index(tmp_path / "corpus", PASSAGES)
    hits = retrieval_shim.search("cash is a position", tenant_id=tid)
    assert hits and hits[0]["video_id"] == "vid-market"
    assert retrieval_shim.passage_count(tid) == len(PASSAGES)

    # And removing it again stops the quoting, rather than serving a stale copy until restart.
    full.unlink()
    assert retrieval_shim.search("cash is a position", tenant_id=tid) == []
    assert retrieval_shim.refusal_reason(tid) == "corpus_missing"
    assert retrieval_shim.corpus_coverage(tid)["available"] is False


def test_an_unreadable_index_does_not_raise(tmp_path):
    tid = "brokenidx"
    broken = tmp_path / "corpus" / "passages.jsonl"
    broken.parent.mkdir(parents=True)
    broken.write_text('{"id": "a", "text": "risk reward sizing", "tf": {"risk": 1}}\nnot json\n',
                      encoding="utf-8")
    tenants.create(tid, "Broken lens")
    conn = tenants._connect()
    conn.execute("UPDATE tenants SET corpus_dir = ? WHERE id = ?", (str(broken.parent), tid))
    conn.commit()
    conn.close()
    retrieval_shim.reset_cache()
    assert retrieval_shim.search("risk reward", tenant_id=tid) == []
    assert retrieval_shim.refusal_reason(tid) == "corpus_unreadable"
    client = TestClient(service.app)
    r = client.post("/api/lens", json={"question": "risk reward"}, headers={"X-Tenant": tid})
    assert r.status_code == 200 and r.json()["refused"] is True


def test_an_index_without_term_maps_still_loads(tmp_path):
    tid = "plainrows"
    plain = tmp_path / "corpus" / "passages.jsonl"
    plain.parent.mkdir(parents=True)
    plain.write_text(json.dumps({"id": "p1", "video_id": "v", "video_title": "t", "start_s": 1,
                                 "stamp": "00:00:01", "url": "", "text": PASSAGES[5]["text"]})
                     + "\n", encoding="utf-8")
    tenants.create(tid, "Plain rows lens")
    conn = tenants._connect()
    conn.execute("UPDATE tenants SET corpus_dir = ? WHERE id = ?", (str(plain.parent), tid))
    conn.commit()
    conn.close()
    retrieval_shim.reset_cache()
    assert retrieval_shim.search("cash is a position", tenant_id=tid)


# ---------------------------------------------------------------- persona

def test_persona_quotes_verbatim_from_the_passage(sample):
    hits = retrieval_shim.search("what is the three point rule", k=3, tenant_id=sample)
    utt = persona.speak("what is the three point rule", hits, tenant_id=sample)
    assert utt.refused is False
    # normalise ellipsis/whitespace, then the quote must be a substring of what was said
    quote = " ".join(utt.quote.replace("…", "").split()).lower()
    passage = " ".join(hits[0]["text"].split()).lower()
    assert quote in passage
    assert utt.video_id == hits[0]["video_id"]
    assert utt.stamp == hits[0]["stamp"]


def test_persona_survives_a_single_short_sentence(sample):
    """"Yes." has no sentence long enough to be a candidate; /api/lens returned 500."""
    utt = persona.speak("so is that a yes", [{"text": "Yes.", "video_id": "X", "video_title": "T",
                                              "stamp": "00:01:00", "url": "u", "score": 9.0}])
    assert utt.refused is False and utt.quote == "Yes."
    client = TestClient(service.app)
    r = client.post("/api/lens", json={"question": "come on in"}, headers={"X-Tenant": sample})
    assert r.status_code == 200


def test_persona_never_stitches_non_adjacent_sentences():
    """The two most relevant sentences of a passage can sit far apart. Combining them would
    invent a sentence that was never said, out of fragments that were — the one thing this must
    not do."""
    passage = (
        "The three point rule is about taking profits as a stock climbs. "
        "Meanwhile I was in Miami last week visiting some friends of mine. "
        "My dog is doing much better after the surgery, thank you for asking. "
        "So the three point rule means three points down from a high gets you out."
    )
    utt = persona.speak("what is the three point rule", [
        {"text": passage, "video_id": "X", "video_title": "T", "stamp": "00:01:00",
         "url": "u", "score": 12.0}])
    flat = " ".join(passage.split()).lower()
    quote = " ".join(utt.quote.replace("…", "").split()).lower()
    assert quote in flat, "the quote must be a contiguous slice of the passage"


def test_persona_numbers_come_from_the_quote():
    text = "The risk reward is 10 to 1 and I own 10% of the stock."
    utt = persona.speak("risk reward and position size",
                        [{"text": text, "video_id": "X", "video_title": "T",
                          "stamp": "00:01:00", "url": "u", "score": 12.0}])
    assert utt.quote
    assert persona.numbers_in(utt.quote) <= persona.numbers_in(text)


def test_lens_endpoint_returns_attribution(sample):
    client = TestClient(service.app)
    r = client.post("/api/lens", json={"question": "what is the three point rule"},
                    headers={"X-Tenant": sample})
    assert r.status_code == 200
    body = r.json()
    assert body["attribution"].startswith("verbatim")
    assert body["refused"] is False
    assert body["tenant"] == sample and body["lens_label"] == "Sample lens"


# ---------------------------------------------------------------- calibration (private index)

@pytest.fixture(scope="module")
def real_corpus():
    if not REAL_INDEX.exists():
        pytest.skip(f"{REAL_INDEX.name} is not committed: tenant material stays local. "
                    "Restore it under corpus/ to run the calibration tests.")
    return Corpus(REAL_INDEX)


def _golden():
    return [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_every_golden_no_answer_item_is_refused(real_corpus):
    for item in _golden():
        if item["label"] != "no_answer":
            continue
        assert real_corpus.search(item["question"]) == [], f"{item['id']} should be refused"
        assert real_corpus.last_refusal.startswith("vocabulary_absent:")


def test_every_golden_answerable_item_is_served(real_corpus):
    for item in _golden():
        if item["label"] != "answerable":
            continue
        assert real_corpus.search(item["question"]), f"{item['id']} should return passages"
