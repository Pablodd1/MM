"""Position sizing, scoring and reconciliation: the numbers a reader acts on.

The sizing invariant itself is property-tested in `test_sizing_review.py`. These tests are for
the places where the card said something that was not true of the numbers beside it.
"""

from __future__ import annotations

import pytest

from mmdesk import decision, rubric, rules
from mmdesk.decision import build_decision_card, reconcile
from mmdesk.rules import Account, Setup
from mmdesk.standard import Bars, standard_lens


def _bars(closes, high=None, low=None):
    return Bars(close=closes, high=high or [c + 1 for c in closes],
                low=low or [c - 1 for c in closes], source="test")


def _setup(price=100.0, stop=99.0, target=120.0, score=10, alert="green"):
    return Setup("T", price=price, invalidation=stop, first_target=target,
                 component_scores={"a": score}, alert=alert)


def _rising(n=210):
    return _bars([80 + i * 0.5 for i in range(n)])


# ---------------------------------------------------------------- the cash cap

@pytest.mark.parametrize("cash", [0.0, 0.02, 0.05])
def test_cash_cap_applies_without_a_minimum(cash):
    """The cap used to need a non-zero floor as well, so a user with two percent cash was sized
    to buy ten percent."""
    out = rules.size_position(_setup(), Account(equity=10_000, cash_pct=cash))
    assert out["notional"] <= 10_000 * cash + 1e-6
    assert out["capped_by"] == "cash"


def test_cash_cap_allows_more_than_the_floor_when_cash_allows():
    out = rules.size_position(_setup(), Account(equity=10_000, cash_pct=0.5, min_cash_pct=0.3))
    assert out["shares"] == 10 and out["capped_by"] == "conviction"


def test_cash_cap_leaves_room_for_the_entry_fee():
    out = rules.size_position(_setup(), Account(equity=1_000, cash_pct=0.1,
                                                fixed_fee_per_order=50))
    assert out["notional"] + 50 <= 100 + 1e-6


def test_unknown_cash_is_not_a_cap():
    out = rules.size_position(_setup(), Account(equity=10_000))
    assert out["shares"] == 10 and out["capped_by"] == "conviction"


# ---------------------------------------------------------------- fractional shares

def test_fractional_account_keeps_fractions_above_one_share():
    """The flag used to apply only below one share, so 2.4 shares came back as 2."""
    out = rules.size_position(
        Setup("T", price=100, invalidation=95, first_target=120, component_scores={"a": 10}),
        Account(equity=2_400, risk_pct_per_trade=0.005, allow_fractional_shares=True))
    assert out["shares"] == pytest.approx(2.4)          # 12 of risk buys 2.4 shares at 5 each
    assert out["risk_at_stop"] <= 12 + 1e-9


# ---------------------------------------------------------------- zero shares: the true reason

def test_zero_shares_names_the_cap_that_bound():
    # one share risks 1 against a budget of 10, but the score permits only 100 and a share is 150
    out = rules.size_position(_setup(price=150.0, stop=149.0, target=200.0),
                              Account(equity=1_000))
    assert out["shares"] == 0
    assert out["capped_by"] == "conviction"
    assert "conviction" in out["reason"] and "stop too wide" not in out["reason"]

    out = rules.size_position(_setup(), Account(equity=10_000, cash_pct=0.002))
    assert out["capped_by"] == "cash" and "cash" in out["reason"]


def test_verdict_reason_is_the_sizers_reason():
    """Zero shares from the budget used to be explained as "alert green with score 10: no new
    exposure permitted"."""
    card = build_decision_card(
        question="?", setup=Setup("T", price=100, invalidation=80, first_target=140,
                                  component_scores={"a": 10}),
        account=Account(equity=1_000), standard=standard_lens(_rising()))
    assert card.verdict == "pass"
    assert "stop too wide" in card.verdict_reason
    assert "alert green" not in card.verdict_reason


def test_alert_block_is_still_named_as_such():
    card = build_decision_card(
        question="?", setup=Setup("T", price=100, invalidation=95, first_target=130,
                                  component_scores={"a": 10}, alert="orange"),
        account=Account(equity=10_000), standard=standard_lens(_rising()))
    assert card.verdict == "pass"
    assert "orange" in card.verdict_reason


# ---------------------------------------------------------------- reward/risk after costs

def test_card_shows_reward_risk_after_costs():
    """With heavy slippage the card showed a gross two-to-one and called it "add"."""
    setup = Setup("T", price=100, invalidation=95, first_target=110, component_scores={"a": 10})
    account = Account(equity=10_000, slippage_bps=200, commission_per_share=0.5)
    card = build_decision_card(question="?", setup=setup, account=account,
                               standard=standard_lens(_rising()))
    assert setup.reward_risk == pytest.approx(2.0)
    assert card.risk["reward_risk_gross"] == 2.0
    assert card.risk["reward_risk"] < 1.0
    assert card.verdict != "add"


def test_reward_risk_is_gross_when_no_costs_are_supplied():
    card = build_decision_card(question="?", setup=_setup(stop=95, target=110),
                               account=Account(equity=10_000),
                               standard=standard_lens(_rising()))
    assert card.risk["reward_risk"] == card.risk["reward_risk_gross"] == 2.0


# ---------------------------------------------------------------- one score per card

