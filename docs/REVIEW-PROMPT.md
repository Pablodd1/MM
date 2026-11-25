# Review prompt — paste this to another AI developer

You are reviewing a working prototype called **MM Desk**. Your job is to find what is wrong,
missing, or dishonest about it — not to praise it. Be specific and cite files and lines.

## What the owner asked for

> "A platform where you ask investment questions, and the avatar of his face will answer with a
> human voice, back and forth like a phone call. I'll give you graphics, you go online and get
> data. It's for investment decision making and how to put the money. Great knowledge of
> day-to-day and day trading. Full platform, UI and documentation, so I can ask another AI
> developer to review it. Do not guess, do not assume."

The subject is **Mark Gomes ("Money Mark")**, a public stock-market creator. The owner intends
to present the finished product to him as a surprise, built from his public videos. Mark has
**not** consented, is not involved, and has no relationship with this project.

## What exists (v0.1.0)

```
mmdesk/
  rules.py        his published rules as deterministic code, each with a corpus citation
  rubric.py       documented 0-10 proxy for his subjective chart score (component bands in code)
  standard.py     trend, momentum, ATR, realised vol, valuation percentile — pure maths
  decision.py     builds the card, reconciles the two lenses
  card.py         card dataclass, markdown renderer, disclaimer
  journal.py      append-only journal, grades calls at 30/90/180/365d, counts losers
  data.py         market data grounding; records missing fields instead of estimating
  llm.py          DeepSeek (OpenAI-compatible) narrative layer with a number-grounding check
  service.py      FastAPI: /, /health, /api/analyze, /api/journal, /api/journal/score, /api/chart
  retrieval_shim.py  corpus adapter that reports honestly when no index exists
  gpu.py          Windows CUDA DLL discovery for faster-whisper
  static/index.html  single-page UI
batch_transcribe.py  resumable transcription of 82 videos / 130.7 h (large-v3, batched, GPU)
build_index.py       time-windowed passages with video id + timestamp + deep link
retrieval.py         BM25 with a refusal path
tests/               35 tests, all passing
docs/                APP-PLAN, BUILD-CONTRACT, reviews and roadmap
```

Verified by the author: `pytest -q` → 35 passed. A live call to `/api/analyze` for NVDA
returned a card with a real price, computed levels, 15 shares sized to 0.94% account risk, a
disagreement block, five cited corpus passages, and a journal id.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install fastapi uvicorn yfinance httpx python-multipart pytest faster-whisper
uvicorn mmdesk.service:app --port 8791
curl -s localhost:8791/health
curl -s -X POST localhost:8791/api/analyze -H 'Content-Type: application/json' \
  -d '{"question":"Is NVDA a buy here?","ticker":"NVDA","account_equity":50000,"risk_pct":0.01}'
```

No API key is required. Without `DEEPSEEK_API_KEY` the narrative falls back to a deterministic
template and the card says so.

## The rules the app enforces (check they match the source)

| Rule | Implementation | Citation |
|---|---|---|
| Risk/reward score drives size | `rules.position_pct`, cap 10% of equity | 01:44, 50:32 |
| Three-point rule | `rules.three_point_rule` — trims at ≥3 points down | 28:57 |
| Sell half at 2× | `rules.exit_signals` | 29:59 |
| Yellow: only score ≥8 | `rules.alert_posture` | 41:49 |
| Orange/red: no new exposure | `rules.position_pct` → 0 | 42:02 |
| Hedge 1:1 with RWM, with decay warning | `rules.hedge_notional` | 42:15 |
| Cash is a position | `Account.cash_pct` / `min_cash_pct` | 16:54 |

## Questions I want answered, in order of importance

1. **Is the position-sizing maths right?** `rules.size_position` applies two caps (risk budget
   and 10% conviction), plus an optional cash floor. Find a case where the output would lose
   more than the stated risk at the stop, or where rounding creates risk above the budget.
2. **Is the rubric defensible?** `rubric.py` maps measurable inputs to his 1–10 score with
   hard-coded bands. Are the bands reasonable, and what would inter-rater agreement look like if
   two people applied them? Is presenting a composite as his score misleading even with the
   label?
3. **Where can a number leak in from the model?** `llm.unpaced_numbers` (see `llm.py`) rejects
   numbers absent from context and retries once. Find an evasion — spelled-out numbers, units,
   percentages, ranges, dates, ticker-adjacent digits — and say whether the fallback is safe.
4. **Is the corpus refusal real?** `retrieval.Corpus.search` returns `[]` below a BM25
   threshold. Test with questions he genuinely never addresses. Is `min_score = 4.0` defensible,
   or can it return confident-sounding but irrelevant passages?
5. **Journal scoring:** `journal.score_entries` grades on the first bar at/after each horizon and
   marks `stopped` if any bar in the window touched invalidation. What bias does that introduce
   (sampling frequency, gaps, dividends, splits, look-ahead)? Is `stopped` even the right
   definition?
6. **Compliance:** the product is educational, non-consensual regarding Mark's name, and has no
   registered-adviser involvement. What must change before it is shown publicly, sold, or shown
   to Mark himself? Be concrete, not generic.
7. **What is missing that the request implies?** The owner wants chart-image input, voice
   conversation and a face avatar. `/api/chart` currently returns 503 without a vision key.
   What is the smallest correct implementation of chart parsing that does not hallucinate axis
   values?
8. **Architecture:** is the split between deterministic code and the language layer sound? What
   would you cut or merge?

## Ground rules for your review

- Do not assume the corpus is complete. It is being transcribed; `health` reports the count.
- Do not propose a cloud dependency for patient-style privacy reasons — this is personal
  research, but the owner prefers local-first.
- Do not recommend an auto-trader, and do not suggest presenting output as personalised advice.
- If you find a factual error in a citation, say which timestamp is wrong and what it should be.
- Rank findings by severity: **breaks a promise the app makes** > **wrong number** >
  **misleading wording** > **style**.
