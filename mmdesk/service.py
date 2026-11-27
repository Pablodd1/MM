"""Decision Desk HTTP service.

One endpoint does the work: POST /api/analyze takes a question and a ticker, grounds
the numbers, runs both lenses, reconciles them, writes the narrative and journals the
card. Everything it returns is either fetched, computed, or cited — and the response
says which.

Everything is per-tenant. A tenant is identified by the ``X-Tenant`` header (or ``?tenant=``),
never inferred. The default tenant has no material and no adapter, so the product stands on the
deterministic engine alone; a tenant becomes specific only when it uploads material and, if it
wants one, trains a Layer 2 adapter that must pass its own gate before it can be called.

Who may do what:

* analysing and quoting is open to anyone who names a tenant — the id is an address, not a secret;
* creating tenants and registering or activating adapters is loopback-only, or needs
  ``MMDESK_ADMIN_TOKEN``;
* a journal is readable only from loopback, with the admin token, or with the ``X-Journal-Id``
  the entries were written under. Journals hold account sizes and positions.

Run:  uvicorn mmdesk.service:app --port 8791
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import conversation as conversation_mod
from . import opinion as opinion_mod
from . import persona as persona_mod
from . import rubric
from . import rules
from . import scenarios as scenario_mod
from . import tenants as tenant_mod
from . import voice as voice_mod
from .card import DISCLAIMER, VERSION
from .data import Grounder, intraday_supported
from .decision import build_decision_card
from .journal import Journal, summary
from .llm import LLMConfig, narrate, template_narrative
from .retrieval_shim import (ambiguous, corpus_available, corpus_coverage, passage_count,
                             refusal_reason, search)
from .rules import Account, Setup
from .standard import atr, range_position, realised_vol, standard_lens, trend_score

log = logging.getLogger("mmdesk.service")

STATIC = Path(__file__).resolve().parent / "static"

MAX_VOICE_BYTES = 4 * 1024 * 1024      # within the 4.5 MB body limit of a serverless deployment
MAX_CHART_BYTES = 4 * 1024 * 1024
MAX_SCORE_INSTRUMENTS = 50
MAX_SCORE_ROWS = 5000
MAX_GRADE_INSTRUMENTS = 25

app = FastAPI(title="Decision Desk", version=VERSION,
              description="Decision support built on attributed rules, with cited sources.")

# --- request context: an id, a log line and baseline headers ------------------------------

_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    # frame-ancestors / base-uri / form-action / object-src restrict where the page can be
    # embedded and what it can submit to, without touching its inline script.
    (b"content-security-policy",
     b"frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"),
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class RequestContext:
    """Pure ASGI, so an unhandled error is logged once with its id and nothing is buffered."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = next((v.decode("latin-1") for k, v in scope.get("headers", [])
                         if k == b"x-request-id"), "")
        # A caller-supplied id is echoed only if it is a plain token: it ends up in the log.
        rid = supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = rid
        status = {"code": 500}
        started = time.perf_counter()

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", rid.encode()))
                headers.extend(_SECURITY_HEADERS)
            await send(message)

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.error("request %s %s %s raised", rid, method, path, exc_info=True)
            raise
        finally:
            log.info("%s %s %s %s %.0fms", rid, method, path, status["code"],
                     (time.perf_counter() - started) * 1000)


app.add_middleware(RequestContext)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """A 422 that names the fields — and never a 500.

    The default handler echoes the offending input back. An input like `1e999` is rejected by
    validation and then cannot be JSON-encoded, so the error response itself raised.
    """
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]})


# --- minimal abuse protection: 30 requests / minute / client ------------------------------

RATE_LIMIT = int(os.environ.get("MMDESK_RATE_LIMIT", "30"))
_RATE_WINDOW = 60.0
_RATE_MAX_KEYS = 10_000
_hits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _client_key(request: Request) -> str:
    """Who to count this request against.

    On Vercel the caller's address travels in a header the caller cannot set; everywhere else
    the socket peer is used. Trusting X-Forwarded-For without a known proxy in front would let
    anyone mint a fresh bucket per request.
    """
    if os.environ.get("VERCEL"):
        forwarded = request.headers.get("x-vercel-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64]
    host = request.client.host if request.client else ""
    return host or "unknown"


def _rate_ok(key: str) -> bool:
    now = time.time()
    with _rate_lock:
        bucket = [t for t in _hits.get(key, ()) if now - t < _RATE_WINDOW]
        if len(bucket) >= RATE_LIMIT:
            # Empty buckets are dropped as they age out: the table used to keep one entry per
            # address forever.
            if bucket:
                _hits[key] = bucket
            else:
                _hits.pop(key, None)
            return False
        bucket.append(now)
        _hits[key] = bucket
        if len(_hits) > _RATE_MAX_KEYS:
            # And it is capped even when every caller is active: idle buckets go first, then
            # the least recently seen.
            order = sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0.0)
            for stale in order[:len(_hits) - _RATE_MAX_KEYS]:
                del _hits[stale]
        return True


