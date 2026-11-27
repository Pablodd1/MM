"""Voice-loop tests. No models are loaded: the heavy backends are monkeypatched."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from mmdesk import service, voice


def test_route_prefers_cashtag():
    out = voice.route("should I add to $pltr after this dip?")
    assert out["ticker"] == "PLTR"


def test_route_knows_company_names():
    assert voice.route("Is Nvidia a buy here?")["ticker"] == "NVDA"
    assert voice.route("what about Microsoft")["ticker"] == "MSFT"


def test_route_falls_back_to_bare_symbol():
    assert voice.route("check AMD today")["ticker"] == "AMD"


def test_route_returns_none_without_a_name():
    assert voice.route("should I buy something today")["ticker"] is None
    assert voice.route("what do you think")["ticker"] is None


def test_reply_uses_only_card_fields():
    card = {
        "instrument": "NVDA", "verdict": "hold",
        "standard_lens": {"price": 153.43},
        "risk": {"shares": 16, "invalidation": 123.88},
        "lens": {"score_10": 6},
    }
    text = voice.reply_text(card)
    assert "NVDA" in text and "153.43" in text and "16 shares" in text and "123.88" in text
    assert "ten" in text


def test_reply_says_no_position_when_size_is_zero():
    card = {"instrument": "AAPL", "verdict": "pass",
            "standard_lens": {"price": 350.7},
            "risk": {"shares": 0, "invalidation": 323.45},
            "lens": {"score_10": 1}}
    text = voice.reply_text(card)
    assert "zero" in text and "350.70" in text


def test_voice_endpoint_rejects_empty_upload():
    c = TestClient(service.app)
    r = c.post("/api/voice", files={"audio": ("clip.webm", b"", "audio/webm")})
    assert r.status_code == 422


def test_voice_endpoint_rejects_oversized_audio():
    c = TestClient(service.app)
    big = b"x" * (16 * 1024 * 1024)
    r = c.post("/api/voice", files={"audio": ("clip.webm", big, "audio/webm")})
    assert r.status_code == 413


def test_voice_endpoint_rejects_bad_risk_pct():
    c = TestClient(service.app)
    r = c.post("/api/voice", files={"audio": ("c.webm", b"abc", "audio/webm")},
               data={"risk_pct": "0.5"})
    assert r.status_code == 422


def test_voice_endpoint_runs_the_loop(monkeypatch):
    """Patch the two model calls; the endpoint must still return a card and spoken text."""
    monkeypatch.setattr(voice, "transcribe", lambda p: "Is Nvidia a buy here?")
    monkeypatch.setattr(voice, "speak", lambda t: b"RIFFfake")

    calls = {}

    def fake_build(request, request_id, tenant=None):
        calls["ticker"] = request.ticker
        calls["question"] = request.question
        return {
            "instrument": request.ticker, "verdict": "hold",
            "standard_lens": {"price": 100.0},
            "risk": {"shares": 5, "invalidation": 90.0},
            "lens": {"score_10": 7},
            "observability": {},
        }

    monkeypatch.setattr(service, "_build_response", fake_build)
    c = TestClient(service.app)
    r = c.post("/api/voice", files={"audio": ("c.webm", b"abc", "audio/webm")})
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "NVDA"
    assert calls["ticker"] == "NVDA"
    assert "5 shares" in body["reply"]
    assert base64.b64decode(body["audio_b64"]) == b"RIFFfake"
    assert body["timings"]["stt_ms"] >= 0


def test_voice_endpoint_speaks_when_no_ticker_heard(monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda p: "what do you think")
    monkeypatch.setattr(voice, "speak", lambda t: b"RIFF")
    c = TestClient(service.app)
    r = c.post("/api/voice", files={"audio": ("c.webm", b"abc", "audio/webm")})
    assert r.status_code == 200
    assert r.json()["needs"] == "ticker"


def test_missing_model_returns_501(monkeypatch):
    def boom(path):
        raise voice.VoiceUnavailable("no model")

    monkeypatch.setattr(voice, "transcribe", boom)
    c = TestClient(service.app)
    r = c.post("/api/voice", files={"audio": ("c.webm", b"abc", "audio/webm")})
    assert r.status_code == 501
    assert r.json()["detail"]["error"] == "voice_not_available"


def test_status_reports_local_only():
    st = voice.status()
    assert st["local_only"] is True
    assert st["tts"]["engine"] == "kokoro-onnx"
