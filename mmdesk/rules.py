"""Rule engine — a published method expressed as deterministic code.

Every rule here traces to a timestamp in the corpus. The LLM may explain what
this module returns; it may never compute these values itself. Where the method gives no
threshold (the alert trigger), the trigger is an explicit user input, not an
invented constant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

Mode = Literal["swing", "intraday"]
Alert = Literal["green", "yellow", "orange", "red"]
Action = Literal["add", "hold", "trim", "pass", "NEEDS_INPUT"]

# Citations for the rules below — video id + start second in the corpus.
CITATIONS = {
    "risk_reward_scale": ("9PhWx9rzIaU", 100),
    "position_by_score": ("9PhWx9rzIaU", 1694),
    "tiered_size": ("9PhWx9rzIaU", 3032),
    "three_point_rule": ("9PhWx9rzIaU", 1737),
    "sell_half_at_double": ("9PhWx9rzIaU", 1799),
    "yellow_alert": ("9PhWx9rzIaU", 2509),
    "orange_hedge": ("9PhWx9rzIaU", 2502),
    "cash_is_position": ("9PhWx9rzIaU", 1014),
    "reversal_disclaimer": ("9PhWx9rzIaU", 64),
}

MAX_POSITION_PCT = 0.10      # "upward of 10%" for high-conviction names (50:32)
DEFAULT_RISK_PCT = 0.01      # user input; the common professional default
MAX_DAY_RISK_PCT = 0.02      # intraday: cap total risk for the session
YELLOW_MIN_SCORE = 8         # yellow posture = only the best odds


@dataclass
class Account:
    equity: float
    risk_pct_per_trade: float = DEFAULT_RISK_PCT
    cash_pct: float | None = None      # user's current cash; None = unknown
    min_cash_pct: float = 0.0          # user-set floor, never assumed by the app
    commission_per_share: float = 0.0  # round-trip cost per share, user/broker supplied
    slippage_bps: float = 0.0          # per side, in basis points of price
    fixed_fee_per_order: float = 0.0   # flat fee charged once per order
    existing_open_risk: float = 0.0    # currency already at risk in open positions
    allow_fractional_shares: bool = False

    def __post_init__(self) -> None:
        if self.equity <= 0:
            raise ValueError("equity must be > 0")
        if not 0 < self.risk_pct_per_trade <= 0.05:
            raise ValueError("risk_pct_per_trade must be in (0, 0.05]")
        if not 0 <= self.min_cash_pct < 1:
            raise ValueError("min_cash_pct must be in [0, 1)")
        if self.commission_per_share < 0:
            raise ValueError("commission_per_share cannot be negative")
        if not 0 <= self.slippage_bps <= 200:
            raise ValueError("slippage_bps must be in [0, 200]")
        if self.fixed_fee_per_order < 0:
            raise ValueError("fixed_fee_per_order cannot be negative")
        if self.existing_open_risk < 0:
            raise ValueError("existing_open_risk cannot be negative")


@dataclass
class Setup:
    ticker: str
    price: float
    invalidation: float            # where the thesis is wrong (support / stop)
    first_target: float            # resistance / expected exit
    component_scores: dict[str, int]   # each 0..10-weighted component
    mode: Mode = "swing"
    alert: Alert = "green"
    entry_price: float | None = None
    time_stop_days: int | None = None
    atr: float | None = None

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("price must be > 0")
        if self.invalidation <= 0 or self.first_target <= 0:
            raise ValueError("levels must be > 0")
        if self.invalidation >= self.price:
            raise ValueError(
                "invalidation must be below the current price for a long setup; "
                "short setups are out of scope in v1"
            )
        if self.first_target <= self.price:
            raise ValueError("first_target must be above the current price for a long setup")
        for name, v in self.component_scores.items():
            if not 0 <= v <= 10:
                raise ValueError(f"component {name!r} must be 0..10, got {v}")

    @property
    def risk_per_share(self) -> float:
        return self.price - self.invalidation

    @property
    def reward_per_share(self) -> float:
        return self.first_target - self.price

    @property
    def reward_risk(self) -> float:
        return self.reward_per_share / self.risk_per_share


def score_10(component_scores: dict[str, int]) -> int:
    """Composite 1-10 score: the half-up rounded mean of the component scores.

    The author's own description is a chart judgement, not an arithmetic sum. We expose
    the components so two raters can disagree visibly, and we clamp to 1..10
    because a 0 would mean "no score", not "worst score".

    Rounding is half-up on purpose: Python's built-in round() is banker's
    rounding, which turns 8.5 into 8 and would quietly bias every borderline
    score downward. An empty dict returns 0 as an explicit "not scored" sentinel.
    """
    if not component_scores:
        return 0
    mean = Decimal(sum(component_scores.values())) / Decimal(len(component_scores))
    rounded = int(mean.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(1, min(10, rounded))


def position_pct(score: int, alert: Alert) -> float:
    """Position size as a share of equity, from the score — not from conviction."""
    if alert in ("orange", "red"):
        return 0.0
    if alert == "yellow" and score < YELLOW_MIN_SCORE:
        return 0.0
    return round(MAX_POSITION_PCT * score / 10, 4)


def _floor_shares(value: Decimal) -> Decimal:
    """Always round down. Rounding to nearest is how a risk budget gets breached."""
    if value >= 1:
        return value.quantize(Decimal("1"), rounding=ROUND_DOWN)
    return value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def size_position(setup: Setup, account: Account) -> dict:
    """Shares from the risk budget, with every cap and cost made explicit.

    Invariant enforced below: the maximum plausible loss at invalidation — including
    entry/exit slippage, round-trip commission and a flat fee — never exceeds the
    stated risk budget minus risk already committed to open positions.

        budget        = equity * risk_pct          (intraday: also capped at MAX_DAY_RISK_PCT)
        available     = budget - existing_open_risk - fixed_fee_per_order
        per_share     = (entry_adj - stop_adj) + commission_per_share
        by_risk       = floor(available / per_share)
        by_conviction = floor(equity * max_pct(score) / entry_adj)
        by_cash       = floor(available_cash / entry_adj)
        shares        = min(by_risk, by_conviction, by_cash)

    If the result is below the smallest tradeable size, the answer is zero shares with a
    reason — never a forced single share that risks more than the user asked for.

    Arithmetic is Decimal end to end: binary floats turn 0.999 into 0.9989999999 and make
    the budget comparison unreliable exactly at the boundary.
    """
    score = score_10(setup.component_scores)
    pct = position_pct(score, setup.alert)
    d_equity = Decimal(str(account.equity))
    d_price = Decimal(str(setup.price))
    d_stop = Decimal(str(setup.invalidation))

    if pct == 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "reason": f"no new exposure: alert={setup.alert}, score={score}",
            "citations": [CITATIONS["position_by_score"], CITATIONS["tiered_size"]],
        }

    slip = Decimal(str(account.slippage_bps)) / Decimal(10000)
    d_comm = Decimal(str(account.commission_per_share))
    d_fixed = Decimal(str(account.fixed_fee_per_order))
    d_open = Decimal(str(account.existing_open_risk))

    entry_adj = d_price * (1 + slip)
    exit_adj = d_stop * (1 - slip)
    per_share = (entry_adj - exit_adj) + d_comm

    budget = d_equity * Decimal(str(account.risk_pct_per_trade))
    if setup.mode == "intraday":
        budget = min(budget, d_equity * Decimal(str(MAX_DAY_RISK_PCT)))

    available = budget - d_open - d_fixed
    notes: list[str] = []

    if available <= 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "risk_at_stop": 0.0, "risk_budget": round(float(budget), 2),
            "reason": (
                "risk budget already exhausted by open positions and fees: "
                f"{float(d_open):.2f} committed vs {float(budget):.2f} budget"
            ),
            "citations": [CITATIONS["position_by_score"]],
        }

    if per_share <= 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "risk_at_stop": 0.0, "risk_budget": round(float(budget), 2),
            "reason": "stop is at or above entry after costs: no definable risk per share",
            "citations": [CITATIONS["position_by_score"]],
        }

    by_risk = _floor_shares(available / per_share)
    by_conviction = _floor_shares((d_equity * Decimal(str(pct))) / entry_adj)
    by_cash = None
    if account.cash_pct is not None and account.min_cash_pct > 0:
        spendable = max(
            Decimal(0),
            (Decimal(str(account.cash_pct)) - Decimal(str(account.min_cash_pct))) * d_equity,
        )
        by_cash = _floor_shares(spendable / entry_adj)

    candidates = {"risk": by_risk, "conviction": by_conviction}
    if by_cash is not None:
        candidates["cash"] = by_cash
    capped_by = min(candidates, key=lambda k: candidates[k])
    shares = candidates[capped_by]

    if not account.allow_fractional_shares:
        shares = shares.quantize(Decimal("1"), rounding=ROUND_DOWN)

    if shares <= 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "risk_at_stop": 0.0, "risk_budget": round(float(budget), 2),
            "reason": (
                "stop too wide for the risk budget: one share risks "
                f"{float(per_share):.2f} against {float(available):.2f} available "
                f"(limited by {capped_by})"
            ),
            "costs": {
                "slippage_bps_per_side": account.slippage_bps,
                "commission_per_share_round_trip": account.commission_per_share,
                "fixed_fee_per_order": account.fixed_fee_per_order,
            },
            "capped_by": capped_by,
            "citations": [CITATIONS["position_by_score"]],
        }

    notional = shares * entry_adj
    risk_at_stop = shares * per_share + d_fixed

    if capped_by == "cash":
        notes.append("cash floor limited the size")
    if account.existing_open_risk:
        notes.append(f"existing open risk {float(d_open):.2f} deducted before sizing")

    # The promise: never above the stated budget, by construction.
    assert risk_at_stop <= budget + Decimal("0.01"), "sizing exceeded the risk budget"

    return {
        "shares": int(shares) if not account.allow_fractional_shares else float(shares),
        "notional": round(float(notional), 2),
        "pct_of_equity": round(float(notional / d_equity), 4),
        "score_10": score,
        "risk_at_stop": round(float(risk_at_stop), 2),
        "risk_pct_of_equity": round(float(risk_at_stop / d_equity), 4),
        "risk_budget": round(float(budget), 2),
        "available_after_open_risk": round(float(available), 2),
        "costs": {
            "slippage_bps_per_side": account.slippage_bps,
            "commission_per_share_round_trip": account.commission_per_share,
            "fixed_fee_per_order": account.fixed_fee_per_order,
            "total_estimated": round(float(
                shares * d_comm
                + shares * (entry_adj - d_price)
                + shares * (d_stop - exit_adj)
                + d_fixed
            ), 2),
        },
        "effective_entry": round(float(entry_adj), 4),
        "effective_stop": round(float(exit_adj), 4),
        "reward_at_target": round(float(shares * (Decimal(str(setup.first_target)) - entry_adj)), 2),
        "reward_risk": round(float((Decimal(str(setup.first_target)) - entry_adj) / per_share), 2),
        "capped_by": capped_by,
        "cash_cap_applied": int(by_cash) if by_cash is not None else None,
        "notes": notes,
        "citations": [CITATIONS["position_by_score"], CITATIONS["tiered_size"]],
    }


def exit_signals(setup: Setup, current_score: int, unrealised_pct: float) -> list[dict]:
    """Rules he states explicitly: the three-point rule and sell-half-at-double."""
    out: list[dict] = []
    if setup.entry_price is not None:
        entry_score = None  # score at entry must be supplied by the caller/journal
        del entry_score
    if unrealised_pct >= 1.0:
        out.append({
            "rule": "sell_half_at_double",
            "action": "sell 50%",
            "text": "Position has doubled — sell half to lock in a win on the trade.",
            "citation": CITATIONS["sell_half_at_double"],
        })
    return [r | {"current_score": current_score} for r in out]


def three_point_rule(score_at_entry: int, score_now: int) -> dict | None:
    """Score down 3 points from entry → take some profit (28:57)."""
    if score_at_entry - score_now >= 3:
        return {
            "rule": "three_point_rule",
            "action": "take partial profit",
            "text": (
                f"Score fell {score_at_entry} -> {score_now}. He trims when it drops "
                "three points, and calls it more art than science."
            ),
            "citation": CITATIONS["three_point_rule"],
        }
    return None


def alert_posture(alert: Alert, score: int) -> dict:
    """What each alert level permits. The trigger itself is user-defined."""
    if alert == "green":
        return {"size_allowed": True, "min_score": 1,
                "note": "Normal conditions. Stay invested.", "citations": [CITATIONS["yellow_alert"]]}
    if alert == "yellow":
        return {"size_allowed": score >= YELLOW_MIN_SCORE, "min_score": YELLOW_MIN_SCORE,
                "note": "Indices expensive: no speculation, be risk/reward picky.",
                "citations": [CITATIONS["yellow_alert"]]}
    if alert == "orange":
        return {"size_allowed": False, "min_score": 99,
                "note": "Authoret almost certainly going down: favourites only, hedge 1:1.",
                "citations": [CITATIONS["orange_hedge"]]}
    return {"size_allowed": False, "min_score": 99,
            "note": "Red: he knows what is happening and how severe it is. Cash.",
            "citations": [CITATIONS["orange_hedge"], CITATIONS["cash_is_position"]]}


def hedge_notional(equity_exposure: float) -> dict:
    """1:1 inverse-benchmark hedge, with the decay caveat carried in the output."""
    return {
        "rwm_notional": round(equity_exposure, 2),
        "warning": (
            "Daily-reset inverse ETF: effective in a fast decline, decays in a slow one. "
            "No holding period was ever given, so compare against holding cash."
        ),
        "citation": CITATIONS["orange_hedge"],
    }
