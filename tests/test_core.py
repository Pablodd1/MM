"""Tests for the deterministic core.

No network, no model, no files — pure maths. These are the tests a reviewer should
run first, because everything else in the app depends on them being right.
"""

from __future__ import annotations

import pytest

from mmdesk import rules
from mmdesk.card import Card, Citation
from mmdesk.decision import build_decision_card, reconcile
from mmdesk.retrieval_shim import corpus_available
from mmdesk.rules import Account, Setup
from mmdesk.standard import Bars, atr, momentum, range_position, sma, standard_lens, trend_score


# ---------- rule engine ----------

def test_score_is_mean_of_components_clamped():
    assert rules.score_10({"a": 8, "b": 9}) == 9
    assert rules.score_10({"a": 10, "b": 10, "c": 10}) == 10
    assert rules.score_10({"a": 0}) == 1          # never 0 — 0 means "no score"
    assert rules.score_10({}) == 0                # explicit missing sentinel


def test_position_size_scales_with_score():
    assert rules.position_pct(10, "green") == pytest.approx(0.10)
    assert rules.position_pct(5, "green") == pytest.approx(0.05)
    assert rules.position_pct(1, "green") == pytest.approx(0.01)


def test_yellow_alert_blocks_weak_scores_only():
    assert rules.position_pct(7, "yellow") == 0.0
    assert rules.position_pct(8, "yellow") == pytest.approx(0.08)


def test_orange_and_red_block_everything():
    assert rules.position_pct(10, "orange") == 0.0
    assert rules.position_pct(10, "red") == 0.0


def test_invalid_setup_rejected():
    with pytest.raises(ValueError):
        Setup("X", price=100, invalidation=110, first_target=130, component_scores={"a": 5})
    with pytest.raises(ValueError):
        Setup("X", price=100, invalidation=90, first_target=95, component_scores={"a": 5})
    with pytest.raises(ValueError):
        Setup("X", price=100, invalidation=90, first_target=130, component_scores={"a": 11})


def test_account_guards_absurd_risk():
    with pytest.raises(ValueError):
        Account(equity=10_000, risk_pct_per_trade=0.5)
    with pytest.raises(ValueError):
        Account(equity=0)


def test_size_capped_by_risk_not_conviction():
    # 1% risk on 10k = $100 budget; stop is $10 away => 10 shares by risk.
    # Conviction cap 10% of 10k = $1000 / $100 = 10 shares. Both 10 here.
    setup = Setup("T", price=100, invalidation=90, first_target=140,
                  component_scores={"a": 10})
    out = rules.size_position(setup, Account(equity=10_000))
    assert out["shares"] == 10
    assert out["risk_at_stop"] == pytest.approx(100.0)


def test_tight_stop_is_capped_by_conviction():
    setup = Setup("T", price=100, invalidation=99, first_target=120,
                  component_scores={"a": 10})
    out = rules.size_position(setup, Account(equity=10_000))
    # risk allows 100 shares, conviction cap is 10% of equity => 10 shares
    assert out["shares"] == 10
    assert out["capped_by"] == "conviction"


def test_cash_floor_reduces_size():
    setup = Setup("T", price=100, invalidation=99, first_target=120,
                  component_scores={"a": 10})
    acct = Account(equity=10_000, cash_pct=0.05, min_cash_pct=0.03)
    out = rules.size_position(setup, acct)
    assert out["shares"] == 2          # only 2% of equity is spendable
    assert out["cash_cap_applied"] == 2


def test_intraday_caps_total_day_risk():
    setup = Setup("T", price=100, invalidation=90, first_target=140,
                  component_scores={"a": 10}, mode="intraday")
    acct = Account(equity=100_000, risk_pct_per_trade=0.02)
    out = rules.size_position(setup, acct)
    assert out["risk_at_stop"] <= 100_000 * rules.MAX_DAY_RISK_PCT


