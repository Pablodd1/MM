"""Does a local embedding reranker fix the ranking the corpus growth broke?

BM25 top-25 by keyword, then nomic-embed-text cosine similarity to reorder. Measured against
the same labelled questions, so the answer is a number and not an opinion.

Run: .venv/Scripts/python.exe eval/rerank_test.py
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrieval import Corpus  # noqa: E402

OLLAMA = "http://127.0.0.1:11434/api/embeddings"
GOLDEN = ROOT / "eval" / "retrieval_golden.jsonl"


def embed(text: str, model: str = "nomic-embed-text", timeout: int = 60) -> list[float]:
    body = json.dumps({"model": model, "prompt": text[:2000]}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["embedding"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    lines = GOLDEN.read_text(encoding="utf-8").splitlines()
    items = [json.loads(line) for line in lines if line.strip()]
    positives = [i for i in items if i["label"] == "answerable" and i.get("acceptable_videos")]
    corpus = Corpus()
    print(f"{corpus.n} passages · {len(positives)} questions\n")

    bm1 = bm5 = rr1 = rr5 = 0
    for item in positives:
        q = item["question"]
        acceptable = set(item["acceptable_videos"])
        hits = corpus.search(q, k=25, min_score=0.0)
        ranked = [h.video_id for h in hits]
        if ranked[:1] and ranked[0] in acceptable:
            bm1 += 1
        if acceptable & set(ranked[:5]):
            bm5 += 1

        qv = embed(q)
        scored = [(cosine(qv, embed(h.text)), h.video_id) for h in hits]
        scored.sort(key=lambda x: -x[0])
        reordered = [vid for _, vid in scored]
        if reordered[:1] and reordered[0] in acceptable:
            rr1 += 1
        if acceptable & set(reordered[:5]):
            rr5 += 1
        print(f"{item['id']}: bm25#1 {ranked[0] if ranked else '-'} · rerank#1 {scored[0][1] if scored else '-'}")

    n = len(positives)
    print(f"\nBM25      hit@1 {bm1 / n:.2f}  hit@5 {bm5 / n:.2f}")
    print(f"+rerank   hit@1 {rr1 / n:.2f}  hit@5 {rr5 / n:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
