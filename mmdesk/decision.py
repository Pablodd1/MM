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


def _lens_action(score: int, pct: float, alert: str, rr: float) -> tuple[str, str]:
    if pct == 0:
        return "pass", f"alert {alert} with score {score}: no new exposure permitted."
    if score >= 8 and rr >= 2:
        return "add", f"score {score}/10 with {rr:.1f}:1 reward/risk — a name he would size."
    if score >= 6:
        return "hold", f"score {score}/10: acceptable odds, no edge worth adding."
    return "trail", f"score {score}/10: poor reward/risk, manage the exit rather than add."


def reconcile(mm: dict, std: dict, alert: str) -> dict:
    lens_bullish = mm.get("action") in ("add", "hold")
    std_bullish = std.get("action") == "favourable"
    std_bearish = std.get("action") == "unfavourable"

    if lens_bullish and std_bearish:
        return {
            "exists": True,
            "summary": (
                "The reference lens likes the odds; the standard lens is against it on "
                f"{std.get('charts_against')} of 3 measures."
            ),
            "tie_break": (
                "He is trading to a defined invalidation level, not to a target. If price closes "
                "below invalidation the trade is wrong regardless of the score — that price, not "
                "the valuation, decides it."
            ),
        }
    if not lens_bullish and std_bullish:
        return {
            "exists": True,
            "summary": "The standard lens likes it; the reference lens will not size it.",
            "tie_break": (
                "That gap is the alert posture and the score, both of which are judgement. If you "
                "take this trade you are overriding his risk rule — do it with money you would "
                "write off, and log why."
            ),
        }
    if lens_bullish and std_bullish:
        return {
            "exists": False,
            "summary": "Both lenses agree the setup is favourable. Disagreement is absent, which "
                       "removes an argument but not the risk.",
            "tie_break": "Agreement is not confirmation: watch the invalidation level.",
        }
    return {
        "exists": False,
        "summary": "Both lenses are negative.",
        "tie_break": "Nothing to settle — pass.",
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
    action, reason = _lens_action(score, size["pct_of_equity"], setup.alert, setup.reward_risk)

    exits = rules.exit_signals(setup, score, unrealised_pct)
    if score_at_entry is not None:
        tp = rules.three_point_rule(score_at_entry, score)
        if tp:
            exits.append(tp)

    cited = [
        {"rule": k, "video_id": v[0], "start_s": v[1],
         "url": f"https://www.youtube.com/watch?v={v[0]}&t={int(v[1])}s",
         "stamp": f"{int(v[1])//3600:02d}:{int(v[1])%3600//60:02d}:{int(v[1])%60:02d}"}
        for k, v in rules.CITATIONS.items()
        if k in {
            "position_by_score", "tiered_size", "three_point_rule",
            "sell_half_at_double", "yellow_alert", "orange_hedge",
            "cash_is_position", "risk_reward_scale",
        }
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
            "reward_risk": round(setup.reward_risk, 2),
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
            {k: h[k] for k in ("video_id", "video_title", "stamp", "url", "text", "score")}
            for h in (corpus_hits or [])
        ],
    )