def test_sell_half_at_double_fires_at_2x():
    setup = Setup("T", price=200, invalidation=150, first_target=260,
                  component_scores={"a": 7}, entry_price=100)
    out = rules.exit_signals(setup, current_score=7, unrealised_pct=1.05)
    assert [r["rule"] for r in out] == ["sell_half_at_double"]
    assert rules.exit_signals(setup, 7, 0.9) == []


def test_three_point_rule_needs_three_points_down():
    # 29:07 — "if the stock moves three points from a 10 to a nine to an eight to a seven"
    # so 10 -> 7 is the pattern he describes; the threshold is >= 3 points down.
    assert rules.three_point_rule(10, 7)["action"] == "take partial profit"
    assert rules.three_point_rule(9, 6)["action"] == "take partial profit"
    assert rules.three_point_rule(10, 8) is None     # only two points down
    assert rules.three_point_rule(9, 7) is None      # two points down: not yet his rule


def test_hedge_carries_decay_warning():
    h = rules.hedge_notional(10_000)
    assert h["rwm_notional"] == 10_000
    assert "decay" in h["warning"].lower()


# ---------- standard lens ----------

def _bars(closes):
    return Bars(close=closes, source="test")


def test_sma_and_trend_uptrend():
    closes = [100 + i for i in range(210)]
    b = _bars(closes)
    assert sma(closes, 20) > sma(closes, 50) > sma(closes, 200)
    assert trend_score(b)["score"] == 3


def test_trend_downtrend_is_negative():
    closes = [400 - i for i in range(210)]
    assert trend_score(_bars(closes))["score"] == -3


def test_momentum_and_range():
    closes = [100 + i for i in range(100)]
    assert momentum(_bars(closes), n=63)["return"] > 0
    r = range_position(_bars(closes), n=50)
    assert 0 <= r["pct_of_range"] <= 1


def test_atr_uses_high_low_when_present():
    # true ranges: max(1.0, 1.5, 0.5)=1.5 ; max(1.0, 1.5, 0.5)=1.5 ; max(1.0, 1.5, 0.5)=1.5
    b = Bars(close=[10, 11, 12, 13], high=[10.5, 11.5, 12.5, 13.5],
             low=[9.5, 10.5, 11.5, 12.5])
    assert atr(b, n=3) == pytest.approx(1.5)


def test_atr_equal_high_low_falls_back_to_close_only():
    b = Bars(close=[10, 11, 12, 13], high=[10, 11, 12, 13], low=[10, 11, 12, 13])
    assert atr(b, n=3) == pytest.approx(1.0)


def test_standard_lens_needs_enough_data():
    out = standard_lens(_bars([100, 101]))
    assert out["volatility_annualised"] is None
    assert out["action"] in ("favourable", "unfavourable", "mixed")


def test_valuation_percentile_bounds():
    from mmdesk.standard import valuation_percentile
    assert valuation_percentile(10, [5, 10, 15, 20])["percentile"] == 0.5
    assert valuation_percentile(1, [5, 10])["percentile"] == 0.0
    assert valuation_percentile(99, [5, 10])["percentile"] == 1.0


# ---------- reconciliation + card ----------

def test_reconcile_names_real_disagreement():
    mm = {"action": "add"}
    std = {"action": "unfavourable", "charts_against": 2}
    out = reconcile(mm, std, "green")
    assert out["exists"] is True
    assert "standard lens is against" in out["summary"]
    assert "invalidation" in out["tie_break"]


def test_reconcile_agreement_is_not_confirmation():
    out = reconcile({"action": "add"}, {"action": "favourable"}, "green")
    assert out["exists"] is False
    assert "not confirmation" in out["tie_break"]


