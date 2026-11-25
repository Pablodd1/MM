"""Retrieval refusal tests: the rule that ships must match the rule that was calibrated."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrieval import Corpus

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "retrieval_golden.jsonl"


# Words a stock-picking channel could plausibly say, used only while the corpus does not
# contain them. A fixed word ages badly: "REITs" was absent at 17 videos and is discussed at 47.
ABSENT_CANDIDATES = ("ichimoku", "bollinger", "staking", "zyzzyva")


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return Corpus()


def absent_word(corpus: Corpus) -> str:
    for word in ABSENT_CANDIDATES:
        if corpus.df.get(word, 0) == 0:
            return word
    pytest.skip("the corpus now covers every candidate word")


def test_absent_topic_finds_a_word_never_spoken(corpus):
    word = absent_word(corpus)
    assert corpus.absent_topic(f"what does he think about {word}") == word


def test_absent_topic_is_none_for_a_covered_question(corpus):
    assert corpus.absent_topic("what is the three point rule") is None


def test_refusal_names_the_missing_word(corpus):
    word = absent_word(corpus)
    hits = corpus.search(f"what does he think about {word}")
    assert hits == []
    assert corpus.last_refusal == f"vocabulary_absent:{word}"


def test_covered_question_still_returns_citations(corpus):
    hits = corpus.search("what is the three point rule")
    assert hits, "the three-point rule is in the corpus and must be retrievable"
    assert hits[0].score >= 8.0
    assert corpus.last_refusal is None


def test_gate_can_be_disabled_for_evaluation(corpus):
    word = absent_word(corpus)
    hits = corpus.search(f"what does he think about {word}", apply_gate=False, min_score=0)
    # With the gate off, the old behaviour returns whatever shares a word.
    assert corpus.last_refusal != f"vocabulary_absent:{word}"


def test_empty_query_is_refused(corpus):
    assert corpus.search("   ") == []


def test_every_golden_no_answer_item_is_refused(corpus):
    items = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    for item in items:
        if item["label"] != "no_answer":
            continue
        assert corpus.search(item["question"]) == [], f"{item['id']} should be refused"
        assert corpus.last_refusal.startswith("vocabulary_absent:")


def test_every_golden_answerable_item_is_served(corpus):
    items = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    for item in items:
        if item["label"] != "answerable":
            continue
        assert corpus.search(item["question"]), f"{item['id']} should return passages"


def test_shim_reports_the_reason(monkeypatch):
    from mmdesk import retrieval_shim

    monkeypatch.setattr(retrieval_shim, "_cache", None)
    corpus = retrieval_shim._corpus()
    word = absent_word(corpus)
    assert retrieval_shim.search(f"what does he think about {word}") == []
    assert retrieval_shim.refusal_reason() == f"vocabulary_absent:{word}"


def test_shim_caches_the_index(monkeypatch):
    from mmdesk import retrieval_shim

    monkeypatch.setattr(retrieval_shim, "_cache", None)
    first = retrieval_shim._corpus()
    second = retrieval_shim._corpus()
    assert first is second


# ---------------------------------------------------------------- persona
def test_persona_quotes_verbatim_from_the_passage(corpus):
    from mmdesk import persona

    hits = corpus.search("what is the three point rule", k=3)
    utt = persona.speak("what is the three point rule", [
        {"text": h.text, "video_id": h.video_id, "video_title": h.video_title,
         "stamp": h.stamp, "url": h.url, "score": h.score} for h in hits
    ])
    assert utt.refused is False
    # normalise ellipsis/whitespace, then the quote must be a substring of what he said
    quote = " ".join(utt.quote.replace("…", "").split()).lower()
    passage = " ".join(hits[0].text.split()).lower()
    assert quote in passage
    assert utt.video_id == hits[0].video_id
    assert utt.stamp == hits[0].stamp


def test_persona_refuses_a_topic_never_spoken(corpus, monkeypatch):
    from mmdesk import persona, retrieval_shim

    monkeypatch.setattr(retrieval_shim, "_cache", None)
    word = absent_word(corpus)
    question = f"what does he think about {word}"
    hits = retrieval_shim.search(question)
    utt = persona.speak(question, hits)
    assert hits == []
    assert utt.refused is True
    assert word in utt.quote.lower()


def test_persona_never_stitches_non_adjacent_sentences():
    """The two most relevant sentences of a passage can sit far apart. Combining them would
    invent a sentence he never said, out of fragments he did — the one thing this must not do."""
    from mmdesk import persona

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
    from mmdesk import persona

    text = "The risk reward is 10 to 1 and I own 10% of the stock."
    utt = persona.speak("risk reward and position size",
                        [{"text": text, "video_id": "X", "video_title": "T",
                          "stamp": "00:01:00", "url": "u", "score": 12.0}])
    assert persona.numbers_in(utt.quote) <= persona.numbers_in(text)


def test_persona_endpoint_returns_attribution():
    from fastapi.testclient import TestClient
    from mmdesk import service

    r = TestClient(service.app).post("/api/persona", json={"question": "what is the three point rule"})
    assert r.status_code == 200
    body = r.json()
    assert body["attribution"].startswith("verbatim")
    assert body["refused"] is False or body["refusal_reason"]
