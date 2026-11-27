"""The synthetic crowd: deterministic, labelled, and tested for the properties that matter."""

from __future__ import annotations

import pytest

from mmdesk.scenarios import DISCLAIMER, STANCES, Facts, run


def facts(**over) -> Facts:
    base = {"ticker": "NVDA", "price": 150.0, "score_10": 8.0, "trend": 2, "momentum": 0.05,
                "volatility": 0.35, "drawdown": -0.08, "range_pos": 0.75}
    base.update(over)
    return Facts(**base)


def test_every_persona_returns_a_valid_stance():
    out = run(facts())
    assert len(out["views"]) == 6
    for view in out["views"]:
        assert view["stance"] in STANCES
        assert 0.0 <= view["conviction"] <= 1.0
        assert view["reason"]


def test_distribution_sums_to_one():
    out = run(facts())
    assert sum(out["weighted_distribution"].values()) == pytest.approx(1.0, abs=0.01)
    assert sum(out["counts"].values()) == 6


def test_cheap_and_trending_gets_add_votes():
    out = run(facts(score_10=9.0, trend=3, momentum=0.12, volatility=0.25, drawdown=-0.02))
    adds = sum(1 for v in out["views"] if v["stance"] == "add")
    assert adds >= 4
    assert out["modal_stance"] == "add"


def test_expensive_and_volatile_gets_trim_votes():
    out = run(facts(score_10=2.0, trend=-3, momentum=-0.1, volatility=0.8, drawdown=-0.3))
    trims = sum(1 for v in out["views"] if v["stance"] == "trim")
    assert trims >= 3
    assert out["modal_stance"] in ("trim", "pass")


def test_no_bands_means_value_abstains():
    out = run(facts(score_10=None))
    value = next(v for v in out["views"] if v["persona"] == "Value investor")
    assert value["stance"] == "pass"
    assert "bands" in value["reason"]


def test_dispersion_is_reported_and_bounded():
    calm = run(facts(score_10=5.0, trend=0, momentum=0.0, volatility=0.25, drawdown=-0.05))
    assert 0.0 <= calm["dispersion"] <= 1.0
    assert calm["agents"] == 6


def test_output_carries_the_label_and_the_limits():
    out = run(facts())
    assert out["label"] == DISCLAIMER
    assert len(out["what_this_is_not"]) == 4
    assert any("not a forecast" in x for x in out["what_this_is_not"])
    assert "observability" not in out, "the pure function adds no request state"


def test_no_valuation_claim_is_made_without_bands():
    """"into a cheap valuation" was asserted when no valuation bands had been supplied."""
    without = run(facts(score_10=None, trend=2, drawdown=-0.4))
    reasons = " ".join(v["reason"] for v in without["views"])
    assert "cheap valuation" not in reasons
    assert "overvalued" not in reasons
    with_bands = run(facts(score_10=7.0, trend=2, drawdown=-0.4))
    assert any("cheap valuation" in v["reason"] for v in with_bands["views"])


def test_high_volatility_alone_makes_the_risk_manager_trim():
    out = run(facts(score_10=8.0, trend=2, momentum=0.05, volatility=0.9, drawdown=-0.05))
    risk = next(v for v in out["views"] if v["persona"] == "Risk manager")
    assert risk["stance"] == "trim"
    assert "volatility" in risk["reason"]


def test_scenario_endpoint_shape(monkeypatch):
    from fastapi.testclient import TestClient

    from mmdesk import service
    from mmdesk.standard import Bars

    class G:
        bars = Bars(close=[100 + i for i in range(80)],
                    high=[101 + i for i in range(80)],
                    low=[99 + i for i in range(80)],
                    open=[100 + i for i in range(80)],
                    dates=[f"2026-01-{i%28+1:02d}" for i in range(80)],
                    source="stub")
        missing: list = []
        errors: list = []
        as_of = "2026-03-01"

    class Grounder:
        def ground(self, ticker, fundamentals=True):
            return G()

    monkeypatch.setattr(service, "Grounder", Grounder)
    r = TestClient(service.app).post("/api/scenario", json={"ticker": "TEST"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["views"]) == 6
    assert "label" in body and "not a forecast" in body["label"]
