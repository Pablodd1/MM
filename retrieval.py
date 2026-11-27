"""BM25 retrieval with provenance, plus a refusal path.

The contract: `search()` either returns passages with citations, or returns an empty list.
An empty list is a valid and important answer — it means the loaded material does not cover
the question, and the app must say exactly that (`last_refusal`) rather than letting a
language model fill the silence.

Indexes are built by `scripts/ingest_corpus.py` with the same `tokens()` used here, so a
question and a passage are always tokenised the same way.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent
INDEX = ROOT / "corpus" / "passages.jsonl"

# Calibrated on eval/retrieval_golden.jsonl — see docs/RETRIEVAL-CALIBRATION.md.
# At 4.0 nothing was ever refused; 8.0 is the highest floor that still serves every
# answerable question in the golden set.
DEFAULT_MIN_SCORE = 8.0

WORD_RE = re.compile(r"[a-z0-9$][a-z0-9$.\-]{1,24}")
STOP = set("""a an the and or but if then than that this these those i you he she it we they
me him her us them my your his its our their is are was were be been being do does did
doing have has had having will would can could should shall may might must of in on at
to for with by from as about into over under again further once here there when where why
how all any both each few more most other some such no nor not only own same so too very
s t just don now know like get got going want well right okay ok yeah see think say said
what about""".split())


def tokens(text: str) -> list[str]:
    """Content words, with punctuation stripped before the stop-word test.

    "now." has to be recognised as the stop word "now". Testing the unstripped match let every
    stop word that happened to end a sentence into queries, where it could also trip the
    vocabulary gate and refuse a question the material does answer.
    """
    out: list[str] = []
    for raw in WORD_RE.findall(text.lower()):
        word = raw.strip(".-")
        if len(word) >= 2 and word not in STOP:
            out.append(word)
    return out


@dataclass
class Hit:
    id: str
    video_id: str
    video_title: str
    start_s: float
    stamp: str
    url: str
    text: str
    score: float
    synthetic: bool = False    # a note with a placeholder stamp, not a position in a recording


class Corpus:
    """One tenant's index. `last_refusal` is kept per thread: two requests in flight at once
    must not read each other's reason for refusing."""

    def __init__(self, index_path: Path = INDEX) -> None:
        if not index_path.exists():
            raise FileNotFoundError(
                f"{index_path} missing — run scripts/ingest_corpus.py first"
            )
        self._local = threading.local()
        self.rows: list[dict] = []
        df: Counter[str] = Counter()
        total_len = 0
        with index_path.open(encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"{index_path}: line {number} is not JSON") from exc
                # An index written without term maps still loads; the map is rebuilt from the text.
                if "tf" not in row:
                    row["tf"] = dict(Counter(tokens(str(row.get("text", "")))))
                row["len"] = sum(row["tf"].values())
                total_len += row["len"]
                for term in row["tf"]:
                    df[term] += 1
                self.rows.append(row)
        self.n = len(self.rows)
        self.avgdl = total_len / self.n if self.n else 0.0
        self.df = df

    @property
    def last_refusal(self) -> str | None:
        return getattr(self._local, "refusal", None)

    @last_refusal.setter
    def last_refusal(self, value: str | None) -> None:
        self._local.refusal = value

    def _df(self, term: str) -> int:
        # "$AAPL" is indexed as "$aapl"; a bare "AAPL" should still find it.
        return self.df.get(term, 0) + self.df.get("$" + term, 0)

    def idf(self, term: str) -> float:
        df = self._df(term)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def absent_topic(self, query: str) -> str | None:
        """The query's most specific word, if it never occurs in the loaded material.

        If the rarest content word of a question has document frequency zero, the question is
        about something the material does not contain — that is checkable, unlike "BM25 felt
        low". It is a check on the word, not the topic: inflections ("bought" for "buy") and
        synonyms are not matched.
        """
        terms = [t for t in tokens(query) if len(t) >= 3]
        if not terms:
            return None
        rarest = min(terms, key=self._df)
        return rarest if self._df(rarest) == 0 else None

    def search(self, query: str, k: int = 6, min_score: float = DEFAULT_MIN_SCORE,
               apply_gate: bool = True) -> list[Hit]:
        self.last_refusal = None
        q_terms = tokens(query)
        if not q_terms:
            self.last_refusal = "empty query"
            return []

        topic = self.absent_topic(query) if apply_gate else None
        if topic:
            self.last_refusal = f"vocabulary_absent:{topic}"
            return []
        q_tf = Counter(q_terms)
        q_bg = set(zip(q_terms, q_terms[1:]))
        k1, b = 1.5, 0.75
        scored: list[tuple[float, dict]] = []
        for row in self.rows:
            s = 0.0
            for term, qn in q_tf.items():
                f = row["tf"].get(term, 0) + row["tf"].get("$" + term, 0)
                if not f:
                    continue
                norm = f * (k1 + 1) / (f + k1 * (1 - b + b * row["len"] / self.avgdl))
                s += self.idf(term) * norm * (1 + math.log(qn))
            if s > 0 and q_bg:
                # Phrase proximity: a passage containing the question's word pairs in order is
                # more likely to be about them. Measured +0.09 hit@1 on the golden set; the
                # heavier boosts tested worse (eval/tune_ranking.py).
                text_terms = row["text"].lower().split()
                row_bg = set(zip(text_terms, text_terms[1:]))
                s *= 1 + 0.5 * len(q_bg & row_bg) / len(q_bg)
            if s > 0:
                scored.append((s, row))
        scored.sort(key=lambda x: -x[0])
        hits = [
            Hit(r["id"], r["video_id"], r["video_title"], r["start_s"], r["stamp"],
                r["url"], r["text"], round(s, 2), bool(r.get("synthetic")))
            for s, r in scored[:k]
        ]
        # Refusal rule: below the calibrated floor, the passage is not citable as the
        # material's view.
        if not hits:
            self.last_refusal = "no lexical match"
            return []
        if hits[0].score < min_score:
            self.last_refusal = f"below_floor:{hits[0].score:.1f}<{min_score:.1f}"
            return []
        return hits

    def ambiguous(self, hits: list[Hit]) -> bool:
        """True when the top two passages are close enough that the order is not meaningful.

        A single confident answer is only honest when one passage clearly wins. Within 10% of
        each other, the ranking is noise and the app should say so instead of picking one.
        """
        if len(hits) < 2 or not hits[0].score:
            return False
        return (hits[0].score - hits[1].score) / hits[0].score < 0.10


def main() -> None:
    import sys

    corpus = Corpus()
    query = " ".join(sys.argv[1:]) or "risk reward position sizing"
    print(f"{corpus.n} passages, avg {corpus.avgdl:.1f} terms")
    hits = corpus.search(query)
    if not hits:
        print(f"NO CITABLE PASSAGE for: {query}")
        return
    for h in hits:
        print(f"\n[{h.score}] {h.video_id} @ {h.stamp} — {h.video_title[:60]}")
        print("  " + h.text[:300].replace("\n", " "))


if __name__ == "__main__":
    main()