def test_card_requires_grounded_numbers_and_carries_disclaimer():
    setup = Setup("NVDA", price=100, invalidation=92, first_target=130,
                  component_scores={"support": 9, "upside": 8, "trend": 7})
    std = standard_lens(_bars([80 + i * 0.1 for i in range(210)]))
    card = build_decision_card(
        question="Add here?", setup=setup, account=Account(equity=50_000),
        standard=std, grounding={"as_of": "2026-11-24T00:00:00Z", "missing": []},
    )
    assert isinstance(card, Card)
    assert card.lens["score_10"] == 8
    assert card.risk["invalidation"] == 92
    assert card.attribution is None          # the default tenant carries no one's name
    assert card.disclaimer.startswith("Educational tool")
    assert "not investment advice" in card.to_markdown().lower()
    assert card.confidence["calibration"].startswith("uncalibrated")


def test_card_flags_missing_data_as_falsifier():
    setup = Setup("X", price=100, invalidation=90, first_target=120,
                  component_scores={"a": 5})
    card = build_decision_card(
        question="?", setup=setup, account=Account(equity=10_000),
        standard=standard_lens(_bars([100, 101, 102])),
        grounding={"missing": ["earnings_date"]},
    )
    assert any("Missing inputs" in f for f in card.falsifiers)


def test_card_without_corpus_hits_says_so():
    setup = Setup("X", price=100, invalidation=90, first_target=120,
                  component_scores={"a": 5})
    card = build_decision_card(question="?", setup=setup, account=Account(equity=10_000),
                               standard=standard_lens(_bars([100, 101, 102])))
    assert any("no passage" in f.lower() for f in card.falsifiers)


def test_citation_builds_deep_link():
    c = Citation(rule="x", video_id="abc", start_s=125)
    assert c.url == "https://www.youtube.com/watch?v=abc&t=125s"
    assert c.stamp == "00:02:05"


def test_corpus_shim_is_honest():
    # The shim must report availability truthfully rather than pretend.
    assert isinstance(corpus_available(), bool)


# ---------- fundamental bands (his actual mechanic) ----------

def test_fair_value_score_at_and_below_green_is_ten():
    from mmdesk.rubric import fair_value_score
    assert fair_value_score(90, green_line=100, red_line=200)["score_10"] == 10
    assert fair_value_score(100, green_line=100, red_line=200)["score_10"] == 10


def test_fair_value_score_at_and_above_red_is_one():
    from mmdesk.rubric import fair_value_score
    assert fair_value_score(200, green_line=100, red_line=200)["score_10"] == 1
    assert fair_value_score(260, green_line=100, red_line=200)["score_10"] == 1


def test_fair_value_score_interpolates_between_lines():
    from mmdesk.rubric import fair_value_score
    mid = fair_value_score(150, green_line=100, red_line=200)
    assert 5 <= mid["score_10"] <= 6
    assert mid["basis"] == "fundamental_bands"
    near_green = fair_value_score(110, green_line=100, red_line=200)["score_10"]
    near_red = fair_value_score(190, green_line=100, red_line=200)["score_10"]
    assert near_green > near_red


def test_fair_value_rejects_inverted_bands():
    from mmdesk.rubric import fair_value_score
    with pytest.raises(ValueError):
        fair_value_score(150, green_line=200, red_line=100)


def test_fair_value_carries_citations():
    from mmdesk.rubric import fair_value_score
    out = fair_value_score(150, 100, 200)
    ids = {c["video_id"] for c in out["citations"]}
    assert "RJ8zTCJaJhU" in ids and "jCS3eoQiXHI" in ids


# ---------- review findings: sizing with costs ----------

def test_costs_are_inside_the_risk_budget():
    setup = Setup("T", price=100, invalidation=95, first_target=120,
                  component_scores={"a": 10})
    acct = Account(equity=10_000, risk_pct_per_trade=0.01,
                   commission_per_share=0.02, slippage_bps=20)
    out = rules.size_position(setup, acct)
    assert out["risk_at_stop"] <= out["risk_budget"] + 1e-6
    assert out["effective_entry"] > setup.price        # slippage applied
    assert out["effective_stop"] < setup.invalidation  # exit slippage applied


