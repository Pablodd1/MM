"""The lens voice, built so it cannot misquote the material.

The difference between this and a chatbot in someone's voice: the lens **quotes**. It selects
sentences the author actually said, keeps them verbatim, and attaches the timestamp. It does not
paraphrase, summarise, or infer what the author "would" think.

That choice is what makes fidelity measurable. A paraphrase can only be graded by opinion; a
verbatim quote either appears in the transcript or it doesn't, and a number in it either appears
or it doesn't. `eval/persona_fidelity.py` measures exactly that.

When the tenant's material has nothing, the lens says so. A "Simon says" that always has an answer
is a horoscope; this one is allowed to be silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
WORD_RE = re.compile(r"[A-Za-z0-9$][A-Za-z0-9$'\-]*")
NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")

# What the lens says when it will not quote. Each sentence states a fact about the material
# that was loaded, never a fact about the author's opinion.
NO_MATERIAL = (
    "No material is loaded for this lens, so there is nothing to quote. The card rests on its "
    "general rules."
)
WORD_ABSENT = (
    "The word “{word}” does not occur anywhere in the material loaded for this lens, so there "
    "is nothing to quote. That is a limit of the material, not a statement about the author's view."
)
NO_MATCH = (
    "No passage in the supplied material matched this closely enough to quote. The material "
    "covers part of the subject, so silence here is not evidence of the author's opinion."
)


@dataclass
class Utterance:
    """One attributed statement: the author's words, the author's timestamp, no paraphrase."""
    question: str
    quote: str
    video_id: str
    video_title: str
    stamp: str
    url: str
    score: float
    supported_terms: list[str] = field(default_factory=list)
    missing_terms: list[str] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    note: str = ""

    @property
    def supported(self) -> bool:
        return not self.refused and not self.missing_terms

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "quote": self.quote,
            "video_id": self.video_id,
            "video_title": self.video_title,
            "stamp": self.stamp,
            "url": self.url,
            "score": self.score,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "supported": self.supported,
            "supported_terms": self.supported_terms,
            "missing_terms": self.missing_terms,
            "note": self.note,
            "attribution": "verbatim from the material — not a paraphrase",
        }


def _content_terms(text: str) -> set[str]:
    from retrieval import tokens

    return set(tokens(text))


def best_span(passage: str, question: str) -> str:
    """The best *contiguous* run of one or two sentences.

    Contiguity is not cosmetic. Joining the two best-scoring sentences from different parts of
    a passage produces words the author never said in that order — a fabricated quote built out of real
    fragments. Only adjacent sentences are ever combined, and the winner is scored as a unit.
    """
    # Every sentence stays in the list, including short ones. Filtering short sentences before
    # pairing makes two distant sentences look adjacent: "…three point rule." followed by
    # "I call it." and "This is a 10 point scale." would be stitched straight to a later
    # sentence, producing a quote that reads perfectly and was never said.
    sentences = [s.strip() for s in SENTENCE_RE.split(passage) if s.strip()]
    if not sentences:
        return passage.strip()
    q = _content_terms(question)

    def score(text: str) -> tuple[int, int]:
        # overlapping terms first; among equals prefer the longer contiguous run, because a
        # one-clause answer is usually too thin to be useful even when it is accurate
        return (len(q & _content_terms(text)), len(text))

    candidates = [(s, i) for i, s in enumerate(sentences) if len(s) >= 30]
    for i in range(len(sentences) - 1):
        joined = sentences[i] + " " + sentences[i + 1]
        if len(joined) >= 30:
            candidates.append((joined, i))
    if not candidates:
        # A passage made only of short sentences is still quotable, as a whole.
        return passage.strip()
    best_text, _ = max(candidates, key=lambda c: score(c[0]))
    return best_text.strip()


def speak(question: str, hits: list[dict], tenant_id: str | None = None) -> Utterance:
    """Return an attributed utterance, or a labelled refusal. Never a guess.

    `tenant_id` selects whose material is being asked about. Without it the refusal reason is
    read from the default tenant's index, which is the wrong material for everyone else.
    """
    if not hits:
        from .retrieval_shim import refusal_reason

        reason = refusal_reason(tenant_id) or "no lexical match"
        if reason.startswith("vocabulary_absent:"):
            word = reason.split(":", 1)[1]
            text = WORD_ABSENT.format(word=word)
        elif reason == "corpus_missing":
            text = NO_MATERIAL
        else:
            text = NO_MATCH
        return Utterance(
            question=question, quote=text, video_id="", video_title="", stamp="", url="",
            score=0.0, refused=True, refusal_reason=reason, note=text,
        )

    top = hits[0]
    passage = top.get("text", "")
    quote = best_span(passage, question)
    if len(quote) > 320:
        cut = quote[:320]
        quote = cut[: cut.rfind(" ")].rstrip() + "…"

    q_terms = _content_terms(question)
    supported = sorted(t for t in q_terms if t in _content_terms(quote))
    # "missing" only counts terms of the question that the quote does not address. The quote is
    # verbatim, so this is a note about coverage, not about faithfulness.
    missing = sorted(q_terms - set(supported))
    return Utterance(
        question=question,
        quote=quote,
        video_id=top.get("video_id", ""),
        video_title=top.get("video_title", ""),
        stamp=top.get("stamp", ""),
        url=top.get("url", ""),
        score=float(top.get("score", 0.0)),
        supported_terms=supported,
        missing_terms=missing,
        note="quote only; the app does not put words in the author's mouth",
    )


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(text)}
