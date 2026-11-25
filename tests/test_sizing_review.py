"""The reviewer's three failure cases, encoded as tests.

Each test is a worked example from the review, so a regression shows up as a failing
number rather than a paragraph of argument.
"""

from __future__ import annotations

import pytest

from mmdesk import rules
from mmdesk.rules import Account, Setup


def setup_(price=10.0, stop=9.001, target=20.0, score=10):
    return Setup("T", price=price, invalidation=stop, first_target=target,
                 component_scores={"a": score})


def test_case_1_rounding_must_not_breach_the_budget():
    """500 / 0.999 = 500.5005 shares. Rounding up to 501 risks 500.499 > 500."""
    acct = Account(equity=100_000, risk_pct_per_trade=0.005)
    out = rules.size_position(setup_(price=10.0, stop=9.001, target=20.0), acct)
    assert out["shares"] == 500                      # floored, never 501
    assert out["risk_at_stop"] <= out["risk_budget"] + 1e-9
    assert out["risk_at_stop"] == pytest.approx(499.5, abs=0.01)


def test_case_2_minimum_one_share_must_not_exceed_the_budget():
    """1.00 budget, 5.00 stop distance: one share would risk five times the budget."""
    acct = Account(equity=1_000, risk_pct_per_trade=0.001)
    out = rules.size_position(setup_(price=100.0, stop=95.0, target=130.0), acct)
    assert out["shares"] == 0
    assert out["risk_at_stop"] == 0
    assert "stop too wide" in out["reason"]


def test_case_3_existing_open_risk_is_deducted():
    """500 budget, 450 already at risk: only 50 is available, so 10 shares at 5/Share."""
    acct = Account(equity=50_000, risk_pct_per_trade=0.01, existing_open_risk=450)
    out = rules.size_position(setup_(price=100.0, stop=95.0, target=130.0), acct)
    assert out["shares"] == 10
    assert out["available_after_open_risk"] == pytest.approx(50.0, abs=0.01)
    assert out["risk_at_stop"] <= out["risk_budget"]


def test_case_3b_open_risk_above_budget_returns_zero():
    acct = Account(equity=50_000, risk_pct_per_trade=0.01, existing_open_risk=600)
    out = rules.size_position(setup_(price=100.0, stop=95.0, target=130.0), acct)
    assert out["shares"] == 0
    assert "exhausted" in out["reason"]


def test_fixed_fee_is_deducted_before_sizing():
    acct = Account(equity=10_000, risk_pct_per_trade=0.01, fixed_fee_per_order=40)
    out = rules.size_position(setup_(price=100.0, stop=95.0, target=130.0), acct)
    # budget 100 - 40 fee = 60 available and risk allows 12 shares, but the 10% conviction
    # cap (1,000 / 100) binds first at 10 shares. The fee still reduces the available budget.
    assert out["available_after_open_risk"] == pytest.approx(60.0)
    assert out["shares"] == 10
    assert out["capped_by"] == "conviction"
    assert out["risk_at_stop"] <= 100


def test_fractional_account_sizes_down_to_fraction():
    acct = Account(equity=1_000, risk_pct_per_trade=0.01, allow_fractional_shares=True)
    out = rules.size_position(setup_(price=100.0, stop=95.0, target=130.0), acct)
    # risk allows 2 shares (10 / 5), conviction cap allows 1 (10% of 1,000 / 100)
    assert out["shares"] == pytest.approx(1.0)
    assert out["capped_by"] == "conviction"
    assert out["risk_at_stop"] <= out["risk_budget"]


def test_fractional_with_wide_stop_returns_fraction_below_one():
    acct = Account(equity=1_000, risk_pct_per_trade=0.001, allow_fractional_shares=True)
    out = rules.size_position(setup_(price=100.0, stop=95.0, target=130.0), acct)
    assert 0 < out["shares"] < 1
    assert out["risk_at_stop"] <= out["risk_budget"] + 1e-9


def test_slippage_cannot_invert_the_stop():
    # Setup() already refuses invalidation >= price, and slippage moves entry up and stop
    # down, so a long setup can never end with stop above entry. This documents why the
    # "per_share <= 0" branch is defensive rather than reachable by construction.
    with pytest.raises(ValueError):
        setup_(price=100.0, stop=100.5)


def test_budget_invariant_holds_across_a_grid():
    """Property check: no combination may exceed the stated budget."""
    for equity in (1_000, 25_000, 100_000):
        for risk in (0.001, 0.005, 0.01, 0.02):
            for price, stop in ((10, 9.001), (100, 95), (250, 240), (40, 39.5)):
                for slip in (0, 15, 50):
                    for comm in (0, 0.01, 0.05):
                        acct = Account(equity=equity, risk_pct_per_trade=risk,
                                       slippage_bps=slip, commission_per_share=comm)
                        out = rules.size_position(setup_(price=price, stop=stop, target=price * 1.3), acct)
                        assert out["risk_at_stop"] <= out["risk_budget"] + 1e-6, (
                            equity, risk, price, stop, slip, comm, out
                        )
