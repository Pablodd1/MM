"""Measure ranking variants instead of guessing at them.

The corpus quadrupled (2.7k → 10.8k passages) and top-1 accuracy fell from 0.82 to 0.27 while
top-5 stayed at 0.82. That means recall is fine and the ordering is the problem — so this script
sweeps cheap ranking changes and reports which one actually moves hit@1 on the labelled set.

Run: .venv/Scripts/python.exe eval/tune_ranking.py
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrieval import Corpus, tokens  # noqa: E402

GOLDEN = ROOT / "eval" / "retrieval_golden.jsonl"


def bigrams(terms: list[str]) -> set[tuple[str, str]]:
    return {(terms[i], terms[i + 1]) for i in range(len(terms) - 1)}


class Scorer:
    def __init__(self, corpus: Corpus) -> None:
        self.c = corpus
        # precompute bigram sets once — the sweep scores the same rows repeatedly
        self.rows = []
        for row in corpus.rows:
            terms = [t for t in row["text"].lower().split()]
            self.rows.append((row, set(zip(terms, terms[1:]))))

    def rank(self, query: str, k: int = 5, k1: float = 1.5, b: float = 0.75,
             coverage_power: float = 0.0, phrase_bonus: float = 0.0,
             title_bonus: float = 0.0) -> list[str]:
        c = self.c
        q_terms = tokens(query)
        q_tf = Counter(q_terms)
        q_bg = bigrams(q_terms)
        seen = set(q_tf)
        scored = []
        for row, row_bg in self.rows:
            s = 0.0
            matched = 0
            for term, qn in q_tf.items():
                f = row["tf"].get(term)
                if not f:
                    continue
                matched += 1
                norm = f * (k1 + 1) / (f + k1 * (1 - b + b * row["len"] / c.avgdl))
                s += c.idf(term) * norm * (1 + math.log(qn))
            if s <= 0:
                continue
            if coverage_power:
                s *= (matched / max(1, len(seen))) ** coverage_power
            if phrase_bonus and q_bg:
                hit = len(q_bg & row_bg) / len(q_bg)
                s *= (1 + phrase_bonus * hit)
            if title_bonus:
                title = set(row["video_title"].lower().split())
                s *= (1 + title_bonus * (len(seen & title) / max(1, len(seen))))
            scored.append((s, row["video_id"]))
        scored.sort(key=lambda x: -x[0])
        return [vid for _, vid in scored[:k]]


def main() -> int:
    items = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    positives = [i for i in items
                 if i["label"] == "answerable" and i.get("acceptable_videos")]
    corpus = Corpus()
    scorer = Scorer(corpus)
    print(f"{corpus.n} passages · {len(positives)} positive questions\n")

    variants = []
    for k1 in (0.9, 1.2, 1.5, 2.0):
        for b in (0.4, 0.6, 0.75, 0.9):
            variants.append((f"k1={k1} b={b}", dict(k1=k1, b=b)))
    for cov in (0.5, 1.0, 1.5, 2.5):
        variants.append((f"k1=1.5 b=0.75 cov^{cov}", dict(coverage_power=cov)))
    for ph in (0.5, 1.0, 2.0):
        variants.append((f"k1=1.5 b=0.75 phrase+{ph}", dict(phrase_bonus=ph)))
    for tb in (0.3, 0.8):
        variants.append((f"k1=1.5 b=0.75 title+{tb}", dict(title_bonus=tb)))
    variants.append(("cov^1.5 phrase+1.0", dict(coverage_power=1.5, phrase_bonus=1.0)))
    variants.append(("cov^2.5 phrase+1.0", dict(coverage_power=2.5, phrase_bonus=1.0)))
    variants.append(("cov^1.5 phrase+1.0 title+0.3",
                     dict(coverage_power=1.5, phrase_bonus=1.0, title_bonus=0.3)))

    results = []
    for name, kwargs in variants:
        hit1 = hit5 = 0
        for item in positives:
            ranked = scorer.rank(item["question"], k=5, **kwargs)
            acceptable = set(item["acceptable_videos"])
            if ranked[:1] and ranked[0] in acceptable:
                hit1 += 1
            if acceptable & set(ranked):
                hit5 += 1
        results.append((hit1, hit5, name))
    results.sort(key=lambda r: (-r[0], -r[1]))
    for hit1, hit5, name in results:
        print(f"hit@1 {hit1 / len(positives):.2f}  hit@5 {hit5 / len(positives):.2f}   {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