def _admit(request: Request) -> None:
    if not _rate_ok(_client_key(request)):
        raise HTTPException(429, detail={"error": "rate_limited", "limit_per_minute": RATE_LIMIT},
                            headers={"Retry-After": "60"})


# --- who is asking -------------------------------------------------------------------------

def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    # "testclient" is the address FastAPI's test client reports; a socket peer is never that string.
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def _admin_token_ok(request: Request, token: str) -> bool:
    supplied = request.headers.get("X-Admin-Token", "")
    return bool(supplied) and secrets.compare_digest(supplied.encode(), token.encode())


def _require_admin(request: Request) -> None:
    """Admin actions are loopback-only unless an explicit token is configured.

    A tenant id in a header is an address, not a credential; without this check anyone who can
    reach the service could create tenants or activate an adapter that never passed its gate.
    """
    token = os.environ.get("MMDESK_ADMIN_TOKEN", "")
    if token:
        if not _admin_token_ok(request, token):
            raise HTTPException(401, detail={"error": "admin_token_required"})
        return
    # With no token, loopback is trusted — but never on a serverless host, where the peer
    # address is whatever the platform's bridge reports rather than the caller's.
    if os.environ.get("VERCEL") or not _is_loopback(request):
        raise HTTPException(403, detail={"error": "admin_loopback_only",
                                         "note": "Set MMDESK_ADMIN_TOKEN to administer remotely."})


def _journal_scope(request: Request) -> str | None:
    raw = request.headers.get("X-Journal-Id")
    if raw is None:
        return None
    if not tenant_mod.JOURNAL_ID.match(raw):
        raise HTTPException(422, detail={
            "error": "bad_journal_id",
            "note": "X-Journal-Id must be 8-64 letters, digits, '-' or '_'",
        })
    return raw


def _require_journal_access(request: Request, scope: str | None) -> None:
    """A journal names positions and, through the sizing, the account behind them.

    It is readable from loopback, with the admin token, or under the scope the caller wrote.
    Anyone else gets a 403: the tenant id alone used to be enough to read every entry.
    """
    token = os.environ.get("MMDESK_ADMIN_TOKEN", "")
    if token and _admin_token_ok(request, token):
        return
    if scope is not None:
        return
    if not os.environ.get("VERCEL") and _is_loopback(request):
        return
    raise HTTPException(403, detail={
        "error": "journal_private",
        "note": "Send the X-Journal-Id the entries were written under, or an admin token.",
    })


def _tenant_from(http_request: Request) -> tenant_mod.Tenant:
    """Resolve the tenant, or refuse. An unknown id is never quietly treated as the default."""
    tid = (http_request.headers.get("X-Tenant")
           or http_request.query_params.get("tenant")
           or tenant_mod.DEFAULT_TENANT)
    t = tenant_mod.get(tid)
    if t is None:
        raise HTTPException(404, detail={"error": "unknown_tenant", "tenant": tid,
                                         "note": "Create the tenant before sending requests for it."})
    return t


def _journal(tenant: tenant_mod.Tenant, scope: str | None = None) -> Journal:
    return Journal(tenant.journal_path(scope))


# --- requests ------------------------------------------------------------------------------

_TICKER = re.compile(r"^[A-Za-z^=][A-Za-z0-9.\-=]{0,11}$")
_COMPONENT = re.compile(r"[a-z0-9_]{1,40}")


class _OrderedBands(BaseModel):
    green_line: float | None = Field(None, gt=0, allow_inf_nan=False,
                                     description="fundamental fair-value line (score 10)")
    red_line: float | None = Field(None, gt=0, allow_inf_nan=False,
                                   description="fundamental too-expensive line (score 1)")

    @model_validator(mode="after")
    def _bands_ordered(self):
        """Reject inverted fundamental bands at the edge, not with a 500 from inside the rubric."""
        if self.green_line is not None and self.red_line is not None:
            if self.red_line <= self.green_line:
                raise ValueError("red_line (too expensive) must sit above green_line (fair value)")
        return self


