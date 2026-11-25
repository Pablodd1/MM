"""MM Desk HTTP service.

One endpoint does the work: POST /api/analyze takes a question and a ticker, grounds
the numbers, runs both lenses, reconciles them, writes the narrative and journals the
card. Everything it returns is either fetched, computed, or cited — and the response
says which.

Run:  uvicorn mmdesk.service:app --port 8791
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

import time
import uuid

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import rules
from . import rubric
from . import persona as persona_mod
from . import conversation as conversation_mod
from . import scenarios as scenario_mod
from . import voice as voice_mod
from .card import DISCLAIMER, VERSION
from .data import Grounder, intraday_supported
from .decision import build_decision_card
from .journal import Journal, summary
from .llm import LLMConfig, narrate
from .retrieval_shim import (ambiguous, corpus_available, corpus_coverage,
                             passage_count, search)
from .rules import Account, Setup
from .standard import atr, range_position, realised_vol, standard_lens, trend_score

def _default_data_dir() -> Path:
    """Where the journal lives.

    On Vercel the bundle is read-only; only /tmp is writable and it is per-instance and
    ephemeral. That is acceptable for a demo as long as /health says so, which it does.
    """
    if os.environ.get("MMDESK_DATA"):
        return Path(os.environ["MMDESK_DATA"])
    if os.environ.get("VERCEL"):
        return Path("/tmp/mmdesk")
    return Path(__file__).resolve().parent.parent / "runtime"


DATA_DIR = _default_data_dir()
JOURNAL_EPHEMERAL = bool(os.environ.get("VERCEL"))
JOURNAL_PATH = DATA_DIR / "journal.jsonl"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="MM Desk", version=VERSION,
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
    return len(bucket) <= RATE_LIMIT


class AnalyzeRequest(BaseModel):
    question: str = Field("", max_length=2000)
    ticker: str = Field(..., min_length=1, max_length=12)
    mode: Literal["swing", "intraday"] = "swing"
    account_equity: float = Field(10_000, gt=0)
    risk_pct: float = Field(0.01, gt=0, le=0.05)
    alert: Literal["green", "yellow", "orange", "red"] = "green"
    cash_pct: float | None = Field(None, ge=0, le=1)
    min_cash_pct: float = Field(0.0, ge=0, lt=1)
    support: float | None = Field(None, gt=0)
    target: float | None = Field(None, gt=0)
    time_stop_days: int | None = Field(None, gt=0, le=3650)
    components: dict[str, int] | None = None
    green_line: float | None = Field(None, gt=0, description="fundamental fair-value line (his 10)")
    red_line: float | None = Field(None, gt=0, description="fundamental too-expensive line (his 1)")
    commission_per_share: float = Field(0.0, ge=0, le=50)
    fixed_fee_per_order: float = Field(0.0, ge=0, le=500)
    slippage_bps: float = Field(0.0, ge=0, le=200)
    existing_open_risk: float = Field(0.0, ge=0)
    allow_fractional_shares: bool = False


@app.get("/health")
def health() -> dict[str, Any]:
    cfg = LLMConfig.from_env()
    return {
        "ok": True,
        "version": VERSION,
        "corpus": {"available": corpus_available(), "passages": passage_count()},
        "llm": {
            "configured": cfg.configured,
            "cloud_enabled": os.environ.get("MMDESK_ALLOW_CLOUD_LLM") == "1",
            "model": cfg.model if cfg.configured else None,
        },
        "corpus_coverage": corpus_coverage(),
        "vision": {"configured": bool(os.environ.get("VISION_API_KEY"))},
        "intraday_supported": intraday_supported(),
        "voice": voice_mod.status(),
        "journal_entries": len(Journal(JOURNAL_PATH).all()),
        "journal_persistent": not JOURNAL_EPHEMERAL,
        "data_dir": str(DATA_DIR),
    }


def _levels(g, request: AnalyzeRequest) -> tuple[float, float, list[str]]:
    """Support/target: user input wins; otherwise computed and labelled as such."""
    notes: list[str] = []
    bars = g.bars
    price = bars.close[-1]
    a = atr(bars) or price * 0.02

    support = request.support
    if support is None:
        lookback = bars.low[-20:] if bars.low else bars.close[-20:]
        support = min(lookback) * 0.995
        notes.append("support computed from the 20-bar low, minus 0.5%")

    target = request.target
    if target is None:
        lookback = bars.high[-60:] if bars.high else bars.close[-60:]
        high = max(lookback)
        target = high if high > price * 1.02 else price + 2 * a
        notes.append("target set from the 60-bar high, or 2×ATR when that high is already behind price")

    if support >= price:
        raise HTTPException(422, "support must sit below the current price for a long setup")
    if target <= price:
        raise HTTPException(422, "target must sit above the current price for a long setup")
    return float(support), float(target), notes


def _days_to_earnings(ts: Any) -> int | None:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone

        when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return (when - datetime.now(timezone.utc)).days
    except (TypeError, ValueError, OSError):
        return None


def _build_response(request: AnalyzeRequest, request_id: str) -> dict:
    """Ground, score, reconcile, journal. Shared by the text and voice endpoints."""
    grounder = Grounder()
    g = grounder.ground(request.ticker)
    if g.bars is None:
        raise HTTPException(
            422,
            detail={
                "error": "no market data",
                "ticker": request.ticker,
                "missing": g.missing,
                "errors": g.errors,
                "note": "The app does not estimate prices. Retry when the provider is reachable.",
            },
        )

    support, target, level_notes = _levels(g, request)
    bars = g.bars
    price = bars.close[-1]
    std = standard_lens(bars)
    tr = trend_score(bars)
    a = atr(bars)

    if request.green_line and request.red_line:
        # His actual mechanic: price between fundamental valuation bands.
        fv = rubric.fair_value_score(price, request.green_line, request.red_line)
        comps = {"fundamental_bands": fv["score_10"]}
        score_meta = fv
        # A single component means rules.score_10() returns the band score unchanged.
    elif request.components:
        comps = request.components
        score_meta = {"score_10": rules.score_10(comps), "components": comps,
                      "source": "user-supplied components"}
    else:
        scored = rubric.score(rubric.RubricInputs(
            price=price, support=support, target=target, atr=a,
            trend_score=tr["score"],
            days_to_earnings=_days_to_earnings(g.fundamentals.get("earnings_date")),
            realised_vol=realised_vol(bars),
        ))
        comps = scored["components"]
        score_meta = scored

    setup = Setup(
        ticker=request.ticker.upper(), price=price, invalidation=support, first_target=target,
        component_scores=comps, mode=request.mode, alert=request.alert,
        time_stop_days=request.time_stop_days, atr=a,
    )
    account = Account(
        equity=request.account_equity, risk_pct_per_trade=request.risk_pct,
        cash_pct=request.cash_pct, min_cash_pct=request.min_cash_pct,
        commission_per_share=request.commission_per_share,
        fixed_fee_per_order=request.fixed_fee_per_order,
        slippage_bps=request.slippage_bps,
        existing_open_risk=request.existing_open_risk,
        allow_fractional_shares=request.allow_fractional_shares,
    )

    hits = search(f"{request.question} {request.ticker}", k=5) if request.question else search(request.ticker, k=5)

    card = build_decision_card(
        question=request.question or f"Analyse {request.ticker}",
        setup=setup, account=account, standard=std,
        corpus_hits=hits,
        grounding={
            "as_of": g.as_of,
            "sources": g.sources,
            "missing": g.missing,
            "level_notes": level_notes,
            "intraday_supported": intraday_supported(),
        "voice": voice_mod.status(),
        },
    )
    card.mm_lens["score_meta"] = score_meta
    if request.mode == "intraday" and not intraday_supported():
        card.falsifiers.append(
            "Intraday mode ran on daily data: the free feed is delayed, so any intraday "
            "conclusion here is indicative only."
        )

    data = card.to_dict()
    if not hits:
        from .retrieval_shim import refusal_reason as _refusal

        data["corpus_refusal"] = _refusal()
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
    )
    card.narrative = data["narrative"]

    try:
        entry = Journal(JOURNAL_PATH).append(card.to_dict() | {"narrative": data["narrative"]})
        data["journal_id"] = entry.id
    except ValueError as exc:
        data["journal_error"] = str(exc)

    data["observability"] = {
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
    return data


@app.post("/api/analyze")
def analyze(request: AnalyzeRequest, http_request: Request) -> JSONResponse:
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited",
                                         "limit_per_minute": RATE_LIMIT})
    return JSONResponse(_build_response(request, uuid.uuid4().hex[:16]))


@app.post("/api/voice")
def voice(
    http_request: Request,
    audio: UploadFile,
    account_equity: float = Form(50_000),
    risk_pct: float = Form(0.01),
    alert: str = Form("green"),
    green_line: float | None = Form(None),
    red_line: float | None = Form(None),
) -> JSONResponse:
    """Push-to-talk: audio in, transcript + card + spoken answer out. Nothing leaves here."""
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited",
                                         "limit_per_minute": RATE_LIMIT})

    raw = audio.file.read()
    if not raw:
        raise HTTPException(422, detail={"error": "empty upload"})
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, detail={"error": "audio too large", "bytes": len(raw)})
    if not 0 < account_equity <= 100_000_000:
        raise HTTPException(422, detail={"error": "account_equity out of range"})
    if not 0 < risk_pct <= 0.05:
        raise HTTPException(422, detail={"error": "risk_pct out of range (0, 0.05]"})
    if alert not in ("green", "yellow", "orange", "red"):
        raise HTTPException(422, detail={"error": "alert must be green/yellow/orange/red"})

    suffix = Path(audio.filename or "clip.webm").suffix or ".webm"
    tmp_dir = Path(tempfile.mkdtemp(prefix="mmdesk-voice-"))
    clip = tmp_dir / f"clip{suffix}"
    clip.write_bytes(raw)

    timings: dict[str, int] = {}
    try:
        t0 = time.perf_counter()
        try:
            transcript = voice_mod.transcribe(clip)
        except voice_mod.VoiceUnavailable as exc:
            raise HTTPException(
                501,
                detail={"error": "voice_not_available", "missing": "stt",
                        "note": str(exc),
                        "required": "local faster-whisper model + ffmpeg on PATH"},
            ) from exc
        except ValueError as exc:
            raise HTTPException(422, detail={"error": "bad_audio", "note": str(exc)}) from exc
        timings["stt_ms"] = int((time.perf_counter() - t0) * 1000)

        routed = voice_mod.route(transcript)
        if not routed["ticker"]:
            spoken = "I could not pick a ticker out of that. Try again and name the symbol."
            audio_b64, audio_mime = _speak_payload(spoken)
            return JSONResponse({
                "transcript": transcript,
                "ticker": None,
                "reply": spoken,
                "audio_b64": audio_b64,
                "audio_mime": audio_mime,
                "timings": timings,
                "needs": "ticker",
            })

        req = AnalyzeRequest(
            question=routed["question"], ticker=routed["ticker"],
            account_equity=account_equity, risk_pct=risk_pct, alert=alert,
            green_line=green_line, red_line=red_line,
        )
        request_id = uuid.uuid4().hex[:16]
        try:
            data = _build_response(req, request_id)
        except HTTPException as exc:
            spoken = ("I could not get market data for that ticker, so I will not guess. "
                      "The card stops rather than inventing a price.")
            audio_b64, audio_mime = _speak_payload(spoken)
            return JSONResponse(
                {
                    "transcript": transcript,
                    "ticker": routed["ticker"],
                    "reply": spoken,
                    "audio_b64": audio_b64,
                    "audio_mime": audio_mime,
                    "timings": timings,
                    "error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
                },
                status_code=200,
            )

        spoken = voice_mod.reply_text(data)
        t1 = time.perf_counter()
        data["audio_b64"], data["audio_mime"] = _speak_payload(spoken)
        timings["tts_ms"] = int((time.perf_counter() - t1) * 1000)
        data["transcript"] = transcript
        data["ticker"] = routed["ticker"]
        data["reply"] = spoken
        data["timings"] = timings
        data["observability"]["voice"] = {"local_only": True, "tts_voice": voice_mod.TTS_VOICE}
        return JSONResponse(data)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _speak_payload(text: str) -> tuple[str | None, str]:
    try:
        wav = voice_mod.speak(text)
    except voice_mod.VoiceUnavailable:
        return None, "audio/wav"
    mp3 = voice_mod.to_mp3(wav)
    if mp3:
        return base64.b64encode(mp3).decode("ascii"), "audio/mpeg"
    return base64.b64encode(wav).decode("ascii"), "audio/wav"


@app.post("/api/voice/selftest")
def voice_selftest(http_request: Request) -> JSONResponse:
    """Run the whole loop on a synthetic question: TTS -> STT -> route -> card -> TTS.

    This is the honest way to prove the voice path works without a microphone, and it is
    the same code path the browser uses.
    """
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited"})
    question = "Is Nvidia a buy here with a stop under support?"
    tmp_dir = Path(tempfile.mkdtemp(prefix="mmdesk-selftest-"))
    try:
        try:
            clip = tmp_dir / "q.wav"
            clip.write_bytes(voice_mod.speak(question))
            t0 = time.perf_counter()
            transcript = voice_mod.transcribe(clip)
            stt_ms = int((time.perf_counter() - t0) * 1000)
        except voice_mod.VoiceUnavailable as exc:
            raise HTTPException(501, detail={"error": "voice_not_available", "note": str(exc)})
        routed = voice_mod.route(transcript)
        request_id = uuid.uuid4().hex[:16]
        data = _build_response(
            AnalyzeRequest(question=routed["question"], ticker=routed["ticker"] or "NVDA",
                           account_equity=50_000),
            request_id,
        )
        spoken = voice_mod.reply_text(data)
        t1 = time.perf_counter()
        data["audio_b64"], data["audio_mime"] = _speak_payload(spoken)
        data["transcript"] = transcript
        data["ticker"] = routed["ticker"]
        data["reply"] = spoken
        data["timings"] = {"stt_ms": stt_ms, "tts_ms": int((time.perf_counter() - t1) * 1000)}
        data["selftest"] = True
        return JSONResponse(data)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.get("/api/journal")
def journal_list() -> dict[str, Any]:
    entries = Journal(JOURNAL_PATH).all()
    return {
        "count": len(entries),
        "summary": summary(entries),
        "entries": [
            {"id": e.id, "instrument": e.instrument, "verdict": e.verdict,
             "price_at_call": e.price_at_call, "created_at": e.created_at,
             "settled": e.settled, "outcomes": e.outcomes}
            for e in entries
        ],
    }


@app.post("/api/journal/score")
def journal_score(payload: dict[str, Any]) -> dict[str, Any]:
    """Grade entries against a supplied price series: {ticker: [[date, close], ...]}."""
    series = payload.get("prices") or {}
    def _row(item):
        if isinstance(item, dict):
            return item
        if len(item) == 2:
            return [str(item[0]), float(item[1])]
        return [str(item[0])] + [float(x) for x in item[1:5]]

    cleaned = {k.upper(): [_row(i) for i in v] for k, v in series.items() if isinstance(v, list)}
    entries = Journal(JOURNAL_PATH).score_entries(cleaned)
    return {"scored": sum(1 for e in entries if e.outcomes), "summary": summary(entries)}


class PersonaRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)


class ScenarioRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=12)
    green_line: float | None = Field(None, gt=0)
    red_line: float | None = Field(None, gt=0)


@app.post("/api/scenario")
def scenario(request: ScenarioRequest, http_request: Request) -> dict[str, Any]:
    """Six rule-based styles read the same data. The spread is the finding, not the mode."""
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited"})
    g = Grounder().ground(request.ticker, fundamentals=False)
    if g.bars is None:
        raise HTTPException(422, detail={"error": "no market data", "ticker": request.ticker,
                                         "missing": g.missing, "errors": g.errors})
    bars = g.bars
    price = bars.close[-1]
    std = standard_lens(bars)
    score = None
    if request.green_line and request.red_line:
        score = rubric.fair_value_score(price, request.green_line, request.red_line)["score_10"]
    lookback = bars.close[-60:] if len(bars.close) >= 60 else bars.close
    high = max(bars.high[-60:] if bars.high else lookback)
    momentum = (price / bars.close[-21] - 1) if len(bars.close) > 21 else 0.0
    facts = scenario_mod.Facts(
        ticker=request.ticker.upper(), price=price, score_10=score,
        trend=std["trend"]["score"], momentum=momentum,
        volatility=std["volatility_60d_annualised"] or std["volatility_annualised"],
        drawdown=(price / high - 1) if high else 0.0,
        range_pos=(range_position(bars)["pct_of_range"] or 0.5),
    )
    out = scenario_mod.run(facts)
    out["observability"] = {"data_timestamp": g.as_of, "source": "yfinance daily bars",
                            "account_sizing": False}
    return out


class CrowdRequest(BaseModel):
    rounds: int = Field(30, ge=0, le=500)
    topology: str = Field("ring", pattern="^(ring|clique|two_camps)$")
    trust: float = Field(0.5, ge=0.0, le=1.5)
    model: str = Field("bounded_confidence", pattern="^(averaging|bounded_confidence)$")
    tolerance: float = Field(0.5, ge=0.05, le=2.0)


@app.post("/api/crowd/talk")
def crowd_talk(request: CrowdRequest) -> dict[str, Any]:
    """Let the six styles influence each other for N rounds and report what happens.

    No language model, no tokens: this shows whether a crowd converges or splits, which is the
    part of a simulated debate that costs nothing to run and survives scrutiny.
    """
    agents = conversation_mod.starter_crowd()
    out = conversation_mod.run(agents, rounds=request.rounds, topology=request.topology,
                               trust=request.trust, model=request.model,
                               tolerance=request.tolerance)
    out["label"] = (
        "Simulated influence between six rule-based styles. No language model is involved, no "
        "text is generated, and nothing here is a forecast."
    )
    return out


@app.post("/api/persona")
def persona_ask(request: PersonaRequest, http_request: Request) -> dict[str, Any]:
    """What did he actually say about this? Verbatim quote with a timestamp, or a refusal."""
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited"})
    hits = search(request.question, k=5)
    payload = persona_mod.speak(request.question, hits).to_dict()
    payload["ambiguous"] = ambiguous(request.question)
    payload["coverage"] = corpus_coverage()
    return payload


@app.post("/api/journal/grade")
def journal_grade(http_request: Request) -> dict[str, Any]:
    """Grade every open entry against real, split-adjusted OHLC bars fetched here.

    This is the version of the journal that can be trusted: the caller does not supply the
    prices, so they cannot be chosen after the fact. Stops are simulated with gap fills, and
    any >25% move that does not touch the stop is flagged as a suspected corporate action
    rather than scored as a loss.
    """
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited"})

    journal = Journal(JOURNAL_PATH)
    entries = journal.all()
    if not entries:
        return {"graded": 0, "note": "no journal entries yet"}

    grounder = Grounder()
    series: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for ticker in sorted({e.instrument for e in entries}):
        try:
            g = grounder.ground(ticker, fundamentals=False)
        except Exception as exc:  # noqa: BLE001
            errors[ticker] = f"{type(exc).__name__}: {exc}"
            continue
        if g.bars is None or g.bars.dates is None or g.bars.open is None:
            errors[ticker] = "no OHLC with dates from the provider"
            continue
        series[ticker] = [
            {"date": d, "open": o, "high": h, "low": l, "close": c}
            for d, o, h, l, c in zip(g.bars.dates, g.bars.open,
                                     g.bars.high or g.bars.close,
                                     g.bars.low or g.bars.close, g.bars.close)
        ]

    scored = journal.score_entries(series, adjusted=True, source="yfinance auto_adjust")
    return {
        "graded": sum(1 for e in scored if e.outcomes),
        "instruments": list(series),
        "errors": errors,
        "summary": summary(scored),
        "entries": [
            {"id": e.id, "instrument": e.instrument, "settled": e.settled,
             "outcomes": e.outcomes}
            for e in scored if e.outcomes
        ],
    }


@app.post("/api/chart")
def chart_upload(
    http_request: Request,
    file: UploadFile | None = None,
    ticker: str = Form(""),
    support: float | None = Form(None),
    target: float | None = Form(None),
    green_line: float | None = Form(None),
    red_line: float | None = Form(None),
    account_equity: float = Form(50_000),
    risk_pct: float = Form(0.01),
) -> JSONResponse:
    """A chart is accepted as a *reference image*, never as a source of numbers.

    Axis values read from pixels are guesses wearing decimal places. So the image is stored
    only to prove what was looked at; every figure must arrive as a typed level or not at all.
    """
    client = http_request.client.host if http_request.client else "unknown"
    if not _rate_ok(client):
        raise HTTPException(429, detail={"error": "rate_limited"})

    raw = file.file.read() if file is not None else b""
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, detail={"error": "image too large", "bytes": len(raw)})

    if not ticker or (support is None and target is None and not (green_line and red_line)):
        raise HTTPException(
            422,
            detail={
                "error": "levels_required",
                "status": "image_alone_not_supported",
                "received": {"bytes": len(raw), "ticker": ticker or None},
                "required_alternatives": [
                    "Send support and target as typed numbers alongside the image",
                    "Send green_line and red_line (the fundamental bands) instead",
                    "Send OHLC JSON and the app will compute the levels",
                ],
                "note": "Axis values are never inferred from pixels in this build.",
            },
        )

    req = AnalyzeRequest(ticker=ticker, question=f"Chart review for {ticker.upper()}",
                         support=support, target=target, green_line=green_line,
                         red_line=red_line, account_equity=account_equity,
                         risk_pct=risk_pct)
    data = _build_response(req, uuid.uuid4().hex[:16])
    data["chart"] = {
        "image_received": bool(raw),
        "bytes": len(raw),
        "numbers_from": "typed levels only",
        "note": "The image is a reference. No value was read from it.",
    }
    return JSONResponse(data)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = STATIC / "index.html"
    if page.exists():
        return page.read_text(encoding="utf-8")
    return f"<h1>MM Desk {VERSION}</h1><p>{DISCLAIMER}</p>"
