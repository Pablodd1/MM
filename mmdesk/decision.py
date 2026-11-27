"""Assemble a decision card from the deterministic parts.

The reconciler is the product: it states where the reference lens and the standard lens
point in opposite directions, and which evidence would settle the argument. A
single confident answer is a horoscope; two lenses that disagree, plus the test
that decides between them, is a decision aid.
"""

from __future__ import annotations

from typing import Any

from . import rules
from .card import Card
from .rules import Account, Setup


def _lens_action(score: int, size: dict, alert: str, rr: float) -> tuple[str, str]:
    """The lens's verdict, with the reason that is actually true of this card.

    Zero shares used to be explained as "alert green with score 10: no new exposure permitted"
    whatever had bound — the alert, the risk budget, the cash cap or the score's own cap. The
    sizer already knows which; its reason is used verbatim.
    """
    if size["pct_of_equity"] == 0:
        return "pass", size.get("reason") or f"no new exposure at alert {alert} with score {score}"
    if score >= 8 and rr >= 2:
        return "add", (
            f"score {score}/10 with {rr:.1f}:1 reward/risk after costs — the score clears the "
            "lens's add threshold."
        )
    if score >= 6:
        return "hold", f"score {score}/10: acceptable odds, no edge worth adding."
    return "trail", f"score {score}/10: poor reward/risk, manage the exit rather than add."


def reconcile(lens: dict, std: dict, alert: str) -> dict:
    """Name the conflict between the two lenses, or state that there is none.

    The standard lens has three readings — favourable, unfavourable and mixed — and "mixed" is
    the ordinary case for any ticker with fewer than 64 bars. A bullish lens next to a mixed
    standard lens used to fall through to "Both lenses are negative".
    """
    action = lens.get("action")
    lens_bullish = action in ("add", "hold")
    lens_pass = action == "pass"
    std_action = std.get("action")
    measures = std.get("measures_evaluated") or 3
    against = std.get("charts_against", 0)
    support = std.get("charts_supported", 0)

    if lens_bullish and std_action == "unfavourable":
        return {
            "exists": True,
            "summary": (
                "The reference lens likes the odds; the standard lens is against it on "
                f"{against} of {measures} measures."
            ),
            "tie_break": (
                "The lens is defined by its invalidation level, not its target. If price closes "
                "below invalidation the trade is wrong regardless of the score — that price, not "
                "the valuation, decides it."
            ),
        }
    if not lens_bullish and std_action == "favourable":
        if not lens_pass:
            return {
                "exists": True,
                "summary": (
                    "The standard lens likes it; the reference lens scores it too low to add "
                    "and only manages the exit."
                ),
                "tie_break": (
                    "A position is sized, so this is about adding, not entering. Watch the "
                    "invalidation level."
                ),
            }
        blocked_by_alert = alert in ("orange", "red") or (
            alert == "yellow" and lens.get("score_10", 0) < rules.YELLOW_MIN_SCORE
        )
        if blocked_by_alert:
            tie_break = (
                "That gap is the alert posture and the score, both of which are your inputs. "
                "Taking this trade overrides the lens's risk rule — do it with money you would "
                "write off, and log why."
            )
        else:
            tie_break = (
                "The lens could not fit a position inside your stated risk budget and caps. The "
                "standard lens does not see those constraints; the budget decides, so loosen "
                "nothing to force the trade."
            )
        return {
            "exists": True,
            "summary": "The standard lens likes it; the reference lens will not size it.",
            "tie_break": tie_break,
        }
    if lens_bullish and std_action == "favourable":
        return {
            "exists": False,
            "summary": "Both lenses agree the setup is favourable. Disagreement is absent, which "
                       "removes an argument but not the risk.",
            "tie_break": "Agreement is not confirmation: watch the invalidation level.",
        }
    if lens_bullish:
        return {
            "exists": True,
            "summary": (
                "The reference lens likes the odds; the standard lens is undecided "
                f"({support} of {measures} measures support a long)."
            ),
            "tie_break": "A neutral tape is not a confirmation. The invalidation level still decides it.",
        }
    if std_action == "unfavourable":
        return {
            "exists": False,
            "summary": "Both lenses are negative.",
            "tie_break": "Nothing to settle — pass.",
        }
    return {
        "exists": False,
        "summary": "Neither lens is positive: the reference lens will not size it and the "
                   "standard lens is undecided.",
        "tie_break": "Nothing to settle — pass.",
    }


# The rules that describe how any card is sized. The rest are cited only on the cards they
# govern: an orange-market hedge rule has no place on a green, fully-sized card.
_ALWAYS_CITED = ("risk_reward_scale", "position_by_score", "tiered_size")
_ALERT_CITED = {
    "yellow": ("yellow_alert",),
    "orange": ("orange_hedge",),
    "red": ("orange_hedge", "cash_is_position"),
}