class AnalyzeRequest(_OrderedBands):
    question: str = Field("", max_length=2000)
    ticker: str = Field(..., min_length=1, max_length=12, pattern=_TICKER.pattern)
    mode: Literal["swing", "intraday"] = "swing"
    account_equity: float = Field(10_000, gt=0, le=1e12, allow_inf_nan=False)
    risk_pct: float = Field(0.01, gt=0, le=0.05, allow_inf_nan=False)
    alert: Literal["green", "yellow", "orange", "red"] = "green"
    cash_pct: float | None = Field(None, ge=0, le=1, allow_inf_nan=False)
    min_cash_pct: float = Field(0.0, ge=0, lt=1, allow_inf_nan=False)
    support: float | None = Field(None, gt=0, allow_inf_nan=False)
    target: float | None = Field(None, gt=0, allow_inf_nan=False)
    time_stop_days: int | None = Field(None, gt=0, le=3650)
    components: dict[str, int] | None = None
    commission_per_share: float = Field(0.0, ge=0, le=50, allow_inf_nan=False)
    fixed_fee_per_order: float = Field(0.0, ge=0, le=500, allow_inf_nan=False)
    slippage_bps: float = Field(0.0, ge=0, le=200, allow_inf_nan=False)
    existing_open_risk: float = Field(0.0, ge=0, le=1e12, allow_inf_nan=False)
    allow_fractional_shares: bool = False

    @field_validator("components")
    @classmethod
    def _components_bounded(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        """Each component is 0..10. An out-of-range value used to escape as a 500."""
        if value is None:
            return value
        if len(value) > 12:
            raise ValueError("at most 12 components")
        for name, score in value.items():
            if not _COMPONENT.fullmatch(name):
                raise ValueError(f"component name {name!r} must be lower-case letters, digits or '_'")
            if not 0 <= score <= 10:
                raise ValueError(f"component {name!r} must be 0..10, got {score}")
        return value


class OpinionRequest(AnalyzeRequest):
    """Same inputs as an analysis, because the opinion is a reading of that analysis."""


class LensRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)


class ScenarioRequest(_OrderedBands):
    ticker: str = Field(..., min_length=1, max_length=12, pattern=_TICKER.pattern)


class CrowdRequest(BaseModel):
    rounds: int = Field(30, ge=0, le=500)
    topology: str = Field("ring", pattern="^(ring|clique|two_camps)$")
    trust: float = Field(0.5, ge=0.0, le=1.5, allow_inf_nan=False)
    model: str = Field("bounded_confidence", pattern="^(averaging|bounded_confidence)$")
    tolerance: float = Field(0.5, ge=0.05, le=2.0, allow_inf_nan=False)


class TenantCreate(BaseModel):
    id: str = Field(..., pattern=tenant_mod.TENANT_ID.pattern)
    name: str = Field(..., min_length=1, max_length=80)
    lens_label: str | None = Field(None, max_length=60)
    attribution: str | None = Field(None, max_length=240)


def _too_large(request: Request, limit: int) -> bool:
    """Refuse by the declared size before reading the body. Multipart adds a little overhead."""
    length = request.headers.get("content-length", "")
    return length.isdigit() and int(length) > limit + 64 * 1024


# --- the card ------------------------------------------------------------------------------

def _levels(g, request: AnalyzeRequest) -> tuple[float, float, list[str]]:
    """Support/target: user input wins; otherwise computed and labelled as such."""
    notes: list[str] = []
    bars = g.bars
    price = bars.close[-1]
    measured = atr(bars)
    a = measured or price * 0.02
    if measured is None:
        notes.append(
            "ATR could not be measured (fewer than 15 bars); a stand-in of 2% of price is used"
        )

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