def test_a_card_carries_one_score():
    """The rubric rounded one way and the rule engine another, so `score_meta.score_10` could
    read six beside a lens score of seven."""
    for comps in ({"support_proximity": 8, "reward_risk": 5, "trend_quality": 7, "event_risk": 6},
                  {"support_proximity": 6, "reward_risk": 6, "trend_quality": 8, "event_risk": 6},
                  {"support_proximity": 10, "reward_risk": 10, "trend_quality": 8, "event_risk": 6}):
        scored = rubric.score(rubric.RubricInputs(
            price=100, support=99, target=120, atr=1.0, trend_score=1, days_to_earnings=30))
        assert scored["score_10"] == rules.score_10(scored["components"])


def test_rubric_composite_rounds_half_up_like_the_rule_engine():
    scored = rubric.score(rubric.RubricInputs(price=100, support=99, target=120, atr=1.0,
                                              trend_score=1, days_to_earnings=30))
    total = sum(scored["components"].values())
    if total % 4 == 2:                                   # a mean ending in .5
        assert scored["score_10"] == total // 4 + 1


def test_fair_value_rounds_halves_up():
    """Between lines of 1 and 91 the raw score is 10 - (price - 1) / 10, so a price ending
    in 6 lands exactly on a half. Banker's rounding sends those halves down to the even number."""
    score = lambda price: rubric.fair_value_score(price, green_line=1, red_line=91)["score_10"]
    assert score(36) == 7          # raw 6.5, not 6
    assert score(56) == 5          # raw 4.5, not 4
    assert score(16) == 9          # raw 8.5, not 8
    assert score(35) == 7 and score(37) == 6


# ---------------------------------------------------------------- the disagreement block

def test_bullish_lens_against_a_mixed_standard_lens_is_a_disagreement():
    """"mixed" is the ordinary reading for any ticker with fewer than 64 bars. The card said
    "Both lenses are negative"."""
    out = reconcile({"action": "add", "score_10": 9}, {"action": "mixed",
                                                       "charts_against": 0, "charts_supported": 1,
                                                       "measures_evaluated": 2}, "green")
    assert out["exists"] is True
    assert "negative" not in out["summary"]
    assert "undecided" in out["summary"]


def test_a_sized_trail_is_not_described_as_unsized():
    out = reconcile({"action": "trail", "shares": 5}, {"action": "favourable"}, "green")
    assert out["exists"] is True
    assert "will not size" not in out["summary"]
    assert "too low to add" in out["summary"]


def test_a_refusal_to_size_names_the_alert_only_when_the_alert_did_it():
    by_budget = reconcile({"action": "pass", "score_10": 10, "shares": 0},
                          {"action": "favourable"}, "green")
    assert "alert posture" not in by_budget["tie_break"]
    assert "risk budget" in by_budget["tie_break"]

    by_alert = reconcile({"action": "pass", "score_10": 9, "shares": 0},
                         {"action": "favourable"}, "orange")
    assert "alert posture" in by_alert["tie_break"]


def test_reconcile_matrix_covers_every_pair():
    for lens_action in ("add", "hold", "trail", "pass"):
        for std_action in ("favourable", "mixed", "unfavourable"):
            out = reconcile({"action": lens_action}, {"action": std_action}, "green")
            assert isinstance(out["exists"], bool) and out["summary"] and out["tie_break"]
            if lens_action in ("add", "hold") and std_action == "mixed":
                assert out["exists"] is True


# ---------------------------------------------------------------- the standard lens

def test_standard_lens_counts_the_measures_it_actually_read():
    short = standard_lens(_bars([100 + i for i in range(30)]))
    assert short["momentum"]["available"] is False
    assert short["measures_evaluated"] == 1

    long = standard_lens(_rising())
    assert long["measures_evaluated"] == 2, "no valuation history is supplied in production"

    with_valuation = standard_lens(_rising(), valuation={"available": True, "percentile": 0.2})
    assert with_valuation["measures_evaluated"] == 3


def test_valuation_percentile_reports_a_real_median():
    from mmdesk.standard import valuation_percentile

    assert valuation_percentile(50, [5, 10, 15, 100])["median"] == 12.5


# ---------------------------------------------------------------- citations

def test_a_card_cites_the_rules_that_govern_it():
    card = build_decision_card(question="?", setup=_setup(alert="orange"),
                               account=Account(equity=10_000), standard=standard_lens(_rising()))
    cited = {c["rule"] for c in card.lens["cited_rules"]}
    assert "orange_hedge" in cited and "yellow_alert" not in cited

    green = build_decision_card(question="?", setup=_setup(), account=Account(equity=10_000),
                                standard=standard_lens(_rising()))
    cited = {c["rule"] for c in green.lens["cited_rules"]}
    assert "orange_hedge" not in cited and "risk_reward_scale" in cited
    assert green.lens["exits"] == []


def test_a_tenants_attribution_travels_with_its_card():
    card = build_decision_card(question="?", setup=_setup(), account=Account(equity=10_000),
                               standard=standard_lens(_rising()))
    card.attribution = "Built with Example Author's permission."
    assert card.to_dict()["attribution"].startswith("Built with")
    assert "Example Author" in card.to_markdown()


# ---------------------------------------------------------------- invalid numbers

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0])
def test_non_finite_levels_are_refused(bad):
    with pytest.raises(ValueError):
        Setup("T", price=bad, invalidation=90, first_target=130, component_scores={"a": 5})
    with pytest.raises(ValueError):
        Account(equity=bad)


def test_absurd_cash_is_refused():
    with pytest.raises(ValueError):
        Account(equity=1_000, cash_pct=1.5)
    with pytest.raises(ValueError):
        Account(equity=1_000, cash_pct=float("nan"))
