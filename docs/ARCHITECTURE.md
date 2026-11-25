# Architecture

## Why two lenses

A single confident answer is a horoscope. The product's value is the *conflict*: his risk-first
lens and a conventional quant lens applied to the same instrument, with the evidence that would
settle the argument named explicitly. That structure also protects the user — if both lenses say
the same thing, the card refuses to call it confirmation.

## Component map

```
UI (static/index.html)
   │  JSON
   ▼
service.py  ──────────────── FastAPI
   │
   ├── data.Grounder ──────── provider → Bars + fundamentals + timestamps + missing[]
   │        └── standard.py ─ trend, momentum, ATR, vol, valuation percentile
   │
   ├── rubric.py ──────────── 0-10 proxy score from measurable inputs (documented bands)
   ├── rules.py ───────────── his rules as code + citations
   ├── decision.py ────────── card assembly + reconciliation
   ├── retrieval_shim.py ──── corpus passages with video id + timestamp, or []
   ├── llm.py ─────────────── narrative only, number-grounding check, template fallback
   └── journal.py ─────────── append-only calls, graded at 30/90/180/365 d
```

## Invariants (enforced by tests)

1. **No invented numbers.** `data.Grounder` records `missing`; the card surfaces it and the
   service returns 422 rather than a card built on nothing.
2. **No model arithmetic.** Sizing lives in `rules.size_position`; the model receives the result.
3. **No ungrounded prose numbers.** `llm.ungrounded_numbers` compares every number in the model's
   output against the JSON context; a mismatch triggers one retry, then the template.
4. **No persona claim without a citation.** `retrieval.Corpus.search` returns `[]` under the
   threshold; the card then says the corpus is silent.
5. **Stops are recorded as lived.** `journal.score_entries` marks `stopped` if any bar in the
   window touched invalidation, even if price recovered afterwards.
6. **Halves round half-up.** `rules.score_10` uses `Decimal` + `ROUND_HALF_UP`; Python's built-in
   banker's rounding would bias borderline scores downward.

## Data contract

`POST /api/analyze` → the card. Fields that matter to a reviewer:

| Field | Meaning |
|---|---|
| `mm_lens.score_10` / `components` / `score_meta.source` | the score and exactly how it was produced |
| `mm_lens.cited_rules[]` | rule name, video id, start second, deep link |
| `standard_lens.trend.score` | −3…+3 from price vs 20/50/200 SMA |
| `disagreement.exists` / `summary` / `tie_break` | the product |
| `risk.invalidation` / `first_target` / `shares` / `risk_at_stop` | deterministic sizing |
| `grounding.missing[]` / `grounding.level_notes[]` | what was unavailable, and which levels were computed rather than supplied |
| `corpus_hits[]` | verbatim passages with timestamps |
| `narrative.generated_by` | which layer wrote the prose (model or template) |
| `confidence.calibration` | always reports "uncalibrated" — there is no track record yet |

## Level selection

Support and target are **user inputs**. When omitted, the app computes them and labels the fact:

- support = 20-bar low × 0.995
- target = 60-bar high, or price + 2×ATR when that high is already behind price

This is stated in `grounding.level_notes` on every card, so nobody mistakes a mechanical level
for his judgement.

## What is deliberately not here

- Chart vision (`/api/chart` returns 503 without `VISION_API_KEY`; parsing is P2).
- Voice loop and avatar (P3/P4).
- Intraday data. `intraday_supported()` returns False; intraday mode runs on daily bars and the
  card carries a falsifier saying so.
- Multi-user accounts, auth, and persistence beyond a local JSONL journal.