def _build_response(request: AnalyzeRequest, request_id: str,
                    tenant: tenant_mod.Tenant | None = None, journal_scope: str | None = None,
                    journal: bool = True) -> dict:
    """Ground, score, reconcile, journal. Shared by the text, chart and voice endpoints.

    `journal=False` is for the second opinion, which re-runs this to see the same numbers: a
    second entry for the same question would have double-counted the call in the track record.
    """
    tenant = tenant or tenant_mod.get(tenant_mod.DEFAULT_TENANT)
    assert tenant is not None
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
    fundamentals = getattr(g, "fundamentals", None) or {}
    price_bar = getattr(g, "last_bar", None)

    if request.green_line and request.red_line:
        # The band mechanic: price between fundamental valuation lines the user supplies.
        fv = rubric.fair_value_score(price, request.green_line, request.red_line)
        comps = {"fundamental_bands": fv["score_10"]}
        score_meta = fv
        # A single component means rules.score_10() returns the band score unchanged.
    elif request.components:
        comps = request.components
        score_meta = {"score_10": rules.score_10(comps), "components": comps,
                      "method": "user_components", "source": "user-supplied components"}
    else:
        scored = rubric.score(rubric.RubricInputs(
            price=price, support=support, target=target, atr=a,
            trend_score=tr["score"],
            days_to_earnings=_days_to_earnings(fundamentals.get("earnings_date")),
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

    hits = (search(f"{request.question} {request.ticker}", k=5, tenant_id=tenant.id)
            if request.question else search(request.ticker, k=5, tenant_id=tenant.id))

    card = build_decision_card(
        question=request.question or f"Analyse {request.ticker}",
        setup=setup, account=account, standard=std,
        corpus_hits=hits,
        grounding={
            "as_of": g.as_of,
            "price_bar": price_bar,
            "sources": g.sources,
            "missing": g.missing,
            "level_notes": level_notes,
            "intraday_supported": intraday_supported(),
        },
    )
    card.lens["score_meta"] = score_meta
    card.lens["label"] = tenant.lens_label
    if tenant.attribution:
        card.attribution = tenant.attribution
    if request.mode == "intraday" and not intraday_supported():
        card.falsifiers.append(
            "Intraday mode ran on daily data: the free feed is delayed, so any intraday "
            "conclusion here is indicative only."
        )

    data = card.to_dict()
    if not hits:
        data["corpus_refusal"] = refusal_reason(tenant.id)

    # The opinion is always shown. Without a model it is composed deterministically from the
    # card's own fields and labelled as such; with one, the model writes the prose and is
    # labelled by name. A tenant with its own adapter keeps its material off the hosted provider.
    provider = opinion_mod.provider(tenant)
    if provider["kind"] == "adapter":
        data["narrative"] = narrate(data, hits, endpoint=provider)
    elif os.environ.get("MMDESK_ALLOW_CLOUD_LLM") == "1":
        data["narrative"] = narrate(data, hits)
    else:
        data["narrative"] = template_narrative(data, "cloud model disabled")
    card.narrative = data["narrative"]

    if journal:
        try:
            entry = _journal(tenant, journal_scope).append(
                card.to_dict() | {"narrative": data["narrative"]})
            data["journal_id"] = entry.id
        except ValueError as exc:
            log.warning("journal write for tenant %s failed: %s", tenant.id, exc)
            data["journal_error"] = "the journal entry could not be written"

    data["observability"] = {
        "request_id": request_id,
        "data_timestamp": g.as_of,
        "price_bar": price_bar,
        "market_data_source": ", ".join(sorted({str(x.get("provider")) for x in g.sources})) or "none",
        "market_data_delay": "daily bars, not real-time",
        "corpus_coverage": corpus_coverage(tenant.id),
        "rules_version": VERSION,
        "rubric_version": score_meta.get("method", "unknown"),
        "tenant": tenant.id,
        "llm_provider": (
            f"{provider['kind']}: {provider['model']}" if provider["key"] and
            os.environ.get("MMDESK_ALLOW_CLOUD_LLM") == "1" else "none (template)"
        ),
        "missing_fields": g.missing,
        "disclaimer_version": VERSION,
    }
    try:
        tenant_mod.record_decision(tenant.id, data.get("journal_id") or request_id,
                                   request.ticker.upper(), data.get("verdict", ""), data)
    except Exception:  # noqa: BLE001 - audit write must not break the card
        log.warning("decision audit write failed", exc_info=True)
        data.setdefault("observability", {})["audit_error"] = "the decision could not be recorded"

    data["markdown"] = card.to_markdown()
    return data


# --- health --------------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, Any]:
    default = tenant_mod.get(tenant_mod.DEFAULT_TENANT)
    assert default is not None
    cfg = LLMConfig.from_env()
    journal = _journal(default)
    return {
        "ok": True,
        "version": VERSION,
        "tenant": default.id,
        "corpus": {"available": corpus_available(default.id),
                   "passages": passage_count(default.id)},
        "llm": {
            "configured": cfg.configured,
            "cloud_enabled": os.environ.get("MMDESK_ALLOW_CLOUD_LLM") == "1",
            "model": cfg.model if cfg.configured else None,
            "layer2": ("adapter" if default.active_adapter else
                       ("hosted" if opinion_mod.available(default) else "template")),
        },
        "corpus_coverage": corpus_coverage(default.id),
        # A count, not the roster: tenant names are the operator's business.
        "tenants": len(tenant_mod.all_tenants()),
        "intraday_supported": intraday_supported(),
        "voice": voice_mod.status(),
        "journal_entries": len(journal.all()),
        "journal_damaged_lines": journal.damaged,
        "journal_persistent": tenant_mod.persistent(),
        "registry_persistent": tenant_mod.persistent(),
    }


# --- analysis ------------------------------------------------------------------------------

@app.post("/api/analyze")
def analyze(request: AnalyzeRequest, http_request: Request) -> JSONResponse:
    _admit(http_request)
    tenant = _tenant_from(http_request)
    rid = http_request.state.request_id
    return JSONResponse(_build_response(request, rid, tenant,
                                        journal_scope=_journal_scope(http_request)))


def _analyzable(payload: dict[str, Any]) -> AnalyzeRequest:
    """Validate fields that arrive as form fields rather than as a JSON body."""
    try:
        return AnalyzeRequest(**payload)
    except ValidationError as exc:
        errors = [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                  for e in exc.errors()]
        raise HTTPException(422, detail={"error": "invalid_input", "fields": errors}) from exc


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
    """Push-to-talk: audio in, transcript + card + spoken answer out."""
    _admit(http_request)
    tenant = _tenant_from(http_request)
    scope = _journal_scope(http_request)
    if _too_large(http_request, MAX_VOICE_BYTES):
        raise HTTPException(413, detail={"error": "audio too large",
                                         "limit_bytes": MAX_VOICE_BYTES})
    raw = audio.file.read(MAX_VOICE_BYTES + 1)
    if not raw:
        raise HTTPException(422, detail={"error": "empty upload"})
    if len(raw) > MAX_VOICE_BYTES:
        raise HTTPException(413, detail={"error": "audio too large", "bytes": len(raw),
                                         "limit_bytes": MAX_VOICE_BYTES})
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
        except Exception as exc:  # noqa: BLE001 - a model crash is not the caller's fault
            log.warning("transcription failed", exc_info=True)
            raise HTTPException(502, detail={"error": "voice_failed",
                                             "note": "speech-to-text raised an error"}) from exc
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

        req = _analyzable({
            "question": routed["question"], "ticker": routed["ticker"],
            "account_equity": account_equity, "risk_pct": risk_pct, "alert": alert,
            "green_line": green_line, "red_line": red_line,
        })
        request_id = http_request.state.request_id
        try:
            data = _build_response(req, request_id, tenant, journal_scope=scope)
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
        mp3 = voice_mod.to_mp3(wav)
    except Exception:  # noqa: BLE001 - losing the audio must not lose the card
        log.warning("speech synthesis failed", exc_info=True)
        return None, "audio/wav"
    if mp3:
        import base64

        return base64.b64encode(mp3).decode("ascii"), "audio/mpeg"
    import base64

    return base64.b64encode(wav).decode("ascii"), "audio/wav"


@app.post("/api/voice/selftest")
def voice_selftest(http_request: Request) -> JSONResponse:
    """Run the whole loop on a synthetic question: TTS -> STT -> route -> card -> TTS.

    This is the honest way to prove the voice path works without a microphone, and it is
    the same code path the browser uses.
    """
    _admit(http_request)
    tenant = _tenant_from(http_request)
    scope = _journal_scope(http_request)
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
        except Exception as exc:  # noqa: BLE001
            log.warning("voice selftest failed", exc_info=True)
            raise HTTPException(502, detail={"error": "voice_failed"}) from exc
        routed = voice_mod.route(transcript)
        request_id = http_request.state.request_id
        data = _build_response(
            _analyzable({"question": routed["question"], "ticker": routed["ticker"] or "NVDA",
                         "account_equity": 50_000}),
            request_id, tenant, journal_scope=scope,
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


# --- journal -------------------------------------------------------------------------------

def _entry_view(e) -> dict[str, Any]:
    return {"id": e.id, "instrument": e.instrument, "verdict": e.verdict,
            "price_at_call": e.price_at_call, "created_at": e.created_at,
            "settled": e.settled, "outcomes": e.outcomes}


@app.get("/api/journal")
def journal_list(http_request: Request) -> dict[str, Any]:
    tenant = _tenant_from(http_request)
    scope = _journal_scope(http_request)
    _require_journal_access(http_request, scope)
    journal = _journal(tenant, scope)
    entries = journal.all()
    return {
        "count": len(entries),
        "summary": summary(entries),
        "damaged_lines": journal.damaged,
        "persistent": tenant_mod.persistent(),
        "scope": "own" if scope else "shared",
        "entries": [_entry_view(e) for e in entries],
    }


def _clean_series(payload: dict[str, Any]) -> dict[str, list]:
    prices = payload.get("prices")
    if not isinstance(prices, dict) or not prices:
        raise HTTPException(422, detail={
            "error": "prices_required",
            "note": 'Send {"prices": {"TICKER": [["YYYY-MM-DD", close], ...]}}',
        })
    if len(prices) > MAX_SCORE_INSTRUMENTS:
        raise HTTPException(413, detail={"error": "too_many_instruments",
                                         "limit": MAX_SCORE_INSTRUMENTS})
    cleaned: dict[str, list] = {}
    for ticker, rows in prices.items():
        if not isinstance(ticker, str) or not _TICKER.match(ticker) or not isinstance(rows, list):
            raise HTTPException(422, detail={"error": "bad_series", "ticker": str(ticker)[:12]})
        if len(rows) > MAX_SCORE_ROWS:
            raise HTTPException(413, detail={"error": "too_many_bars", "ticker": ticker,
                                             "limit": MAX_SCORE_ROWS})
        cleaned[ticker.upper()] = rows
    return cleaned


@app.post("/api/journal/score")
def journal_score(payload: dict[str, Any], http_request: Request) -> dict[str, Any]:
    """What a supplied price series *would* grade. Nothing here is ever written.

    This endpoint takes the caller's prices, so it used to be a way to write any outcome into
    the track record — permanently, because a written outcome was never recomputed. Only
    `/api/journal/grade`, which fetches the bars itself, may persist.
    """
    _admit(http_request)
    tenant = _tenant_from(http_request)
    scope = _journal_scope(http_request)
    _require_journal_access(http_request, scope)
    if _too_large(http_request, 2 * 1024 * 1024):
        raise HTTPException(413, detail={"error": "body too large"})
    cleaned = _clean_series(payload)
    try:
        scored = _journal(tenant, scope).score_entries(cleaned, persist=False)
    except ValueError as exc:
        raise HTTPException(422, detail={"error": "bad_bars", "note": str(exc)[:300]}) from exc
    return {"scored": sum(1 for e in scored if e.outcomes), "persisted": False,
            "summary": summary(scored),
            "entries": [_entry_view(e) for e in scored if e.outcomes]}


@app.post("/api/journal/grade")
def journal_grade(http_request: Request) -> dict[str, Any]:
    """Grade open entries against real, split-adjusted OHLC bars fetched here.

    The caller does not supply the prices, so they cannot be chosen after the fact. Stops are
    simulated with gap fills, stored levels are rescaled if the series was adjusted since the
    call, and a >25% move that does not touch the stop is flagged as a suspected corporate
    action rather than scored as a loss.
    """
    _admit(http_request)
    tenant = _tenant_from(http_request)
    scope = _journal_scope(http_request)
    _require_journal_access(http_request, scope)
    journal = _journal(tenant, scope)
    entries = journal.all()
    if not entries:
        return {"graded": 0, "note": "no journal entries yet"}

    grounder = Grounder()
    series: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    # One provider fetch per distinct instrument: capped per call, because the journal is
    # caller-writable and every tenant shares the egress address.
    wanted = sorted({e.instrument for e in entries})
    deferred = wanted[MAX_GRADE_INSTRUMENTS:]
    for ticker in wanted[:MAX_GRADE_INSTRUMENTS]:
        try:
            g = grounder.ground(ticker, fundamentals=False)
        except Exception as exc:  # noqa: BLE001
            errors[ticker] = f"{type(exc).__name__}"
            continue
        if g.bars is None or not g.bars.dates or g.bars.open is None:
            errors[ticker] = "no OHLC with dates from the provider"
            continue
        series[ticker] = [
            {"date": d, "open": o, "high": h, "low": low, "close": c}
            for d, o, h, low, c in zip(g.bars.dates, g.bars.open,
                                       g.bars.high or g.bars.close,
                                       g.bars.low or g.bars.close, g.bars.close, strict=True)
        ]

    scored = journal.score_entries(series, adjusted=True, source="yfinance auto_adjust")
    return {
        "graded": sum(1 for e in scored if e.outcomes),
        "instruments": list(series),
        "deferred": deferred,
        "errors": errors,
        "damaged_lines": journal.damaged,
        "summary": summary(scored),
        "entries": [_entry_view(e) for e in scored if e.outcomes],
    }


# --- crowd, lens, chart --------------------------------------------------------------------

@app.post("/api/scenario")
def scenario(request: ScenarioRequest, http_request: Request) -> dict[str, Any]:
    """Six rule-based styles read the same data. The spread is the finding, not the mode."""
    _admit(http_request)
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
    volatility = std["volatility_60d_annualised"] or std["volatility_20d_annualised"]
    if volatility is None:
        raise HTTPException(422, detail={"error": "not_enough_bars", "ticker": request.ticker,
                                         "note": "the crowd needs at least 21 daily bars"})
    high = max(bars.high[-60:] if bars.high else bars.close[-60:])
    momentum = (price / bars.close[-21] - 1) if len(bars.close) > 21 else 0.0
    range_pos = range_position(bars)["pct_of_range"]
    facts = scenario_mod.Facts(
        ticker=request.ticker.upper(), price=price, score_10=score,
        trend=std["trend"]["score"], momentum=momentum,
        volatility=volatility,
        drawdown=(price / high - 1) if high else 0.0,
        # `or 0.5` turned a true 0.0 — price at the low of its range — into the middle.
        range_pos=0.5 if range_pos is None else range_pos,
    )
    out = scenario_mod.run(facts)
    out["observability"] = {"data_timestamp": g.as_of, "source": f"{g.bars.source} daily bars",
                            "account_sizing": False}
    return out


@app.post("/api/crowd/talk")
def crowd_talk(request: CrowdRequest, http_request: Request) -> dict[str, Any]:
    """Let the six styles influence each other for N rounds and report what happens.

    No language model, no tokens: this shows whether a crowd converges or splits, which is the
    part of a simulated debate that costs nothing to run and survives scrutiny.
    """
    _admit(http_request)
    agents = conversation_mod.starter_crowd()
    out = conversation_mod.run(agents, rounds=request.rounds, topology=request.topology,
                               trust=request.trust, model=request.model,
                               tolerance=request.tolerance)
    out["label"] = (
        "Simulated influence between six rule-based styles. No language model is involved, no "
        "text is generated, and nothing here is a forecast."
    )
    return out


@app.post("/api/lens")
@app.post("/api/lens/quote")
def lens_quote(request: LensRequest, http_request: Request) -> dict[str, Any]:
    """What the tenant's own material says: a verbatim quote with a timestamp, or a refusal.

    This is the only endpoint that may put sentences in the author's voice, and it does so by
    selecting a contiguous run of words from one cited passage — never by generating them.
    """
    _admit(http_request)
    tenant = _tenant_from(http_request)
    hits = search(request.question, k=5, tenant_id=tenant.id)
    payload = persona_mod.speak(request.question, hits, tenant_id=tenant.id).to_dict()
    payload["ambiguous"] = ambiguous(request.question, tenant.id)
    payload["coverage"] = corpus_coverage(tenant.id)
    payload["tenant"] = tenant.id
    payload["lens_label"] = tenant.lens_label
    if not hits:
        payload["note"] = ("No material is loaded for this lens, or nothing in it covers the "
                           "question. The lens refuses rather than inventing a position.")
    return payload


@app.post("/api/chart")
def chart_upload(
    http_request: Request,
    file: UploadFile | None = File(None),
    ticker: str = Form(""),
    support: float | None = Form(None),
    target: float | None = Form(None),
    green_line: float | None = Form(None),
    red_line: float | None = Form(None),
    account_equity: float = Form(50_000),
    risk_pct: float = Form(0.01),
) -> JSONResponse:
    """A chart is accepted as a *reference image*, never as a source of numbers.

    Axis values read from pixels are guesses wearing decimal places. So only the fact of the
    image is recorded; every figure must arrive as a typed level or not at all.
    """
    _admit(http_request)
    tenant = _tenant_from(http_request)
    scope = _journal_scope(http_request)
    if _too_large(http_request, MAX_CHART_BYTES):
        raise HTTPException(413, detail={"error": "image too large", "limit_bytes": MAX_CHART_BYTES})
    # Bounded read: the bytes are counted and discarded, so there is no reason to buffer more.
    received = 0 if file is None else len(file.file.read(MAX_CHART_BYTES + 1))
    if received > MAX_CHART_BYTES:
        raise HTTPException(413, detail={"error": "image too large", "limit_bytes": MAX_CHART_BYTES})

    if not ticker or (support is None and target is None and not (green_line and red_line)):
        raise HTTPException(
            422,
            detail={
                "error": "levels_required",
                "status": "image_alone_not_supported",
                "received": {"bytes": received, "ticker": ticker or None},
                "required_alternatives": [
                    "Send support and target as typed numbers alongside the image",
                    "Send green_line and red_line (the fundamental bands) instead",
                    "Send OHLC JSON and the app will compute the levels",
                ],
                "note": "Axis values are never inferred from pixels in this build.",
            },
        )

    req = _analyzable({"ticker": ticker, "question": f"Chart review for {ticker.upper()}",
                       "support": support, "target": target, "green_line": green_line,
                       "red_line": red_line, "account_equity": account_equity,
                       "risk_pct": risk_pct})
    data = _build_response(req, http_request.state.request_id, tenant, journal_scope=scope)
    data["chart"] = {
        "image_received": received > 0,
        "bytes": received,
        "numbers_from": "typed levels only",
        "note": "The image is a reference. No value was read from it.",
    }
    return JSONResponse(data)


# --- second opinion ------------------------------------------------------------------------

@app.post("/api/opinion")
def api_opinion(request: OpinionRequest, http_request: Request):
    """The second-opinion endpoint: a model's reading of a card that was already computed.

    It runs the same analysis so the model sees the numbers the page shows, then asks for two
    voices — the disciplined analysis and the material's, grounded in the retrieved passages. It
    does not journal: that would count one question twice in the track record. Nothing the model
    returns is treated as a number; callers render figures from the card.
    """
    _admit(http_request)
    tenant = _tenant_from(http_request)
    card = _build_response(request, http_request.state.request_id, tenant,
                           journal_scope=_journal_scope(http_request), journal=False)
    hits = search(request.question, k=4, tenant_id=tenant.id) if request.question else []
    result = opinion_mod.opinion(card, hits, tenant=tenant)
    result["available"] = True
    result["tenant"] = tenant.id
    result["lens_label"] = tenant.lens_label
    result["attribution"] = tenant.attribution
    result["citations"] = [
        {"video_id": h.get("video_id"), "stamp": h.get("stamp"),
         "video_title": h.get("video_title"), "url": h.get("url"), "text": h.get("text"),
         "synthetic": h.get("synthetic", False)}
        for h in hits[:3]
    ]
    return JSONResponse(result)


# --- tenancy -------------------------------------------------------------------------------

@app.get("/api/tenants")
def tenants_list(http_request: Request) -> dict[str, Any]:
    """The full roster is the operator's: ids are otherwise enumerable from journal access."""
    _require_admin(http_request)
    return {"count": len(tenant_mod.all_tenants()),
            "tenants": [t.public() for t in tenant_mod.all_tenants()]}


@app.get("/api/tenants/{tenant_id}")
def tenant_get(tenant_id: str, http_request: Request) -> dict[str, Any]:
    """What any caller may know about a tenant whose id they already have."""
    t = tenant_mod.get(tenant_id)
    if t is None:
        raise HTTPException(404, detail={"error": "unknown_tenant", "tenant": tenant_id})
    return t.public()


@app.post("/api/tenants")
def tenants_create(payload: TenantCreate, http_request: Request) -> dict[str, Any]:
    _require_admin(http_request)
    if not tenant_mod.persistent():
        # On a serverless host the registry lives in a per-instance temp dir: a tenant created
        # here returns 404 on the next instance. Refusing is kinder than losing it quietly.
        raise HTTPException(503, detail={
            "error": "registry_ephemeral",
            "note": "This host cannot keep a tenant registry across requests. Set "
                    "MMDESK_DATA_DIR to persistent storage, or create the tenant on a "
                    "persistent host.",
        })
    if tenant_mod.get(payload.id) is not None:
        raise HTTPException(409, detail={"error": "tenant_exists", "tenant": payload.id})
    try:
        t = tenant_mod.create(payload.id, payload.name, payload.lens_label, payload.attribution)
    except ValueError as exc:
        raise HTTPException(409, detail={"error": "tenant_exists", "tenant": payload.id}) from exc
    log.info("tenant %s created", payload.id)
    return t.public()


@app.post("/api/tenants/{tenant_id}/adapter/activate")
def adapter_activate(tenant_id: str, http_request: Request,
                     force: bool = False) -> dict[str, Any]:
    """Promote a trained adapter only if its own recorded evaluation clears the bar."""
    _require_admin(http_request)
    token = os.environ.get("MMDESK_ADMIN_TOKEN", "")
    if force:
        if not token or not _admin_token_ok(http_request, token):
            raise HTTPException(403, detail={"error": "force_requires_admin_token"})
    try:
        adapter = tenant_mod.activate_adapter(tenant_id, require_gate=not force)
    except KeyError as exc:
        raise HTTPException(404, detail={"error": "unknown_tenant", "tenant": tenant_id}) from exc
    except ValueError as exc:
        raise HTTPException(409, detail={"error": "gate_failed", "detail": str(exc)}) from exc
    log.info("adapter activated for tenant %s (forced=%s)", tenant_id, force)
    return {"tenant": tenant_id, "adapter": adapter}


@app.post("/api/tenants/{tenant_id}/adapter")
def adapter_register(tenant_id: str, payload: dict[str, Any], http_request: Request) -> dict[str, Any]:
    """Record a trained candidate. It stays inert: activation is a separate, gated call.

    Whatever status the payload claims, it is stored as a candidate. `"status": "active"` used
    to be accepted here and made an adapter routable without any evaluation.
    """
    _require_admin(http_request)
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(422, detail={"error": "adapter_object_required"})
    if tenant_mod.get(tenant_id) is None:
        raise HTTPException(404, detail={"error": "unknown_tenant", "tenant": tenant_id})
    if not str(payload.get("path") or "").strip():
        raise HTTPException(422, detail={"error": "adapter_path_required",
                                         "note": "a candidate needs the path of its weights"})
    try:
        base = str(payload.get("base_url") or "http://127.0.0.1:8899/v1")
        if not opinion_mod.endpoint_allowed(base):
            raise HTTPException(422, detail={"error": "base_url_refused",
                                             "note": "http(s) only, and not a link-local or "
                                                     "metadata address"})
        tenant_mod.set_adapter(tenant_id, payload | {"base_url": base})
    except ValueError as exc:
        raise HTTPException(409, detail={"error": "adapter_refused", "detail": str(exc)}) from exc
    stored = tenant_mod.get(tenant_id)
    return {"tenant": tenant_id, "adapter": (stored.candidate or {}) if stored else payload,
            "note": "stored as a candidate; activate it to make it routable"}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = STATIC / "index.html"
    body = page.read_text(encoding="utf-8") if page.exists() else (
        f"<h1>Decision Desk {VERSION}</h1><p>{DISCLAIMER}</p>"
    )
    return HTMLResponse(body, headers={"cache-control": "no-cache"})
