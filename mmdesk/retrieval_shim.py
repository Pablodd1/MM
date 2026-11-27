"""Per-tenant corpus retrieval.

The first version of this file read one hard-coded index: the whole product assumed a single
corpus. Without that assumption the app has to answer three different situations honestly — a
tenant with no material, a tenant with material but no match, and a tenant with a match — and the
difference has to survive all the way to the card.

`[]` always means "nothing in the material this tenant supplied". It never means "no opinion".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from . import tenants as tenant_mod  # noqa: E402

_cache: dict[str, object] = {}


def _tenant(tenant_id: str | None = None):
    t = tenant_mod.get(tenant_id or tenant_mod.DEFAULT_TENANT)
    if t is None:
        raise KeyError(f"unknown tenant {tenant_id}")
    return t


def corpus_available(tenant_id: str | None = None) -> bool:
    t = _tenant(tenant_id)
    return t.has_corpus and t.passages_file.stat().st_size > 0


def passage_count(tenant_id: str | None = None) -> int:
    if not corpus_available(tenant_id):
        return 0
    with _tenant(tenant_id).passages_file.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _threshold(tenant_id: str | None = None) -> float:
    """The citation floor, scaled to the corpus it is applied to.

    DEFAULT_MIN_SCORE was calibrated on ten thousand passages. BM25 scores grow with the number of
    documents that could match, so the same absolute floor that refuses noise on a large index
    refuses *everything* on a small one — a tenant with one page of notes would get silence and
    conclude the product is broken. Scale by sqrt(n / 2000), never below one.
    """
    from retrieval import DEFAULT_MIN_SCORE

    n = passage_count(tenant_id)
    if n >= 2000:
        return DEFAULT_MIN_SCORE
    # The lower bound is deliberately small: on a one-page corpus an IDF-weighted score is a
    # fraction of a point, and refusing at one would make a correctly indexed tenant look empty.
    # The vocabulary gate, not the floor, is what refuses topics the material never covers.
    return max(0.4, DEFAULT_MIN_SCORE * (max(n, 1) / 2000) ** 0.5)


def _corpus(tenant_id: str | None = None):
    """Load and cache each tenant's index separately; sharing one cache would cross tenants."""
    t = _tenant(tenant_id)
    key = str(t.passages_file)
    if key not in _cache:
        from retrieval import Corpus

        _cache[key] = Corpus(t.passages_file)
    return _cache[key]


def reset_cache() -> None:
    _cache.clear()


def search(query: str, k: int = 5, tenant_id: str | None = None) -> list[dict]:
    if not corpus_available(tenant_id):
        return []
    return [
        {"video_id": h.video_id, "video_title": h.video_title, "start_s": h.start_s,
         "stamp": h.stamp, "url": h.url, "text": h.text, "score": h.score}
        for h in _corpus(tenant_id).search(query, k=k, min_score=_threshold(tenant_id))
    ]


def refusal_reason(tenant_id: str | None = None) -> str | None:
    if not corpus_available(tenant_id):
        return "corpus_missing"
    return getattr(_corpus(tenant_id), "last_refusal", None)


def corpus_coverage(tenant_id: str | None = None) -> dict:
    """What this tenant's material actually covers, read from the index it ships with."""
    t = _tenant(tenant_id)
    if not corpus_available(tenant_id):
        return {
            "tenant": t.id,
            "videos_transcribed": 0,
            "videos_in_catalogue": 0,
            "hours_transcribed": 0.0,
            "passages": 0,
            "min_score": None,
            "read_from": None,
            "available": False,
            "coverage_note": "No material uploaded for this tenant. The lens will refuse rather than invent.",
        }

    corpus = _corpus(tenant_id)
    ids = {row["video_id"] for row in corpus.rows}
    seconds = 0.0
    catalogue_total = len(ids)

    meta_path = t.corpus_path / "video-meta.json"
    if not meta_path.exists():
        meta_path = ROOT / "corpus" / "video-meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        catalogue_total = len(meta) or len(ids)
        for vid in ids:
            seconds += float(meta.get(vid, {}).get("duration") or 0)

    from retrieval import DEFAULT_MIN_SCORE

    read_from = None
    try:
        read_from = str(t.passages_file.relative_to(ROOT))
    except ValueError:
        read_from = str(t.passages_file)

    return {
        "tenant": t.id,
        "videos_transcribed": len(ids),
        "videos_in_catalogue": catalogue_total,
        "hours_transcribed": round(seconds / 3600.0, 1),
        "passages": len(corpus.rows),
        "min_score": round(_threshold(tenant_id), 2),
        "read_from": read_from,
        "available": True,
        "coverage_note": "A refusal means no indexed statement was found in this tenant's material, not that none exists.",
    }


def ambiguous(question: str, tenant_id: str | None = None) -> bool:
    if not corpus_available(tenant_id):
        return False
    corpus = _corpus(tenant_id)
    hits = corpus.search(question, k=3, min_score=_threshold(tenant_id))
    return corpus.ambiguous(hits)