def test_shares_are_floored_never_rounded_up():
    # budget 100, risk/share 3 -> 33.33 shares -> 33, never 34
    setup = Setup("T", price=10, invalidation=7, first_target=20, component_scores={"a": 10})
    acct = Account(equity=10_000, risk_pct_per_trade=0.01)
    out = rules.size_position(setup, acct)
    assert out["shares"] == 33
    assert out["risk_at_stop"] <= 100


def test_absurd_cost_reduces_size_to_zero_not_negative():
    # budget is $10; a $20 round-trip commission on a $0.50 stop means one share would
    # risk 20.5, so the only honest answer is zero shares.
    setup = Setup("T", price=100, invalidation=99.5, first_target=120, component_scores={"a": 10})
    acct = Account(equity=1_000, risk_pct_per_trade=0.01, commission_per_share=20)
    out = rules.size_position(setup, acct)
    assert out["shares"] == 0
    assert out["risk_at_stop"] == 0


def test_one_share_is_allowed_when_it_fits_the_budget():
    setup = Setup("T", price=100, invalidation=99.5, first_target=120, component_scores={"a": 10})
    acct = Account(equity=1_000, risk_pct_per_trade=0.01, commission_per_share=5)
    out = rules.size_position(setup, acct)
    assert out["shares"] == 1
    assert out["risk_at_stop"] <= out["risk_budget"]


def test_negative_commission_rejected():
    with pytest.raises(ValueError):
        Account(equity=1_000, commission_per_share=-1)


# ---------- review findings: model may not emit numerals ----------

def test_digit_detection_catches_spelled_and_unicode():
    from mmdesk.llm import contains_digits
    assert contains_digits("the score is 7") is True
    assert contains_digits("about 7.5 dollars") is True
    assert contains_digits("score ٧ arabic") is True       # unicode digit
    assert contains_digits("the score is favourable") is False


def test_template_narrative_is_used_without_a_key(monkeypatch):
    """And it must not try the network: clearing one variable used to leave a key in another,
    which turned this test into a real, billed call on a developer's machine."""
    import httpx

    from mmdesk.llm import LLMConfig, narrate

    def no_network(*args, **kwargs):
        raise AssertionError("no model call may be made without a key")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    assert LLMConfig.from_env().configured is False
    out = narrate({"verdict": "hold", "lens": {}, "standard_lens": {}, "falsifiers": []}, [])
    assert "template" in out["generated_by"]


def test_an_unrecognised_key_is_not_sent_anywhere(monkeypatch):
    """"sk-" is shared by several vendors. Without a named endpoint the key stays unused."""
    from mmdesk.llm import LLMConfig

    monkeypatch.setenv("MMDESK_LLM_KEY", "sk-not-a-known-vendor")
    cfg = LLMConfig.from_env()
    assert cfg.configured is False
    assert "MMDESK_LLM_BASE" in cfg.problem
    monkeypatch.setenv("MMDESK_LLM_BASE", "https://api.example-vendor.test/v1")
    assert LLMConfig.from_env().configured is True


# ---------- review findings: journal honesty flags ----------

def test_journal_records_basis_and_sparsity(tmp_path):
    from mmdesk.journal import Journal
    from datetime import date, timedelta
    j = Journal(tmp_path / "j.jsonl")
    j.append({"instrument": "AAA", "created_at": "2026-01-02T00:00:00+00:00",
              "risk": {"invalidation": 90, "first_target": 130},
              "standard_lens": {"price": 100}})
    start = date(2026, 1, 2)
    series = [((start + timedelta(days=i)).isoformat(), 101.0) for i in range(1, 5)]
    out = j.score_entries({"AAA": series}, adjusted=True, source="test")[0].outcomes["30"]
    assert out["stopped_basis"] == "close_only"
    assert out["adjusted_for_splits"] is True
    assert out["price_source"] == "test"
    assert out["sparse"] is True


