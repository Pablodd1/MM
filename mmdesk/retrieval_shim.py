"""Per-tenant corpus retrieval.

Without a per-tenant index the app has to answer three different situations honestly — a
tenant with no material, a tenant with material but no match, and a tenant with a match — and the
difference has to survive all the way to the card.

`[]` always means "nothing in the material this tenant supplied". It never means "no opinion".

The index cache is keyed on each file's path, size and modification time. It used to be keyed on
the path alone, so material that was re-ingested — or deleted — kept being quoted until restart.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retrieval import Corpus  # noqa: E402

from . import tenants as tenant_mod  # noqa: E402

log = logging.getLogger("mmdesk.retrieval")

_cache: dict[str, tuple[tuple[int, int], _Index | None]] = {}
_unreadable: dict[str, str] = {}


@dataclass
class _Index:
    corpus: Corpus
    passages: int


def _tenant(tenant_id: str | None = None) -> tenant_mod.Tenant:
    t = tenant_mod.get(tenant_id or tenant_mod.DEFAULT_TENANT)
    if t is None:
        raise KeyError(f"unknown tenant {tenant_id}")
    return t


def _index(t: tenant_mod.Tenant) -> _Index | None:
    """This tenant's loaded index, or None when there is nothing to search."""
    if not t.has_corpus:
        return None
    path = t.passages_file
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size == 0:
        return None
    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _cache.get(key)
    if hit is None or hit[0] != stamp:
        index = None
        try:
            corpus = Corpus(path)
            index = _Index(corpus, corpus.n)
            _unreadable.pop(key, None)
        except (OSError, ValueError) as exc:
            # An index that will not load must not turn every request for this tenant into a 500.
            _unreadable[key] = str(exc)[:200]
            log.warning("index for tenant %s did not load: %s", t.id, exc)
        hit = (stamp, index)
        _cache[key] = hit
    return hit[1]


def _threshold(n: int) -> float:
    """The citation floor, scaled to the corpus it is applied to.

    The default floor was calibrated on ten thousand passages. BM25 scores grow with the number
    of documents that could match, so the same absolute floor that refuses noise on a large index
    refuses *everything* on a small one — a tenant with one page of notes would get silence and
    conclude the product was broken. Scale by sqrt(n / 2000), down to a small fraction.
    """
    from retrieval import DEFAULT_MIN_SCORE

    if n >= 2000:
        return DEFAULT_MIN_SCORE
    return max(0.4, DEFAULT_MIN_SCORE * (max(n, 1) / 2000) ** 0.5)


def _loaded(tenant_id: str | None = None) -> _Index | None:
    return _index(_tenant(tenant_id))


def corpus_available(tenant_id: str | None = None) -> bool:
    return _loaded(tenant_id) is not None


def passage_count(tenant_id: str | None = None) -> int:
    index = _loaded(tenant_id)
    return index.passages if index else 0


def _corpus(tenant_id: str | None = None) -> Corpus:
    index = _loaded(tenant_id)
    if index is None:
        raise FileNotFoundError(f"no readable index for tenant {tenant_id or 'default'}")
    return index.corpus


def reset_cache() -> None:
    _cache.clear()
    _unreadable.clear()


def search(query: str, k: int = 5, tenant_id: str | None = None) -> list[dict]:
    index = _loaded(tenant_id)
    if index is None:
        return []
    return [
        {"video_id": h.video_id, "video_title": h.video_title, "start_s": h.start_s,
         "stamp": h.stamp, "url": h.url, "text": h.text, "score": h.score,
         "synthetic": h.synthetic}
        for h in index.corpus.search(query, k=k, min_score=_threshold(index.passages))
    ]


def refusal_reason(tenant_id: str | None = None) -> str | None:
    """Why the last search on this tenant's index, in this request, returned nothing.

    The reason is read from the same index the search ran against and the same thread that ran
    it, so it is neither another tenant's reason nor another request's.
    """
    t = _tenant(tenant_id)
    if not t.has_corpus:
        return "corpus_missing"
    key = str(t.passages_file)
    _loaded(tenant_id)   # refresh the cache so an unreadable index is recorded
    if key in _unreadable:
        return "corpus_unreadable"
    index = _cache.get(key, (None, None))[1]
    return index.corpus.last_refusal if index else "corpus_missing"


def corpus_coverage(tenant_id: str | None = None) -> dict:
    """What this tenant's material actually covers, read from the index it ships with."""
    t = _tenant(tenant_id)
    index = _index(t)
    if index is None:
        return {
            "tenant": t.id,
            "videos_transcribed": 0,
            "videos_in_catalogue": 0,
            "hours_transcribed": 0.0,
            "passages": 0,
            "min_score": None,
            "synthetic_timestamps": False,
            "available": False,
            "readable": str(t.passages_file) not in _unreadable,
            "coverage_note": "No material is loaded for this tenant. The lens will refuse rather than invent.",
        }

    ids = {row["video_id"] for row in index.corpus.rows}
    seconds = 0.0
    synthetic = False
    catalogue_total = len(ids)
    # Only this tenant's own catalogue. A shared, repo-wide fallback used to report another
    # corpus's video count for every tenant.
    meta_path = t.corpus_path / "video-meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        catalogue_total = len(meta) or len(ids)
        for vid in ids:
            entry = meta.get(vid, {})
            seconds += float(entry.get("duration") or 0)
            synthetic = synthetic or bool(entry.get("synthetic"))

    return {
        "tenant": t.id,
        "videos_transcribed": len(ids),
        "videos_in_catalogue": catalogue_total,
        "hours_transcribed": round(seconds / 3600.0, 1),
        "passages": index.passages,
        "min_score": round(_threshold(index.passages), 2),
        "synthetic_timestamps": synthetic,
        "available": True,
        "readable": True,
        "coverage_note": (
            "Timestamps on notes are sequential placeholders, not positions in a recording. "
            "A refusal means no indexed statement was found in this tenant's material, not that "
            "none exists."
            if synthetic else
            "A refusal means no indexed statement was found in this tenant's material, not that "
            "none exists."
        ),
    }


def ambiguous(question: str, tenant_id: str | None = None) -> bool:
    index = _loaded(tenant_id)
    if index is None:
        return False
    hits = index.corpus.search(question, k=3, min_score=_threshold(index.passages))
    return index.corpus.ambiguous(hits)
