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
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmdesk import rubric, rules  # noqa: E402
from mmdesk.decision import _lens_action  # noqa: E402
from mmdesk.llm import LLMConfig  # noqa: E402

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
    # The same inputs the shipped card uses: the sizer's dict, and reward/risk after costs.
    rr = size["reward_risk"] if size.get("shares") else setup.reward_risk
    action, reason = _lens_action(score, size, alert, rr)

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
        "label": {"stance": action, "engine_reason": reason,
                   "reason_words": reason_in_words(action, size, alert)},
    }


def reason_in_words(action: str, size: dict, alert: str) -> str:
    """The engine's reason with no numeral in it.

    The reason shown on a card carries the score and the ratio. A training target may not:
    it fell back to that text whenever the teacher model was unavailable or refused a case, and
    every offline case carried a digit — teaching the adapter to write what the app rejects.
    """
    if action == "add":
        return ("The composite score clears the add threshold, and reward-to-risk after costs "
                "is at least two to one.")
    if action == "hold":
        return "The score sits in the hold band: the odds are acceptable and no edge justifies adding."
    if action == "trail":
        return ("The score is below the hold band: reward-to-risk is poor, so the exit is "
                "managed rather than added to.")
    cap = size.get("capped_by")
    if cap == "risk":
        return "No position fits the risk budget: a single share would risk more than the budget allows."
    if cap == "cash":
        return "No position fits: the stated cash does not cover a share."
    if cap == "conviction":
        return "No position fits under the position cap that follows from the score."
    if str(size.get("reason", "")).startswith("no new exposure"):
        return f"The {alert} alert posture selected permits no new exposure at this score."
    return "No position fits the risk budget as stated."


DIGIT_RE = re.compile(r"\d")


def configured() -> LLMConfig:
    """The teacher is the configured hosted model. A key with no recognised vendor and no
    named endpoint has nowhere to go, so the build stops instead of guessing one."""
    cfg = LLMConfig.from_env()
    if not cfg.configured:
        raise SystemExit(f"no usable model key: {cfg.problem}")
    return cfg


def ask_teacher(cases: list[dict], cfg: LLMConfig, timeout: float = 120.0) -> dict[int, dict]:
    """One call per batch. Returns {id: {reasoning, falsifier}}; empty on failure."""
    import httpx

    model = cfg.model
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
        r = httpx.post(f"{cfg.base_url}/chat/completions",
                       headers={"Authorization": f"Bearer {cfg.api_key}",
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
        "reasoning": case.get("teacher", {}).get("reasoning") or case["label"]["reason_words"],
        "falsifier": case.get("teacher", {}).get("falsifier")
        or "A close beyond the invalidation level, or the stated trend measure flipping.",
    }
    return {
        "messages": [
            {"role": "system", "content": (
                "You are the decision voice of a swing-trading tool. Given a situation, answer with "
                "a JSON object: stance (add, hold, trail or pass), reasoning, falsifier. Never write "
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
    ap.add_argument("--tenant", default="", help="write data/<tenant>/sft-cases.jsonl")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if not args.out:
        args.out = str(Path("data") / args.tenant / "sft-cases.jsonl") if args.tenant else str(OUT)

    cfg = None if args.dry_run else configured()

    rng = random.Random(args.seed)
    # Balance by drawing until every stance has a quota. Random draws are dominated by "pass"
    # (most sampled situations fail a gate), and a set that is mostly one label teaches the model
    # to say that label — the accuracy looks fine and the model is useless.
    quota = max(1, args.n // 4)
    # The engine's four stances. ("trim" is a crowd stance; the card never emits it, and a
    # bucket for it kept the sampler drawing until the attempt limit.)
    stances = ("add", "hold", "trail", "pass")
    buckets: dict[str, list[dict]] = {stance: [] for stance in stances}
    idx = 0
    attempts = 0
    while attempts < args.n * 60 and any(len(buckets[s]) < quota for s in stances):
        attempts += 1
        case = sample_case(rng, idx)
        idx += 1
        bucket = buckets[case["label"]["stance"]]
        if len(bucket) < quota:
            bucket.append(case)
    cases = [c for stance in stances for c in buckets[stance]]

    if not args.dry_run:
        for start in range(0, len(cases), args.batch):
            chunk = cases[start:start + args.batch]
            written = ask_teacher(chunk, cfg)
            for c in chunk:
                if c["id"] in written:
                    c["teacher"] = written[c["id"]]
            print(f"  batch {start // args.batch + 1}: {len(written)}/{len(chunk)} verbalised")

    # The last gate before anything is written: a target with a numeral would teach the model
    # to write one, which is the one thing inference refuses.
    records, dropped = [], 0
    for case in cases:
        record = to_record(case)
        answer = json.loads(record["messages"][-1]["content"])
        if DIGIT_RE.search(answer["reasoning"] + answer["falsifier"]):
            dropped += 1
            continue
        records.append(record)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    from collections import Counter
    labels = Counter(r["meta"]["label"] for r in records)
    taught = sum(1 for r in records if r["meta"]["teacher"])
    print(f"\nwrote {len(records)} cases -> {out_path}" + (f" ({dropped} dropped: numeral in the target)" if dropped else ""))
    print("labels:", dict(labels))
    print(f"teacher-written: {taught}/{len(records)}")
    if records:
        print("\nsample answer:", records[0]["messages"][-1]["content"][:300])
    return 0 if records else 2


if __name__ == "__main__":
    raise SystemExit(main())
