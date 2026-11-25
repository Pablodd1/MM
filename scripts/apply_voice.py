"""Add the voice endpoint by splitting analyze() into a reusable builder (run once)."""

from __future__ import annotations

from pathlib import Path

p = Path("mmdesk/service.py")
s = p.read_text(encoding="utf-8")

start = s.index('@app.post("/api/analyze")')
end = s.index('@app.get("/api/journal")')
body = s[start:end]

# The old body, minus the decorator and the rate-limit preamble, becomes _build_response.
inner = body.split('    request_id = uuid.uuid4().hex[:16]\n', 1)[1]
inner = inner.rsplit("    return JSONResponse(data)", 1)[0]

new = '''def _build_response(request: AnalyzeRequest, request_id: str) -> dict:
    """Ground, score, reconcile, journal. Shared by the text and voice endpoints."""
''' + inner + '''    return data


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
            return JSONResponse({
                "transcript": transcript,
                "ticker": None,
                "reply": spoken,
                "audio_b64": _speak_b64(spoken),
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
            return JSONResponse(
                {
                    "transcript": transcript,
                    "ticker": routed["ticker"],
                    "reply": spoken,
                    "audio_b64": _speak_b64(spoken),
                    "timings": timings,
                    "error": exc.detail if isinstance(exc.detail, dict) else str(exc.detail),
                },
                status_code=200,
            )

        spoken = voice_mod.reply_text(data)
        t1 = time.perf_counter()
        data["audio_b64"] = _speak_b64(spoken)
        timings["tts_ms"] = int((time.perf_counter() - t1) * 1000)
        data["transcript"] = transcript
        data["ticker"] = routed["ticker"]
        data["reply"] = spoken
        data["timings"] = timings
        data["observability"]["voice"] = {"local_only": True, "tts_voice": voice_mod.TTS_VOICE}
        return JSONResponse(data)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _speak_b64(text: str) -> str | None:
    try:
        return base64.b64encode(voice_mod.speak(text)).decode("ascii")
    except voice_mod.VoiceUnavailable:
        return None


'''
s = s[:start] + new + s[end:]

s = s.replace(
    "from fastapi import FastAPI, HTTPException, Request, UploadFile",
    "from fastapi import FastAPI, Form, HTTPException, Request, UploadFile",
)
s = s.replace(
    "import os\nfrom pathlib import Path",
    "import base64\nimport os\nimport shutil\nimport tempfile\nfrom pathlib import Path",
)
s = s.replace(
    "from . import rules\nfrom . import rubric",
    "from . import rules\nfrom . import rubric\nfrom . import voice as voice_mod",
)
s = s.replace(
    '''        "intraday_supported": intraday_supported(),''',
    '''        "intraday_supported": intraday_supported(),
        "voice": voice_mod.status(),''',
)

p.write_text(s, encoding="utf-8")
print("service rewritten:", "_build_response" in s and "/api/voice" in s)
