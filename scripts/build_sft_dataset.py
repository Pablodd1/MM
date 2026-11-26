"""Build the fine-tuning set for the decision voice (Model A).

The label is not invented: every case is generated, run through the same rubric, sizer and action
policy the app uses, and only then verbalised by a teacher model. That means the training target
agrees with the shipped engine by construction — a fine-tune that learned a *different* policy
would be worse than useless, because the app would then disagree with its own model.

Numbers live in the *input* (that is the situation). The assistant target must contain none: stance
is a word, reasoning is prose, the falsifier names a level by description. This is the same guard
the product enforces at inference time, so the model is trained in the shape it must answer.

Usage:
    python scripts/build_sft_dataset.py --n 60 --batch 10          # teacher-written reasoning
    python scripts/build_sft_dataset.py --n 2000 --dry-run          # engine reasons only, offline
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmdesk import rubric, rules                      # noqa: E402
from mmdesk.decision import _mm_action                # noqa: E402
from mmdesk.opinion import api_key, BASE, MODEL       # noqa: E402

OUT = ROOT / "data" / "sft-cases.jsonl"

SYSTEM = """You are the reasoning voice of a disciplined swing-trading decision tool.

For each situation you are given, state the decision the rules produce and explain it the way a
careful investor would. You do not compute: the stance is supplied and the figures are supplied.
You must not write a single digit — no prices, percentages, scores, dates or counts. Name levels by
description ("the invalidation level", "the first target"), never by value.