def build_decision_card(
    *,
    question: str,
    setup: Setup,
    account: Account,
    standard: dict[str, Any],
    score_at_entry: int | None = None,
    unrealised_pct: float = 0.0,
    corpus_hits: list[dict] | None = None,
    grounding: dict | None = None,
) -> Card:
    score = rules.score_10(setup.component_scores)
    size = rules.size_position(setup, account)
    posture = rules.alert_posture(setup.alert, score)
    exits = rules.exit_signals(setup, score, unrealised_pct)
    if score_at_entry is not None:
        tp = rules.three_point_rule(score_at_entry, score)
        if tp:
            exits.append(tp)

    # Reward/risk after the costs the user supplied. The card used to show the gross figure and
    # gate "add" on it, so heavy slippage still read as a two-to-one setup.
    rr = size["reward_risk"] if size.get("shares") else setup.reward_risk
    action, reason = _lens_action(score, size, setup.alert, rr)

    cited_rules = list(_ALWAYS_CITED) + list(_ALERT_CITED.get(setup.alert, ()))
    cited_rules += [ex["rule"] for ex in exits if ex["rule"] in rules.CITATIONS]
    cited = [
        {"rule": k, "video_id": v[0], "start_s": v[1],
         "url": f"https://www.youtube.com/watch?v={v[0]}&t={int(v[1])}s",
         "stamp": f"{int(v[1])//3600:02d}:{int(v[1])%3600//60:02d}:{int(v[1])%60:02d}"}
        for k, v in rules.CITATIONS.items()
        if k in cited_rules
    ]

    lens = {
        "score_10": score,
        "proxy_score": score,
        "score_label": "unvalidated proxy — not a statement of opinion by the material's author",
        "components": setup.component_scores,
        "action": action,
        "position_pct": size["pct_of_equity"],
        "shares": size["shares"],
        "alert": setup.alert,
        "posture": posture["note"],
        "cited_rules": cited,
        "exits": exits,
        "reason": size.get("reason", ""),
        "capped_by": size.get("capped_by"),
    }

    disagreement = reconcile(lens, standard, setup.alert)

    conf = "low"
    if grounding and not grounding.get("missing") and corpus_hits:
        conf = "medium"
    if score >= 8 and standard.get("action") == "favourable" and len(corpus_hits or []) >= 3:
        conf = "medium"

    falsifiers = [
        f"A close below {setup.invalidation:.2f} invalidates the setup — the stop, not the opinion, decides.",
        "Price reaching the first target without a new higher low would mean the exit came from luck, not structure.",
    ]
    if grounding and grounding.get("missing"):
        falsifiers.append(
            "Missing inputs: " + ", ".join(grounding["missing"]) + " — any conclusion is provisional."
        )
    if not corpus_hits:
        falsifiers.append(
            "No passage in this tenant's material covers the question, so the lens rests on the general "
            "rules, not on a specific statement."
        )

    return Card(
        question=question,
        instrument=setup.ticker,
        mode=setup.mode,
        verdict=action,
        verdict_reason=reason,
        lens=lens,
        standard_lens=standard,
        disagreement=disagreement,
        risk={
            "invalidation": setup.invalidation,
            "first_target": setup.first_target,
            "time_stop_days": setup.time_stop_days,
            "risk_per_share": round(setup.risk_per_share, 4),
            "reward_per_share": round(setup.reward_per_share, 4),
            "reward_risk": round(rr, 2),
            "reward_risk_gross": round(setup.reward_risk, 2),
            "shares": size["shares"],
            "notional": size["notional"],
            "pct_of_equity": size["pct_of_equity"],
            "risk_at_stop": size.get("risk_at_stop"),
            "risk_budget": size.get("risk_budget"),
            "costs": size.get("costs"),
            "effective_entry": size.get("effective_entry"),
            "effective_stop": size.get("effective_stop"),
            "capped_by": size.get("capped_by"),
        },
        confidence={
            "level": conf,
            "basis": (
                "Deterministic maths on fetched inputs. Confidence reflects data completeness and "
                "corpus coverage, not certainty about the future."
            ),
            "calibration": "uncalibrated — this build has no published track record yet.",
        },
        falsifiers=falsifiers,
        grounding=grounding or {},
        corpus_hits=[
            {k: h[k] for k in ("video_id", "video_title", "stamp", "url", "text", "score",
                               "synthetic") if k in h}
            for h in (corpus_hits or [])
        ],
    )
