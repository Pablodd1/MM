"""Optional semantic reranker (open source): fastembed's ONNX cross-encoder.

BM25 decides what *mentions* the question's words; a cross-encoder decides what *answers* it.
On the demo corpus the difference is large: for "when should I sell half of my position", the
true rule passage scores +6.2 while the passages BM25 ranked first score −2.0 and −11.1.

This module is deliberately optional:

* **Off by default.** First use downloads the model once (tens of MB, ~40 s), and a surprise
  download inside a request is unacceptable. Set ``MMDESK_RERANKER=1`` to enable.
* **Absent is fine.** ``requirements.txt`` does not include fastembed, so serverless hosts
  never install it (torch-sized dependencies would not fit the function size limit) and
  everything falls back to plain BM25. ``/health`` reports which is active.
* **Never breaks a card.** Any failure degrades to BM25 order and is reported in the status.

Model: ``Xenova/ms-marco-MiniLM-L-6-v2`` by default — the MiniLM cross-encoder trained on
MS MARCO (Apache-2.0), run through fastembed (https://github.com/qdrant/fastembed, Apache-2.0).
Override with ``MMDESK_RERANKER_MODEL``.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("mmdesk.rerank")

DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
# Cross-encoder convention for MiniLM on MS MARCO: a score above zero means the passage
# answers the question. Below zero means it merely shares vocabulary.
RELEVANT_ABOVE = 0.0

_lock = threading.Lock()
_model = None            # loaded cross-encoder, or "failed"
_load_error = ""


def _model_name() -> str:
    return (os.environ.get("MMDESK_RERANKER_MODEL") or DEFAULT_MODEL).strip()


def installed() -> bool:
    """Whether the optional dependency is importable in this environment."""
    try:
        import fastembed.rerank.cross_encoder  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - any import failure means "not installed"
        return False


def enabled() -> bool:
    return os.environ.get("MMDESK_RERANKER") == "1"


def _load():
    """The loaded model, or None. A failed load is remembered so one bad install does not
    retry on every request."""
    global _model, _load_error
    with _lock:
        if _model is not None:
            return _model
        if _model == "failed":
            return None
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            _model = TextCrossEncoder(model_name=_model_name())
            log.info("reranker loaded: %s", _model_name())
        except Exception as exc:  # noqa: BLE001 - reranking is optional by design
            _model = "failed"
            _load_error = f"{type(exc).__name__}: {exc}"[:160]
            log.warning("reranker could not load: %s", _load_error)
            return None
    return _model


def rerank(query: str, texts: list[str]) -> list[float] | None:
    """Cross-encoder relevance scores for the texts, highest first input order preserved.

    Returns None when the reranker is disabled, not installed, or fails — callers treat that
    as "keep the BM25 order".
    """
    if not (enabled() and texts):
        return None
    model = _load()
    if model is None:
        return None
    try:
        return [float(s) for s in model.rerank(query=query, documents=texts)]
    except Exception as exc:  # noqa: BLE001
        log.warning("rerank failed, keeping BM25 order: %s", exc)
        return None


def status() -> dict:
    name = _model_name()
    active = bool(enabled() and _model is not None and _model != "failed")
    out: dict = {
        "installed": installed(),
        "enabled": enabled(),
        "active": active,
        "model": name if (enabled() or installed()) else None,
        "engine": "fastembed (ONNX cross-encoder)",
    }
    if _model == "failed":
        out["error"] = _load_error
    return out
