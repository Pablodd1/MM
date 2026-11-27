"""The HTTP surface: what gets in, what gets out, and who is allowed to do what.

Every request here runs offline — bars come from the stub in `conftest.py`.
"""

from __future__ import annotations

import json
import sys

import httpx
import pandas as pd
import pytest
from conftest import StubGrounder
from fastapi.testclient import TestClient
from starlette.requests import Request

from mmdesk import data, opinion, service, tenants, voice
from mmdesk.data import Grounder, Grounding, YFinanceProvider
from mmdesk.llm import LLMConfig
from mmdesk.standard import Bars

REMOTE = {"client": ("203.0.113.9", 50000)}
XAI_KEY = "xai-" + "0" * 32


class NoHostedCall:
    """Stands in for `httpx.post`; a hosted call without opt-in is a test failure."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        raise AssertionError(f"unexpected model call to {url}")


class JsonReply:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


@pytest.fixture
def client(stub_market):
    return TestClient(service.app, raise_server_exceptions=False)


# ---------------------------------------------------------------- what a card carries

def test_analyze_returns_a_card_the_page_can_render(client):
    body = client.post("/api/analyze", json={"question": "Is it a buy?", "ticker": "msft",
                                             "account_equity": 50000, "risk_pct": 0.01}).json()
    assert body["instrument"] == "MSFT"
    for key in ("lens", "standard_lens", "disagreement", "risk", "grounding", "falsifiers",
                "narrative", "verdict", "verdict_reason", "journal_id", "observability",
                "markdown", "disclaimer"):
        assert key in body, key
    assert body["lens"]["score_10"] == body["lens"]["score_meta"]["score_10"]
    assert body["lens"]["label"] == "House lens"
    assert isinstance(body["lens"]["shares"], int) and body["lens"]["shares"] > 0
    assert body["lens"]["cited_rules"]
    assert body["narrative"]["lens_narrative"]
    assert "template" in body["narrative"]["generated_by"]
    assert body["grounding"]["price_bar"] == "2025-11-03"
    assert body["observability"]["rubric_version"] == "technical_proxy_v1"
    assert body["attribution"] is None


def test_bands_supplied_by_the_user_drive_the_score(client):
    body = client.post("/api/analyze", json={"ticker": "MSFT", "green_line": 200,
                                             "red_line": 300}).json()
    assert body["lens"]["score_10"] == 7          # price 229.5 sits 29.5% of the way up the band
    assert body["lens"]["score_meta"]["method"] == "fundamental_bands"


def test_the_journal_records_the_price_bar(client):
    before = {e["id"] for e in client.get("/api/journal").json()["entries"]}
    body = client.post("/api/analyze", json={"ticker": "MSFT"}).json()
    listed = client.get("/api/journal").json()["entries"]
    new = [e for e in listed if e["id"] not in before]
    assert [e["id"] for e in new] == [body["journal_id"]]
    assert new[0]["instrument"] == "MSFT"


# ---------------------------------------------------------------- input is validated at the edge

@pytest.mark.parametrize("payload", [
    {"ticker": "MSFT", "components": {"x": 50}},            # used to be a 500
    {"ticker": "MSFT", "components": {"x": -1}},
    {"ticker": "MSFT", "components": {"Bad Name": 5}},
    {"ticker": "MSFT", "account_equity": -1},
    {"ticker": "MSFT", "risk_pct": 0.5},
    {"ticker": "MSFT", "cash_pct": 1.5},
    {"ticker": "MSFT", "green_line": 100, "red_line": 100},
    {"ticker": "MSFT", "time_stop_days": 0},
    {"ticker": "../etc/passwd"},
    {"ticker": "MSFT", "slippage_bps": 500},
    {"ticker": "MSFT", "existing_open_risk": -5},
])
def test_invalid_input_is_a_422_never_a_500(client, payload):
    r = client.post("/api/analyze", json=payload)
    assert r.status_code == 422, (payload, r.text[:200])


def test_non_finite_numbers_are_refused(client):
    for field in ("account_equity", "risk_pct", "support", "target", "cash_pct"):
        body = f'{{"ticker": "MSFT", "{field}": 1e999}}'      # valid JSON, parses to infinity
        r = client.post("/api/analyze", content=body,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, field


def test_scenario_rejects_inverted_bands(stub_market):
    r = TestClient(service.app).post("/api/scenario", json={"ticker": "T", "green_line": 200,
                                                            "red_line": 100})
    assert r.status_code == 422


def test_scenario_says_so_when_there_are_too_few_bars(monkeypatch):
    class Short:
        def ground(self, ticker, fundamentals=True):
            g = Grounding(ticker=ticker, as_of="2026-03-01T00:00:00+00:00")
            g.bars = Bars(close=[100.0, 101.0], source="stub")
            return g

    monkeypatch.setattr(service, "Grounder", Short)
    r = TestClient(service.app).post("/api/scenario", json={"ticker": "T"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "not_enough_bars"


def test_scenario_reports_a_true_range_low(monkeypatch):
    """`pct_of_range or 0.5` turned a true 0.0 into the middle of the range."""
    class Falling:
        def ground(self, ticker, fundamentals=True):
            g = Grounding(ticker=ticker, as_of="2026-03-01T00:00:00+00:00")
            closes = [200 - i for i in range(80)]
            g.bars = Bars(close=closes, high=[c + 1 for c in closes], low=[c - 1 for c in closes],
                          source="stub")
            return g

    monkeypatch.setattr(service, "Grounder", Falling)
    body = TestClient(service.app).post("/api/scenario", json={"ticker": "T"}).json()
    assert body["facts"]["range_position"] == 0.0


def test_chart_validates_typed_levels_instead_of_crashing(client):
    r = client.post("/api/chart", files={"file": ("c.png", b"x", "image/png")},
                    data={"ticker": "MSFT", "support": "-1", "target": "130"})
    assert r.status_code == 422
    r = client.post("/api/chart", files={"file": ("c.png", b"x", "image/png")},
                    data={"ticker": "MSFT", "green_line": "100", "red_line": "90"})
    assert r.status_code == 422
    r = client.post("/api/chart", files={"file": ("c.png", b"x", "image/png")},
                    data={"ticker": "MSFT", "support": "90", "target": "80"})
    assert r.status_code == 422
    assert "target must sit above" in r.json()["detail"]


def test_chart_with_valid_levels_journals_once(client):
    before = client.get("/api/journal").json()["count"]
    r = client.post("/api/chart", files={"file": ("c.png", b"x", "image/png")},
                    data={"ticker": "MSFT", "support": "200", "target": "300"})
    assert r.status_code == 200
    body = r.json()
    assert body["chart"]["numbers_from"] == "typed levels only"
    assert body["risk"]["invalidation"] == 200.0
    assert client.get("/api/journal").json()["count"] == before + 1


def test_an_oversized_upload_is_refused_by_its_declared_size(client):
    big = b"x" * (5 * 1024 * 1024)
    r = client.post("/api/chart", files={"file": ("c.png", big, "image/png")},
                    data={"ticker": "MSFT", "support": "90", "target": "130"})
    assert r.status_code == 413


# ---------------------------------------------------------------- the second opinion

def test_opinion_endpoint_returns_a_labelled_template_without_a_key(client, monkeypatch):
    monkeypatch.setattr(httpx, "post", NoHostedCall())
    before = client.get("/api/journal").json()["count"]
    body = client.post("/api/opinion", json={"ticker": "MSFT", "question": "is it a buy"}).json()
    assert body["available"] is True
    assert body["stance"] in {"add", "hold", "trail", "pass"}
    for key in ("analysis", "lens_opinion", "agreement", "falsifier"):
        assert body[key], key
    assert "template" in body["generated_by"]
    assert body["tenant"] == "default" and body["lens_label"] == "House lens"
    # A second opinion on one question must not count as a second call in the track record.
    assert client.get("/api/journal").json()["count"] == before


def test_nothing_is_sent_to_a_hosted_model_without_the_opt_in(client, monkeypatch):
    monkeypatch.setenv("MMDESK_LLM_KEY", XAI_KEY)
    monkeypatch.setattr(httpx, "post", NoHostedCall())
    analysis = client.post("/api/analyze", json={"ticker": "MSFT"}).json()
    assert "cloud model disabled" in analysis["narrative"]["generated_by"]
    opinion_ = client.post("/api/opinion", json={"ticker": "MSFT"}).json()
    assert "cloud model disabled" in opinion_["generated_by"]


# ---------------------------------------------------------------- a failing model

def test_voice_model_crash_is_not_a_500(monkeypatch):
    """A runtime error inside the speech model is the server's problem, not the caller's."""
    def boom(path):
        raise RuntimeError("cuda device-side assert")

    monkeypatch.setattr(voice, "transcribe", boom)
    r = TestClient(service.app).post("/api/voice",
                                     files={"audio": ("c.webm", b"abc", "audio/webm")})
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "voice_failed"
    assert "cuda" not in r.text


# ---------------------------------------------------------------- headers, ids, limits

def test_every_response_carries_the_baseline_headers(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert len(r.headers["x-request-id"]) >= 8


def test_the_request_id_echoes_into_the_response_and_the_card(client):
    r = client.post("/api/analyze", json={"ticker": "MSFT"}, headers={"X-Request-ID": "trace-1234"})
    assert r.headers["x-request-id"] == "trace-1234"
    assert r.json()["observability"]["request_id"] == "trace-1234"


def test_an_unsafe_request_id_is_replaced(client):
    """It ends up in the log line, so it has to be a plain token."""
    forged = 'forged 1.2.3.4 - - [x] "quote'
    r = client.get("/health", headers={"X-Request-ID": forged})
    assert r.headers["x-request-id"] != forged
    assert r.headers["x-request-id"].isalnum()


def test_rate_limit_is_per_caller_and_recovers(client, monkeypatch):
    monkeypatch.setattr(service, "RATE_LIMIT", 3)
    for _ in range(3):
        assert client.post("/api/lens", json={"question": "risk reward"}).status_code == 200
    limited = client.post("/api/lens", json={"question": "risk reward"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"

    other = TestClient(service.app, **REMOTE)             # another caller is not affected
    assert other.post("/api/lens", json={"question": "cash"}).status_code == 200

    later = service.time.time() + 61
    monkeypatch.setattr(service.time, "time", lambda: later)
    assert client.post("/api/lens", json={"question": "risk reward"}).status_code == 200
    assert other.post("/api/lens", json={"question": "cash"}).status_code == 200


def _request(client_addr, headers=()):
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
                    "client": client_addr})


def test_rate_limit_keys_on_the_platform_header_only_on_vercel(monkeypatch):
    headers = (("X-Forwarded-For", "1.1.1.1"), ("X-Vercel-Forwarded-For", "9.9.9.9"))
    request = _request(("203.0.113.9", 50000), headers)
    assert service._client_key(request) == "203.0.113.9", "spoofable headers are ignored locally"
    monkeypatch.setenv("VERCEL", "1")
    assert service._client_key(request) == "9.9.9.9"


def test_idle_rate_buckets_do_not_accumulate(monkeypatch):
    monkeypatch.setattr(service, "RATE_LIMIT", 1)
    monkeypatch.setattr(service, "_RATE_MAX_KEYS", 5)
    for n in range(40):
        service._rate_ok(f"caller-{n}")
    assert len(service._hits) <= 5
    assert "caller-39" in service._hits, "the caller just seen is never the one evicted"


# ---------------------------------------------------------------- the market data layer

class CountingProvider:
    name = "counting"
    calls = 0

    def daily_bars(self, ticker):
        type(self).calls += 1
        g = StubGrounder().ground(ticker)
        return g.bars, {"field": "daily_bars", "provider": "counting", "last_bar": g.last_bar}

    def fundamentals(self, ticker):
        return {}, {"field": "fundamentals", "provider": "counting"}


def test_grounding_is_cached_across_requests_for_the_default_provider(monkeypatch):
    """A new Grounder per request meant a new empty cache per request, so the same bars were
    downloaded three times for one question."""
    counter = CountingProvider()
    CountingProvider.calls = 0
    monkeypatch.setattr(YFinanceProvider, "daily_bars", counter.daily_bars)
    monkeypatch.setattr(YFinanceProvider, "fundamentals", counter.fundamentals)
    data._SHARED_CACHE.clear()
    first = Grounder().ground("CACHED")
    second = Grounder().ground("CACHED")
    third = Grounder().ground("cached")            # tickers are case-insensitive
    assert CountingProvider.calls == 1
    assert first is second is third
    data._SHARED_CACHE.clear()
    Grounder().ground("CACHED")
    assert CountingProvider.calls == 2


def test_an_injected_provider_keeps_a_private_cache():
    CountingProvider.calls = 0
    Grounder(provider=CountingProvider()).ground("PRIV")
    Grounder(provider=CountingProvider()).ground("PRIV")
    assert CountingProvider.calls == 2
    assert not data._SHARED_CACHE


def test_provider_failure_is_recorded_not_estimated():
    class Down:
        name = "down"

        def daily_bars(self, ticker):
            raise RuntimeError("connection reset")

        def fundamentals(self, ticker):
            raise RuntimeError("connection reset")

    g = Grounder(provider=Down()).ground("OUTAGE")
    assert g.bars is None and g.missing == ["price", "fundamentals"]
    assert "connection reset" in g.errors[0]
    assert g.to_dict()["price"] is None


def test_missing_bars_is_a_422_that_names_what_was_missing(monkeypatch):
    class Down:
        def ground(self, ticker, fundamentals=True):
            g = Grounding(ticker=ticker, as_of="now")
            g.missing, g.errors = ["price"], ["stub: no bars"]
            return g

    monkeypatch.setattr(service, "Grounder", Down)
    r = TestClient(service.app).post("/api/analyze", json={"ticker": "MSFT"})
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["error"] == "no market data" and body["missing"] == ["price"]
    assert "does not estimate" in body["note"]


def _fake_yfinance(monkeypatch, frame: pd.DataFrame) -> None:
    class FakeYFinance:
        class Ticker:
            def __init__(self, ticker):
                pass

            def history(self, **kwargs):
                return frame

    monkeypatch.setitem(sys.modules, "yfinance", FakeYFinance)


def test_nan_bars_from_the_provider_are_dropped(monkeypatch):
    """A NaN in a close used to reach the arithmetic and the JSON encoder."""
    frame = pd.DataFrame({
        "Open": [1.0, float("nan"), 3.0, 4.0], "High": [1.1, 2.1, float("nan"), 4.1],
        "Low": [0.9, 1.9, 2.9, 4.0], "Close": [1.0, 2.0, 3.0, float("nan")],
        "Volume": [1, 1, 1, 1],
    }, index=pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]))
    _fake_yfinance(monkeypatch, frame)
    bars, src = YFinanceProvider().daily_bars("X")
    assert bars.close == [1.0, 2.0, 3.0]
    assert src["last_bar"] == "2026-01-06"


def test_a_history_that_is_all_gaps_is_an_error(monkeypatch):
    frame = pd.DataFrame({"Open": [float("nan")], "High": [float("nan")], "Low": [float("nan")],
                          "Close": [float("nan")], "Volume": [0]},
                         index=pd.to_datetime(["2026-01-02"]))
    _fake_yfinance(monkeypatch, frame)
    with pytest.raises(RuntimeError):
        YFinanceProvider().daily_bars("X")


# ---------------------------------------------------------------- the model layers

def test_model_discovery_actually_runs(monkeypatch):
    """`httpx` was imported only inside `narrate`, so discovery raised NameError, swallowed it,
    and returned the stale model name."""
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return JsonReply({"data": [{"id": "grok-9"}, {"id": "grok-4.7"}, {"id": "other"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    cfg = LLMConfig(api_key="xai-1", base_url="https://api.x.ai/v1", model="retired-name")
    assert cfg.discover_model() == "grok-9"
    assert seen == ["https://api.x.ai/v1/models"]


ADAPTER_PROSE = JsonReply({"choices": [{"message": {"content": json.dumps({
    "verdict_reason": "the odds are fair", "lens_narrative": "a cited rule drives it",
    "standard_narrative": "the tape agrees", "disagreement": "little",
    "caveat": "a close below the stop"})}}]})


def test_a_tenant_with_its_own_adapter_keeps_its_material_off_the_hosted_provider(
        client, monkeypatch):
    tenants.create("own", "Own lens")
    tenants.set_adapter("own", {"path": "models/own", "model": "own-lens",
                                "base_url": "http://127.0.0.1:8899/v1",
                                "eval": {"stance_agreement": 0.95, "leakage_free": True}})
    tenants.activate_adapter("own")

    urls: list[str] = []

    def fake_post(url, **kwargs):
        urls.append(url)
        return ADAPTER_PROSE

    monkeypatch.setenv("MMDESK_ALLOW_CLOUD_LLM", "1")
    monkeypatch.setenv("MMDESK_LLM_KEY", XAI_KEY)
    monkeypatch.setattr(httpx, "post", fake_post)
    body = client.post("/api/analyze", headers={"X-Tenant": "own"},
                       json={"ticker": "MSFT", "question": "risk reward"}).json()
    assert urls and all(u.startswith("http://127.0.0.1:8899/v1/") for u in urls)
    assert "adapter own" in body["narrative"]["generated_by"]
    assert body["narrative"]["verdict_reason"] == "the odds are fair"
    assert opinion.provider(tenants.get("own"))["key"] == "local"