Reply with a JSON object: {"cases": [{"id": <id>, "reasoning": "...", "falsifier": "..."}, ...]}
in the same order as given. Reasoning is two to four sentences. The falsifier is one sentence
naming the observable event that would prove the decision wrong.
"""


def sample_case(rng: random.Random, idx: int) -> dict:
    price = round(rng.uniform(4, 480), 2)
    drop = rng.uniform(0.02, 0.22)
    support = round(price * (1 - drop), 2)
    target = round(price * (1 + rng.uniform(0.04, 0.55)), 2)
    atr = round(price * rng.uniform(0.015, 0.075), 3)
    equity = rng.choice([5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000])
    risk_pct = rng.choice([0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02])
    cash_pct = rng.choice([None, 0.05, 0.1, 0.2, 0.35, 0.6])
    alert = str(rng.choice(["green", "green", "yellow", "orange", "red"]))
    trend = rng.randint(-3, 3)
    days = rng.choice([None, 3, 7, 14, 30, 45])
    vol = round(rng.uniform(0.18, 1.15), 3)

    scored = rubric.score(rubric.RubricInputs(
        price=price, support=support, target=target, atr=atr,
        trend_score=trend, days_to_earnings=days, realised_vol=vol,
    ))
    comps = scored["components"]
    score = rules.score_10(comps)

    setup = rules.Setup(ticker=f"CASE{idx}", price=price, invalidation=support,
                        first_target=target, component_scores=comps, mode="swing",
                        alert=alert, atr=atr)  # type: ignore[arg-type]
    account = rules.Account(equity=float(equity), risk_pct_per_trade=risk_pct,
                            cash_pct=cash_pct, min_cash_pct=0.0,
                            commission_per_share=0.0, slippage_bps=0.0)
    size = rules.size_position(setup, account)
    action, reason = _mm_action(score, size["pct_of_equity"], alert, setup.reward_risk)

    return {
        "id": idx,
        "situation": {
            "ticker": f"CASE{idx}", "price": price, "support": support, "first_target": target,
            "atr": atr, "trend_score": trend, "days_to_earnings": days,
            "annualised_volatility": vol, "alert": alert,
            "account_equity": equity, "risk_per_trade": risk_pct, "cash_pct": cash_pct,
            "reward_risk": round(setup.reward_risk, 2), "composite_score": score,
            "components": comps, "shares": size["shares"],
            "pct_of_equity": round(size["pct_of_equity"], 4), "capped_by": size.get("capped_by"),
        },
        "label": {"stance": action, "engine_reason": reason},
    }


DIGIT_RE = re.compile(r"\d")


def ask_teacher(cases: list[dict], model: str = MODEL, timeout: float = 120.0) -> dict[int, dict]:
    """One call per batch. Returns {id: {reasoning, falsifier}}; empty on failure."""
    import httpx

    payload_cases = [
        {"id": c["id"], "situation": c["situation"], "decision": c["label"]["stance"],
         "engine_reason": c["label"]["engine_reason"]}
        for c in cases
    ]
    body = {
        "model": model,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"cases": payload_cases}, default=str)},
        ],
    }
    try:
        r = httpx.post(f"{BASE}/chat/completions",
                       headers={"Authorization": f"Bearer {api_key()}",
                                "Content-Type": "application/json"},
                       json=body, timeout=timeout)
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
        out: dict[int, dict] = {}
        for item in data.get("cases", []):
            reasoning = str(item.get("reasoning", "")).strip()
            falsifier = str(item.get("falsifier", "")).strip()
            if not reasoning or not falsifier:
                continue
            if DIGIT_RE.search(reasoning) or DIGIT_RE.search(falsifier):
                continue
            out[int(item.get("id", -1))] = {"reasoning": reasoning, "falsifier": falsifier}
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"  teacher batch failed: {type(exc).__name__}: {exc}"[:200])
        return {}


def to_record(case: dict) -> dict:
    situation = case["situation"]
    answer = {
        "stance": case["label"]["stance"],
        "reasoning": case.get("teacher", {}).get("reasoning") or case["label"]["engine_reason"],
        "falsifier": case.get("teacher", {}).get("falsifier")
        or "A close beyond the invalidation level, or the stated trend measure flipping.",
    }
    return {
        "messages": [
            {"role": "system", "content": (
                "You are the decision voice of a swing-trading tool. Given a situation, answer with "
                "a JSON object: stance (add, hold, trim or pass), reasoning, falsifier. Never write "
                "a numeral; refer to levels and sizes by description."
            )},
            {"role": "user", "content": json.dumps(situation, default=str)},
            {"role": "assistant", "content": json.dumps(answer)},
        ],
        "meta": {"id": case["id"], "label": case["label"]["stance"],
                 "teacher": bool(case.get("teacher")), "score": situation["composite_score"]},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true", help="engine reasons only, no API calls")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if not args.dry_run and not api_key():
        print("no key found at ~/.config/mmdesk/llm.key and MMDESK_LLM_KEY unset")
        return 2

    rng = random.Random(args.seed)
    # Balance by drawing until every stance has a quota. Random draws are dominated by "pass"
    # (most sampled situations fail a gate), and a set that is mostly one label teaches the model
    # to say that label — the accuracy looks fine and the model is useless.
    quota = max(1, args.n // 4)
    buckets: dict[str, list[dict]] = {"add": [], "hold": [], "trim": [], "trail": [], "pass": []}
    idx = 0
    attempts = 0
    while attempts < args.n * 60 and any(len(v) < quota for v in buckets.values()):
        attempts += 1
        case = sample_case(rng, idx)
        idx += 1
        bucket = buckets.setdefault(case["label"]["stance"], [])
        if len(bucket) < quota:
            bucket.append(case)
    cases = [c for stance in ("add", "hold", "trim", "trail", "pass") for c in buckets[stance]]

    if not args.dry_run:
        for start in range(0, len(cases), args.batch):
            chunk = cases[start:start + args.batch]
            written = ask_teacher(chunk)
            for c in chunk:
                if c["id"] in written:
                    c["teacher"] = written[c["id"]]
            print(f"  batch {start // args.batch + 1}: {len(written)}/{len(chunk)} verbalised")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(to_record(case), ensure_ascii=False) + "\n")

    from collections import Counter
    labels = Counter(c["label"]["stance"] for c in cases)
    taught = sum(1 for c in cases if c.get("teacher"))
    print(f"\nwrote {len(cases)} cases -> {out_path}")
    print("labels:", dict(labels))
    print(f"teacher-written: {taught}/{len(cases)}")
    if cases:
        sample = to_record(cases[0])
        print("\nsample answer:", sample["messages"][-1]["content"][:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