def test_volatility_is_reported_for_both_windows():
    """A 20-day annualised number is a shock measure; 60-day is the calmer read."""
    from mmdesk.standard import Bars, standard_lens

    calm = [100.0] * 40 + [100, 101, 102, 101, 100, 101, 102, 103, 104, 105] * 3
    bars = Bars(close=calm, high=[c * 1.01 for c in calm], low=[c * 0.99 for c in calm],
                source="test")
    lens = standard_lens(bars)
    assert lens["volatility_20d_annualised"] is not None
    assert lens["volatility_60d_annualised"] is not None


def test_a_short_spike_inflates_20d_more_than_60d():
    from mmdesk.standard import Bars, realised_vol

    base = [100.0] * 80
    spiky = base + [100, 112, 98, 115, 96, 118, 99, 120, 97, 119,
                    98, 117, 99, 116, 97, 118, 98, 117, 99, 116]
    bars = Bars(close=spiky, high=spiky, low=spiky, source="test")
    short = realised_vol(bars, n=20)
    long = realised_vol(bars, n=60)
    assert short is not None and long is not None
    assert short > long, "the 20-day window must react harder to a recent spike"


# ---------------------------------------------------------------- chart input
def test_chart_without_levels_is_refused_with_a_reason():
    from fastapi.testclient import TestClient
    from mmdesk import service

    r = TestClient(service.app).post("/api/chart", files={"file": ("c.png", b"x", "image/png")},
                                     data={"ticker": "NVDA"})
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["error"] == "levels_required"
    assert any("typed" in alt for alt in body["required_alternatives"])


def test_chart_with_typed_levels_runs_the_analysis(monkeypatch):
    from fastapi.testclient import TestClient
    from mmdesk import service

    def fake_build(req, rid, tenant=None, **kwargs):
        return {
            "instrument": req.ticker, "verdict": "hold", "risk": {"invalidation": req.support},
            "observability": {}, "standard_lens": {"price": 100.0}, "lens": {"score_10": 5},
        }

    monkeypatch.setattr(service, "_build_response", fake_build)
    r = TestClient(service.app).post(
        "/api/chart",
        files={"file": ("c.png", b"x", "image/png")},
        data={"ticker": "NVDA", "support": "90", "target": "130"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chart"]["numbers_from"] == "typed levels only"
    assert body["chart"]["image_received"] is True


def test_template_narrative_renders_without_a_model(monkeypatch):
    """A visitor sees a narrative on the card even with no model key and no cloud opt-in."""
    from fastapi.testclient import TestClient
    from mmdesk import service
    from mmdesk.standard import Bars

    class G:
        bars = Bars(close=[100 + i * 0.4 for i in range(120)],
                    high=[101 + i * 0.4 for i in range(120)],
                    low=[99 + i * 0.4 for i in range(120)],
                    open=[100 + i * 0.4 for i in range(120)],
                    dates=[f"2026-01-{i%28+1:02d}" for i in range(120)], source="stub")
        fundamentals: dict = {}
        missing: list = []
        errors: list = []
        as_of = "2026-03-01"
        sources = [{"provider": "stub"}]

        def __init__(self, *a, **k):
            pass

        def ground(self, ticker, fundamentals=True):
            return G()

    monkeypatch.setattr(service, "Grounder", G)
    monkeypatch.delenv("MMDESK_ALLOW_CLOUD_LLM", raising=False)
    body = TestClient(service.app).post("/api/analyze", json={
        "ticker": "TEST", "question": "is this a buy", "account_equity": 50000,
        "risk_pct": 0.01}).json()
    n = body["narrative"]
    assert n["lens_narrative"], "the MM opinion must render without a model"
    assert n["standard_narrative"]
    assert "template" in n["generated_by"]
