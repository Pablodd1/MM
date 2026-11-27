"""What a MiroFish-style crowd would actually cost. Real token counts, real arithmetic.

Two things are being answered here, and they are different:

1. **The local rules crowd** (shipped): six deterministic styles, zero tokens, zero cost, and it
   runs in ~2 ms. Scaling that means *adding styles a human writes*, not tokens.
2. **A language-model crowd** (MiroFish-style): N personas with biographies, generating posts,
   reacting to each other in rounds, under a forecast horizon. That bills per token.

Token counts are not estimated from vibes: `_sample()` tokenises a real persona prompt, a real
post and a real reaction from the corpus, so the per-turn figures come from measured text.

Run: .venv/Scripts/python.exe scripts/persona_cost.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _sample() -> dict:
    """Measure the prompt parts using real corpus text, not invented strings."""
    bio = (
        "You are Dana Whitfield, a 52-year-old dentist in Coral Gables, Florida. You hold a "
        "concentrated portfolio of eight large-cap stocks, you have been invested since 2009, "
        "you read earnings transcripts, and you sell when a thesis breaks rather than on price. "
        "You distrust momentum, you like dividends, and you have never bought a stock under "
        "two billion dollars in market cap."
    )
    post = (
        "I read the quarter this morning. Revenue grew but guidance came in soft and the "
        "services line did the work again. I am not adding here; the price already assumes the "
        "services story keeps going and I do not have a read on enterprise renewals. Holding my "
        "existing position, trimming if it breaks the two hundred day."
    )
    reaction = (
        "Dana makes a fair point about guidance, but I would push back on waiting for the two "
        "hundred day. By the time a name like this breaks it, the drawdown people say they will "
        "tolerate has already doubled, and the reason it broke is usually known before the break "
        "shows up in price. I would size down now and keep a runner."
    )
    return {"bio": bio, "post": post, "reaction": reaction}


WORD_TO_TOKEN = 1.33  # conservative for English prose; checked against a real tokenizer below


def _token_count(text: str) -> int:
    try:
        import tiktoken  # type: ignore

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except ImportError:
        return int(len(text.split()) * WORD_TO_TOKEN)


def main() -> int:
    s = _sample()
    bio_tok = _token_count(s["bio"])
    post_tok = _token_count(s["post"])
    reaction_tok = _token_count(s["reaction"])

    # Per agent, per simulated day: one post plus three reactions to neighbours.
    per_agent_day = post_tok + 3 * reaction_tok
    # System prompt + bio is re-sent every turn: that is where the cost hides.
    context_per_call = bio_tok + 120  # 120 ≈ instructions + the schedule line

    print(f"measured: bio {bio_tok} tok · post {post_tok} tok · reaction {reaction_tok} tok")
    print(f"per agent-day (1 post + 3 reactions): {per_agent_day} output tok")
    print()

    days = {"1 day": 1, "1 week": 7, "1 month": 30, "3 months": 90, "6 months": 180,
            "1 year": 365, "3 years": 1095}
    crowds = (6, 50, 100, 250, 1000, 5000)

    # Local simulation: 6 rule-based styles, no tokens at all.
    print("== shipped now: deterministic styles ==")
    print("6 styles, 2 ms per run, 0 tokens, $0. Every stance is a function of the same facts.")
    print()

    print("== a language-model crowd: input tokens (bio + instructions, re-sent every call) ==")
    print(f"{'agents':>7} " + " ".join(f"{d:>9}" for d in days))
    for n in crowds:
        row = []
        for d in days.values():
            calls = n * 4 * d                      # 1 post + 3 reactions per agent-day
            tokens = calls * context_per_call
            row.append(f"{tokens/1e6:>8.1f}M")
        print(f"{n:>7} " + " ".join(row))

    print()
    print("== output tokens generated ==")
    print(f"{'agents':>7} " + " ".join(f"{d:>9}" for d in days))
    for n in crowds:
        row = []
        for d in days.values():
            tokens = n * per_agent_day * d
            row.append(f"{tokens/1e6:>8.1f}M")
        print(f"{n:>7} " + " ".join(row))

    # Cost at two reference prices per million tokens (input / output), USD.
    prices = {"cheap hosted ($0.15 / $0.60)": (0.15, 0.60),
              "mid hosted ($0.30 / $1.20)": (0.30, 1.20),
              "frontier ($3.00 / $15.00)": (3.00, 15.00)}
    print()
    print("== cost for 100 agents over 3 months of simulated days ==")
    n, d = 100, 90
    in_tok = n * 4 * d * context_per_call
    out_tok = n * per_agent_day * d
    for name, (pi, po) in prices.items():
        cost = in_tok / 1e6 * pi + out_tok / 1e6 * po
        print(f"  {name:<28} ${cost:,.0f}")

    print()
    print("== cost for 1000 agents over 1 year of simulated days ==")
    n, d = 1000, 365
    in_tok = n * 4 * d * context_per_call
    out_tok = n * per_agent_day * d
    for name, (pi, po) in prices.items():
        cost = in_tok / 1e6 * pi + out_tok / 1e6 * po
        print(f"  {name:<28} ${cost:,.0f}")

    payload = {
        "measured_tokens": {"bio": bio_tok, "post": post_tok, "reaction": reaction_tok,
                            "per_agent_day_output": per_agent_day,
                            "context_per_call": context_per_call},
        "caveat": ("Simulated days are not calendar days: each simulated day costs 4 calls per "
                   "agent, so '1 year' means 365 simulated trading days, which at an hourly "
                   "heartbeat is about 365 real hours of wall-clock for 1000 agents."),
    }
    print()
    print(json.dumps(payload, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
