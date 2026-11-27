"""A synthetic crowd, built to be honest about what it is.

Six investment styles read the same grounded facts and disagree on purpose. Each is a small
deterministic policy — a weighting over valuation, trend, volatility and drawdown — not a
language model improvising a personality.

Why not MiroFish-style agents posting at each other: a thousand agents sharing one model and
one corpus produce one opinion a thousand times, with borrowed confidence. What actually
carries information is the **spread** between styles: if value says add and the risk manager
says trim, the disagreement is the finding.

So the output is a distribution over stances plus the dispersion, labelled as a sensitivity
check. It is not a forecast, and the API says so in the payload, not just in the docs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STANCES = ("add", "hold", "trim", "pass")
DISCLAIMER = (
    "Synthetic crowd: six rule-based styles reading the same data. This is a sensitivity check "
    "on how different mandates would treat the setup — not a forecast, not a probability, and "
    "not a survey of real investors."
)


@dataclass
class Facts:
    """Everything a persona is allowed to see. No persona gets extra information."""
    ticker: str
    price: float
    score_10: float | None          # fundamental band score, if bands were supplied
    trend: int                       # -3..3 from the standard lens
    momentum: float                  # fractional move over the lookback
    volatility: float                # annualised
    drawdown: float                  # from the 60-bar high, negative fraction
    range_pos: float                 # 0 at the low of the range, 1 at the high


@dataclass
class View:
    persona: str
    mandate: str
    stance: str
    conviction: float                # 0..1, how strongly this style holds its view
    reason: str

    def to_dict(self) -> dict:
        return {"persona": self.persona, "mandate": self.mandate, "stance": self.stance,
                "conviction": round(self.conviction, 2), "reason": self.reason}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _value(f: Facts) -> View:
    if f.score_10 is None:
        return View("Value investor", "buy below fair value", "pass", 0.2,
                    "no valuation bands supplied, so this style has nothing to price against")
    s = f.score_10
    if s >= 7:
        return View("Value investor", "buy below fair value", "add", _clamp((s - 5) / 5),
                    f"price sits near the fair-value line (score {s:.1f}/10)")
    if s >= 4:
        return View("Value investor", "buy below fair value", "hold", 0.4,
                    f"price is mid-band (score {s:.1f}/10): neither cheap nor stretched")
    return View("Value investor", "buy below fair value", "trim", _clamp((5 - s) / 5),
                f"price is at or above the expensive line (score {s:.1f}/10)")


def _momentum(f: Facts) -> View:
    if f.trend >= 2 and f.momentum > 0:
        return View("Momentum trader", "buy strength", "add", _clamp(0.5 + f.momentum * 4),
                    f"trend {f.trend}/3 with positive momentum")
    if f.trend <= -2:
        return View("Momentum trader", "buy strength", "trim", 0.7,
                    f"trend {f.trend}/3: the tape is against the position")
    return View("Momentum trader", "buy strength", "hold", 0.45,
                f"trend {f.trend}/3, momentum {f.momentum:+.1%}: no clear edge")


def _risk(f: Facts) -> View:
    if f.volatility >= 0.6:
        return View("Risk manager", "survive first", "trim", _clamp(f.volatility - 0.3),
                    f"annualised volatility {f.volatility:.0%} is above the risk budget")
    if f.drawdown <= -0.25:
        return View("Risk manager", "survive first", "trim", 0.65,
                    f"{f.drawdown:.0%} below the 60-bar high: a position taken there is losing")
    if f.volatility >= 0.4:
        return View("Risk manager", "survive first", "hold", 0.5,
                    f"volatility {f.volatility:.0%}: hold, size down")
    return View("Risk manager", "survive first", "add", 0.4,
                f"volatility {f.volatility:.0%} is within a normal band")


def _cash(f: Facts) -> View:
    if f.score_10 is not None and f.score_10 < 5:
        return View("Cash holder", "get paid to wait", "hold", 0.6,
                    "a stretched price on the supplied bands: this style sees no reason to add")
    if f.range_pos >= 0.8:
        return View("Cash holder", "get paid to wait", "trim", 0.55,
                    "price is in the top fifth of its range: taking some off is reasonable")
    return View("Cash holder", "get paid to wait", "hold", 0.4,
                "nothing forces a decision here")


def _growth(f: Facts) -> View:
    if f.trend > 0 and (f.score_10 is None or f.score_10 >= 4):
        # No valuation claim is made when no bands were supplied.
        why = ("uptrend and not obviously overvalued on the supplied bands"
               if f.score_10 is not None else "uptrend; no valuation bands supplied")
        return View("Growth buyer", "pay for expansion", "add", _clamp(0.4 + f.momentum * 3), why)
    return View("Growth buyer", "pay for expansion", "hold", 0.35,
                "trend or valuation does not justify adding")


def _contrarian(f: Facts) -> View:
    if f.drawdown <= -0.3 and (f.score_10 is None or f.score_10 >= 6):
        why = f"{abs(f.drawdown):.0%} drawdown" + (
            " into a cheap valuation" if f.score_10 is not None else ", no valuation bands supplied"
        )
        return View("Contrarian", "buy fear", "add", _clamp(abs(f.drawdown)), why)
    if f.range_pos >= 0.9:
        return View("Contrarian", "buy fear", "trim", 0.5,
                    "price is in the top tenth of its range")
    return View("Contrarian", "buy fear", "hold", 0.3, "no dislocation to exploit")


PERSONAS = (_value, _momentum, _risk, _cash, _growth, _contrarian)


def run(facts: Facts) -> dict:
    """Every persona reads the same facts; the disagreements are the output."""
    views = [fn(facts) for fn in PERSONAS]
    counts = {s: 0 for s in STANCES}
    weight = {s: 0.0 for s in STANCES}
    for v in views:
        counts[v.stance] += 1
        weight[v.stance] += v.conviction

    total_w = sum(weight.values()) or 1.0
    distribution = {s: round(weight[s] / total_w, 3) for s in STANCES}
    n = len(views)
    # Modal by weight, not by headcount: conviction is what carries a view.
    modal = max(weight, key=lambda s: (weight[s], counts[s]))
    # Dispersion: how much of the crowd is off the modal stance. A unanimous crowd is
    # suspicious, not reassuring — it usually means every persona shares the same input.
    dispersion = round(1 - counts[modal] / n, 3)

    return {
        "ticker": facts.ticker,
        "facts": {
            "price": round(facts.price, 2),
            "score_10": facts.score_10,
            "trend": facts.trend,
            "momentum": round(facts.momentum, 4),
            "volatility_annualised": round(facts.volatility, 4),
            "drawdown_from_high": round(facts.drawdown, 4),
            "range_position": round(facts.range_pos, 4),
        },
        "views": [v.to_dict() for v in views],
        "counts": counts,
        "weighted_distribution": distribution,
        "modal_stance": modal,
        "dispersion": dispersion,
        "agents": n,
        "label": DISCLAIMER,
        "what_this_is_not": [
            "not a forecast, and not a probability",
            "not a survey: the personas are rules, not people",
            "not independent: every persona reads the same numbers, so their errors correlate",
            "adding more personas of the same style would multiply agreement, not insight",
        ],
    }
