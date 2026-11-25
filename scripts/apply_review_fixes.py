"""Apply the reviewer's remaining minimum fixes (run once)."""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------- service.py
p = Path("mmdesk/service.py")
s = p.read_text(encoding="utf-8")

s = s.replace(
    """from fastapi import FastAPI, HTTPException, UploadFile""",
    """import time
import uuid

from fastapi import FastAPI, HTTPException, Request, UploadFile""",
)

s = s.replace(
    '''app = FastAPI(title="MM Desk", version=VERSION,
              description="Decision support built on published rules, with cited sources.")''',
    '''app = FastAPI(title="MM Desk", version=VERSION,
              description="Decision support built on attributed rules, with cited sources.")

# --- minimal abuse protection: 30 requests / minute / client -------------------
RATE_LIMIT = int(os.environ.get("MMDESK_RATE_LIMIT", "30"))
_RATE_WINDOW = 60.0
_hits: dict[str, list[float]] = {}


def _rate_ok(client: str) -> bool:
    now = time.time()
    bucket = [t for t in _hits.get(client, []) if now - t < _RATE_WINDOW]
    bucket.append(now)
    _hits[client] = bucket
    return len(bucket) <= RATE_LIMIT''',
)

# observability + rate limit at the top of analyze
s = s.replace(
    '''@app.post("/api/analyze")
def analyze(request: AnalyzeRequest) -> JSONResponse:
    grounder = Grounder()''',
    '''@app.post("/api/analyze")
def analyze(request: AnalyzeRequest, http_request: Request) -> JSONResponse:
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited",
                                         "limit_per_minute": RATE_LIMIT})
    request_id = uuid.uuid4().hex[:16]
    grounder = Grounder()''',
)

# cloud LLM is opt-in
s = s.replace(
    '''    data = card.to_dict()
    data["narrative"] = narrate(data, hits)''',
    '''    data = card.to_dict()
    allow_cloud = os.environ.get("MMDESK_ALLOW_CLOUD_LLM") == "1"
    data["narrative"] = (
        narrate(data, hits) if allow_cloud
        else {
            "verdict_reason": data.get("verdict_reason", ""),
            "mm_narrative": "",
            "standard_narrative": "",
            "disagreement": (data.get("disagreement") or {}).get("summary", ""),
            "caveat": (data.get("falsifiers") or [""])[0],
            "generated_by": "template (cloud model disabled: set MMDESK_ALLOW_CLOUD_LLM=1)",
        }
    )''',
)

# observability block before returning
s = s.replace(
    '''    data["markdown"] = card.to_markdown()
    return JSONResponse(data)''',
    '''    data["observability"] = {
        "request_id": request_id,
        "data_timestamp": g.as_of,
        "market_data_source": ", ".join(sorted({str(x.get("provider")) for x in g.sources})) or "none",
        "market_data_delay": "daily bars, not real-time",
        "corpus_coverage": corpus_coverage(),
        "rules_version": VERSION,
        "rubric_version": (score_meta or {}).get("source", "unknown"),
        "llm_provider": "cloud (opt-in)" if allow_cloud else "none (template)",
        "missing_fields": g.missing,
        "disclaimer_version": VERSION,
    }
    data["markdown"] = card.to_markdown()
    return JSONResponse(data)''',
)

# health: coverage + cloud flag
s = s.replace(
    '''        "llm": {"configured": cfg.configured, "model": cfg.model if cfg.configured else None},''',
    '''        "llm": {
            "configured": cfg.configured,
            "cloud_enabled": os.environ.get("MMDESK_ALLOW_CLOUD_LLM") == "1",
            "model": cfg.model if cfg.configured else None,
        },
        "corpus_coverage": corpus_coverage(),''',
)

# import coverage helper
s = s.replace(
    '''from .retrieval_shim import corpus_available, passage_count, search''',
    '''from .retrieval_shim import corpus_available, corpus_coverage, passage_count, search''',
)

# chart 501 gets the required alternatives
s = s.replace(
    '''                "error": "capability_not_implemented",
                "missing": "vision_provider",
                "note": "Chart parsing is not built in this release. Set VISION_API_KEY only "
                        "when the parser exists; the app refuses to guess chart values.",''',
    '''                "error": "chart_vision_not_available",
                "status": "not_implemented",
                "missing": "vision_provider",
                "required_alternatives": [
                    "POST typed levels: support, target, green_line, red_line",
                    "Provide OHLC JSON so every number is machine-readable",
                ],
                "note": "Axis values are never inferred from pixels in this build.",''',
)

p.write_text(s, encoding="utf-8")
print("service:", all(x in s for x in ("_rate_ok", "observability", "MMDESK_ALLOW_CLOUD_LLM")))

# ------------------------------------------------------ retrieval_shim.py
p = Path("mmdesk/retrieval_shim.py")
s = p.read_text(encoding="utf-8")
s += '''

def corpus_coverage() -> dict:
    """What the corpus actually covers — so a refusal is never read as "he never said it"."""
    import json as _json

    meta_path = ROOT / "corpus" / "video-meta.json"
    videos = hours = 0
    first = last = None
    if meta_path.exists():
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        videos = len(meta)
        for entry in meta.values():
            dur = entry.get("duration") or 0
            hours += dur / 3600.0
            up = str(entry.get("upload_date") or "")
            if len(up) == 8:
                iso = f"{up[:4]}-{up[4:6]}-{up[6:]}"
                first = min(first, iso) if first else iso
                last = max(last, iso) if last else iso
    return {
        "videos_indexed": videos,
        "hours_indexed": round(hours, 1),
        "date_range": [first, last] if first else None,
        "passages": passage_count(),
        "note": "A refusal means no indexed statement was found, not that none exists.",
    }
'''
p.write_text(s, encoding="utf-8")
print("coverage helper:", "def corpus_coverage" in s)

# ---------------------------------------------------------------- decision.py
p = Path("mmdesk/decision.py")
s = p.read_text(encoding="utf-8")
s = s.replace(
    '''    mm_lens = {
        "score_10": score,''',
    '''    mm_lens = {
        "score_10": score,
        "proxy_score": score,
        "score_label": "unvalidated proxy — not a statement by Mark Gomes",''',
)
p.write_text(s, encoding="utf-8")
print("decision proxy label:", "score_label" in s)

# ---------------------------------------------------------------- journal.py
p = Path("mmdesk/journal.py")
s = p.read_text(encoding="utf-8")
s = s.replace(
    '''    outcomes: dict = field(default_factory=dict)   # horizon_days -> outcome dict
    settled: bool = False''',
    '''    outcomes: dict = field(default_factory=dict)   # horizon_days -> outcome dict
    settled: bool = False
    stop_policy: str = "close_only_no_gap_model"
    rules_version: str = ""
    rubric_version: str = ""''',
)
s = s.replace(
    '''                    "stopped_basis": "close_only",''',
    '''                    "stopped_basis": "close_only",
                    "stop_policy": e.stop_policy,''',
)
p.write_text(s, encoding="utf-8")
print("journal policy fields:", "stop_policy" in s)
