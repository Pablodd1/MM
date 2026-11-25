"""Adapter: expose the corpus retriever to the package without a hard dependency.

The retrieval layer lives at the project root (it is shared with the transcription
pipeline). If the index has not been built yet — a fresh clone before any
transcription — every function here reports that honestly instead of failing
import or pretending there are results.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_INDEX = ROOT / "corpus" / "passages.jsonl"


def corpus_available() -> bool:
    return _INDEX.exists() and _INDEX.stat().st_size > 0


def passage_count() -> int:
    if not corpus_available():
        return 0
    with _INDEX.open(encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _threshold() -> float:
    from retrieval import DEFAULT_MIN_SCORE

    return DEFAULT_MIN_SCORE


_cache: object | None = None


def _corpus():
    """Load the index once. Rebuilding it per request cost ~0.3 s on every card."""
    global _cache
    if _cache is None:
        from retrieval import Corpus

        _cache = Corpus(_INDEX)
    return _cache


def search(query: str, k: int = 5) -> list[dict]:
    """Return cited passages, or [] — [] means the corpus does not cover this."""
    if not corpus_available():
        return []
    return [
        {"video_id": h.video_id, "video_title": h.video_title, "start_s": h.start_s,
         "stamp": h.stamp, "url": h.url, "text": h.text, "score": h.score}
        for h in _corpus().search(query, k=k)
    ]


def refusal_reason() -> str | None:
    """Why the last search returned nothing, when it did."""
    if not corpus_available():
        return "corpus_missing"
    return getattr(_corpus(), "last_refusal", None)


def corpus_coverage() -> dict:
    """What the corpus actually covers — read from the index, not from files that may be absent.

    The first version counted `transcripts/*.json`, which exists on this machine but is
    deliberately excluded from the deployment. On Vercel it reported "0 videos, 0 hours" while
    serving 10,810 passages — a contradiction a reader would rightly distrust. The index is the
    single source of truth now; transcripts only refine it when they happen to be present.
    """
    import json as _json

    corpus = _corpus()
    ids = {row["video_id"] for row in corpus.rows}
    seconds = 0.0
    catalogue_total = len(ids)

    meta_path = ROOT / "corpus" / "video-meta.json"
    if meta_path.exists():
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        catalogue_total = len(meta) or len(ids)
        for vid in ids:
            seconds += float(meta.get(vid, {}).get("duration") or 0)

    # If the transcripts are here (local), they carry durations the index does not.
    if seconds == 0.0:
        for path in sorted((ROOT / "transcripts").glob("*.json")):
            if path.name.startswith("_"):
                continue
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
            except (_json.JSONDecodeError, OSError):
                continue
            seconds += float(data.get("duration") or 0)

    from retrieval import DEFAULT_MIN_SCORE

    return {
        "videos_transcribed": len(ids),
        "videos_in_catalogue": catalogue_total,
        "hours_transcribed": round(seconds / 3600.0, 1),
        "passages": len(corpus.rows),
        "min_score": DEFAULT_MIN_SCORE,
        "read_from": "corpus/passages.jsonl",
        "coverage_note": (
            "A refusal means no indexed statement was found in the transcribed videos, "
            "not that none exists."
        ),
    }


def ambiguous(question: str) -> bool:
    """Whether the top passage clearly beats the second. False when nothing was served."""
    corpus = _corpus()
    hits = corpus.search(question, k=3)
    return corpus.ambiguous(hits)
