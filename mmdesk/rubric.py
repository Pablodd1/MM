"""Scoring the 1-10 risk/reward scale.

CORRECTED 2026-11-24 from source evidence (docs/FINDING-rubric-is-fundamental.md): the scale is
anchored to **fundamental** valuation lines drawn on the chart, not to technical support:

    "They are not technical lines… They are fundamental… If you're at the green line,
     that's a 10. This stock is cheap." — RJ8zTCJaJhU @ 00:27:16
    "The gray lines are technical. The red green lines are fundamental." — jCS3eoQiXHI @ 00:48:31

So the primary function here is `fair_value_score`: interpolate price between a green line
(fair value, score 10) and a red line (too expensive, score 1). The user supplies those lines
because the fundamental formula behind them is not known and is not invented here.

`technical_proxy_v1` remains for cases where no bands are available, and is labelled as a proxy.
Every composite rounds through `rules.half_up`, the same rounding `rules.score_10` applies, so a
card never carries two different scores for one set of components.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .rules import half_up

MAX_SCORE = 10
ATR_SUPPORT_BANDS = (0.5, 1.0, 1.5, 2.0, 3.0)
RR_BANDS = (0.5, 1.0, 1.5, 2.0, 3.0)
EARNINGS_BLACKOUT_DAYS = 14

# Where the green/red band mechanic is described in the source material.
BAND_CITATIONS = [
    {"video_id": "RJ8zTCJaJhU", "start_s": 1636,
     "url": "https://www.youtube.com/watch?v=RJ8zTCJaJhU&t=1636s"},
    {"video_id": "jCS3eoQiXHI", "start_s": 2911,
     "url": "https://www.youtube.com/watch?v=jCS3eoQiXHI&t=2911s"},
]


def fair_value_score(price: float, green_line: float, red_line: float) -> dict:
    """The band mechanic: price at/below green = 10, at/above red = 1, linear between.

    Both lines are fundamental valuation levels the user supplies. If red <= green the input is
    rejected rather than silently reordered — the sign of the bands matters.
    """
    if price <= 0 or green_line <= 0 or red_line <= 0:
        raise ValueError("price and both lines must be > 0")
    if red_line <= green_line:
        raise ValueError("red line (expensive) must sit above the green line (fair value)")

    p, green, red = (Decimal(str(x)) for x in (price, green_line, red_line))
    if p <= green:
        raw = Decimal(10)
    elif p >= red:
        raw = Decimal(1)
    else:
        raw = Decimal(10) - Decimal(9) * (p - green) / (red - green)

    score = int(max(Decimal(1), min(Decimal(10), raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
    return {
        "score_10": score,
        "basis": "fundamental_bands",
        "method": "fundamental_bands",
        "inputs": {"price": price, "green_line": green_line, "red_line": red_line,
                   "position_between_lines": round((price - green_line) / (red_line - green_line), 4)},
        "unrounded": float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "source": (
            "band interpolation — green line = 10 (cheap), red line = 1 (too expensive); "
            "the lines are the user's input"
        ),
        "citations": [dict(c) for c in BAND_CITATIONS],
    }


@dataclass
class RubricInputs:
    price: float
    support: float
    target: float
    atr: float | None
    trend_score: int            # -3..+3 from the standard lens
    days_to_earnings: int | None
    realised_vol: float | None = None

    @property
    def risk_per_share(self) -> float:
        return max(1e-9, self.price - self.support)

    @property
    def reward_per_share(self) -> float:
        return max(0.0, self.target - self.price)

    @property
    def reward_risk(self) -> float:
        return self.reward_per_share / self.risk_per_share


def support_proximity_score(distance_atr: float) -> int:
    """Closer to support = better odds. Bands are widest at the top."""
    if distance_atr <= ATR_SUPPORT_BANDS[0]:
        return 10
    if distance_atr <= ATR_SUPPORT_BANDS[1]:
        return 9
    if distance_atr <= ATR_SUPPORT_BANDS[2]:
        return 8
    if distance_atr <= ATR_SUPPORT_BANDS[3]:
        return 6
    if distance_atr <= ATR_SUPPORT_BANDS[4]:
        return 4
    return 2


def reward_risk_score(rr: float) -> int:
    if rr >= RR_BANDS[4]:
        return 10
    if rr >= RR_BANDS[3]:
        return 8
    if rr >= RR_BANDS[2]:
        return 6
    if rr >= RR_BANDS[1]:
        return 4
    if rr >= RR_BANDS[0]:
        return 2
    return 1


def trend_quality_score(trend: int) -> int:
    """-3..+3 mapped to 1..10; a broken trend is not a bargain."""
    return max(1, min(10, round((trend + 3) / 6 * 9 + 1)))


def event_risk_score(days: int | None) -> int:
    """Earnings inside the blackout window is real risk, not a detail."""
    if days is None:
        return 5          # unknown date is neutral, and flagged as missing
    if days < 0:
        return 7
    if days <= 2:
        return 2
    if days <= 7:
        return 4
    if days <= EARNINGS_BLACKOUT_DAYS:
        return 6
    return 9


def score(inputs: RubricInputs) -> dict:
    """Technical proxy — NOT a valuation-band score. See module docstring.

    Use `fair_value_score` when the user can supply the green/red valuation lines; that
    implements the band mechanic. This function remains for instruments where no fundamental
    bands are available, and says so in its output.
    """
    atr = inputs.atr or (inputs.price * 0.02)
    distance_atr = (inputs.price - inputs.support) / atr if atr else 99.0
    comps = {
        "support_proximity": support_proximity_score(distance_atr),
        "reward_risk": reward_risk_score(inputs.reward_risk),
        "trend_quality": trend_quality_score(inputs.trend_score),
        "event_risk": event_risk_score(inputs.days_to_earnings),
    }
    mean = sum(comps.values()) / len(comps)
    composite = max(1, min(MAX_SCORE, half_up(mean)))
    return {
        "score_10": composite,
        "components": comps,
        "method": "technical_proxy_v1",
        "inputs": {
            "distance_to_support_atr": round(distance_atr, 3),
            "reward_risk": round(inputs.reward_risk, 3),
            "trend_score": inputs.trend_score,
            "days_to_earnings": inputs.days_to_earnings,
            "atr": round(atr, 4) if atr else None,
        },
        "source": "technical_proxy_v1 — a technical approximation, not a valuation-band score",
        "limitations": [
            "The source method's score includes chart structure a formula cannot see "
            "(pattern, volume shelf, prior reactions).",
            "Bands were chosen to be inspectable, not fitted to the source material's published "
            "calls; calibrate before trusting the number.",
        ],
    }
