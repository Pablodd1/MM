"""Standard (non-lens) analysis: valuation, trend, momentum, volatility.

Deterministic maths over fetched numbers. No model involvement, and no number is
invented here — every figure arrives as an argument and its source is carried
through to the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Sequence


@dataclass
class Bars:
    """Daily (or intraday) OHLCV series, oldest first."""
    close: Sequence[float]
    high: Sequence[float] | None = None
    low: Sequence[float] | None = None
    volume: Sequence[float] | None = None
    source: str = "unknown"
    open: Sequence[float] | None = None
    dates: Sequence[str] | None = None

    def __post_init__(self) -> None:
        if len(self.close) < 2:
            raise ValueError("need at least 2 bars")
        if any(c <= 0 for c in self.close):
            raise ValueError("close prices must be positive")


def sma(values: Sequence[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return mean(values[-n:])


def atr(bars: Bars, n: int = 14) -> float | None:
    """Average true range from high/low when available, else close-to-close."""
    if bars.high is not None and bars.low is not None:
        trs = []
        for i in range(1, len(bars.close)):
            tr = max(
                bars.high[i] - bars.low[i],
                abs(bars.high[i] - bars.close[i - 1]),
                abs(bars.low[i] - bars.close[i - 1]),
            )
            trs.append(tr)
        if len(trs) < n:
            return None
        return mean(trs[-n:])
    if len(bars.close) < n + 1:
        return None
    diffs = [abs(bars.close[i] - bars.close[i - 1]) for i in range(1, len(bars.close))]
    return mean(diffs[-n:])


def realised_vol(bars: Bars, n: int = 20) -> float | None:
    if len(bars.close) < n + 1:
        return None
    rets = [
        (bars.close[i] / bars.close[i - 1]) - 1
        for i in range(len(bars.close) - n, len(bars.close))
    ]
    return pstdev(rets) * (252 ** 0.5)


def trend_score(bars: Bars) -> dict:
    """-3..+3 from price vs 20/50/200 SMA and SMA ordering."""
    price = bars.close[-1]
    s20, s50, s200 = sma(bars.close, 20), sma(bars.close, 50), sma(bars.close, 200)
    score = 0
    notes = []
    if s20 is not None:
        if price > s20:
            score += 1
            notes.append("above 20-day")
        else:
            score -= 1
            notes.append("below 20-day")
    if s50 is not None:
        if price > s50:
            score += 1
            notes.append("above 50-day")
        else:
            score -= 1
            notes.append("below 50-day")
    if s200 is not None:
        if price > s200:
            score += 1
            notes.append("above 200-day")
        else:
            score -= 1
            notes.append("below 200-day")
        if s50 is not None:
            if s50 > s200:
                notes.append("50>200 (golden-ish)")
            else:
                notes.append("50<200 (death-ish)")
    return {
        "score": score,
        "sma20": round(s20, 2) if s20 else None,
        "sma50": round(s50, 2) if s50 else None,
        "sma200": round(s200, 2) if s200 else None,
        "notes": notes,
        "source": bars.source,
    }


def momentum(bars: Bars, n: int = 63) -> dict:
    """Return over n bars (about a quarter at 63 sessions)."""
    if len(bars.close) <= n:
        return {"available": False, "n": n}
    ret = bars.close[-1] / bars.close[-1 - n] - 1
    return {"available": True, "n": n, "return": round(ret, 4)}


def range_position(bars: Bars, n: int = 252) -> dict:
    window = bars.close[-n:] if len(bars.close) >= n else bars.close
    lo, hi = min(window), max(window)
    pos = (bars.close[-1] - lo) / (hi - lo) if hi > lo else None
    return {
        "low": round(lo, 2), "high": round(hi, 2),
        "pct_of_range": round(pos, 4) if pos is not None else None,
        "bars_used": len(window), "source": bars.source,
    }


def valuation_percentile(current: float, history: Sequence[float]) -> dict:
    """Where today's multiple sits in its own history (0 = cheapest ever)."""
    if not history:
        return {"available": False}
    below = sum(1 for h in history if h <= current)
    return {
        "available": True,
        "percentile": round(below / len(history), 4),
        "n": len(history),
        "min": round(min(history), 2),
        "max": round(max(history), 2),
        "median": round(median(history), 2),
    }


def standard_lens(bars: Bars, valuation: dict | None = None) -> dict:
    tr = trend_score(bars)
    mo = momentum(bars)
    vol = realised_vol(bars)              # 20-day window, annualised
    vol_60 = realised_vol(bars, n=60)     # quarter window: less jumpy, used for risk sizing
    a = atr(bars)
    price = bars.close[-1]

    bullish = bull_bear = 0
    measures = 1                      # the trend score always exists
    if tr["score"] > 0:
        bullish += 1
    elif tr["score"] < 0:
        bull_bear += 1
    if mo.get("available"):
        measures += 1
        if mo["return"] > 0:
            bullish += 1
        else:
            bull_bear += 1
    if valuation and valuation.get("available"):
        measures += 1
        if valuation["percentile"] <= 0.33:
            bullish += 1
        elif valuation["percentile"] >= 0.67:
            bull_bear += 1

    if bullish - bull_bear >= 2:
        action = "favourable"
    elif bull_bear - bullish >= 2:
        action = "unfavourable"
    else:
        action = "mixed"

    return {
        "action": action,
        "trend": tr,
        "momentum": mo,
        "valuation": valuation or {"available": False},
        # Named for its window: a 20-day annualised figure is a short-horizon shock measure,
        # not "the" volatility of the stock. A 134% print on a 50% name is a spike, not a typo.
        "volatility_annualised": round(vol, 4) if vol else None,
        "volatility_20d_annualised": round(vol, 4) if vol else None,
        "volatility_60d_annualised": round(vol_60, 4) if vol_60 else None,
        "atr": round(a, 4) if a else None,
        "atr_pct_of_price": round(a / price, 4) if a else None,
        "charts_supported": bullish,
        "charts_against": bull_bear,
        # How many of the three measures could actually be evaluated. With no valuation history
        # only two exist, so "x of 3 measures" overstated what was read.
        "measures_evaluated": measures,
        "price": price,
        "source": bars.source,
    }
