# Response to the review — status of each minimum fix

Line-by-line review needs the code, not a summary: `docs/REVIEW-BUNDLE.md` contains every
requested file, the 57-test inventory, and sample `/health`, `/api/analyze`, `/api/journal` and
retrieval outputs in one paste-able document.

| # | Reviewer's minimum fix | Status | Where / evidence |
|---|---|---|---|
| 1 | Remove Mark Gomes's name, face, voice, likeness | **Resolved by permission** | Mark Gomes gave verbal approval on 2026-11-24 for name, likeness, voice and his published content. Recorded in `docs/CONSENT.md`; a written release is still recommended before public launch or sale. No avatar or voice model exists yet. |
| 2 | Remove or license the transcribed corpus | **Permission granted; written terms open** | Mark's approval covers use of his published content for this project. Commercial redistribution terms are not in writing. Raw audio and transcripts stay git-ignored; only the derived index is committed, in a private repo. |
| 3 | Rename "Mark score" → proxy | **Done** | `mm_lens.proxy_score` plus `score_label: "unvalidated proxy — not a statement by Mark Gomes"` on every card; UI labels it "MM lens (proxy score)". |
| 4 | Sizing: floor, fees, slippage, open risk, reject impossible | **Done** | `rules.size_position`: Decimal + `ROUND_DOWN`, `available = budget − existing_open_risk − fixed_fee`, `min(risk, conviction, cash)`, 0 shares with a numeric reason when the stop is too wide. |
| 5 | Property tests proving no position exceeds the budget | **Done** | `tests/test_sizing_review.py`: the reviewer's three worked cases plus a 1,152-combination grid asserting `risk_at_stop ≤ risk_budget`. |
| 6 | Replace LLM numeric generation with deterministic rendering | **Done** | `llm.contains_digits` rejects any digit in any prose field, retries once, then template. Cloud LLM is off unless `MMDESK_ALLOW_CLOUD_LLM=1`. |
| 7 | Retrieval evaluation set + calibrated threshold | **Done — with a measured limit** | `eval/retrieval_golden.jsonl` (20 labelled questions) + `eval/calibrate_retrieval.py` + `docs/RETRIEVAL-CALIBRATION.md`. Measured: at the old `4.0`, nothing was ever refused; irrelevant questions outscored relevant ones (19.7 vs 8.5). Shipped rule is a **vocabulary gate** (refuse when the question's rarest word never occurs in the corpus) plus a floor of 8. Result: 100% of never-discussed topics refused, 100% of answerable questions served, precision@1 0.82. `mentioned_only` topics are still served — correctly, and labelled as mentions. |
| 8 | Journal assumptions + corporate actions | **Done** | Stops now simulate a real stop order: `open <= stop` fills at the open (`gap_open`), `low <= stop` fills at the stop (`intrabar_stop`), otherwise `not_stopped`. `return` is the lived outcome; `return_hold` keeps the ignored-stop number separately. A >25% drop that does not touch the stop is flagged `corporate_action_suspected` instead of being scored as a loss. New `POST /api/journal/grade` fetches split-adjusted OHLC itself, so prices cannot be supplied after the fact. |
| 9 | Disable `/api/chart` or require OHLC | **Done (disabled)** | Returns 501 `chart_vision_not_available` with `required_alternatives` naming typed levels and OHLC JSON. No pixel inference exists in the build. |
| 10 | Prominent educational/no-advice disclaimers | **Done** | Persistent bar above the fold in the UI, plus the card disclaimer and `docs/`. |
| 11 | Input validation + rate limiting | **Done (minimal)** | Pydantic bounds on every field; 30 requests/min/client in memory; no stack traces returned. Not a substitute for auth before public hosting. |
| 12 | Cloud LLM optional and off by default | **Done** | Default is the deterministic template with `generated_by` naming the reason. `/health.llm.cloud_enabled` reports the flag. |

## Still outstanding, in priority order

1. **Journal sample size** — the grading path is built and tested, but every existing entry
   was created today, so no horizon has elapsed and the live call correctly grades nothing yet.
2. **Reranking** — precision@1 of 0.82 is the honest ceiling of BM25 here; a cross-encoder is
   the next step, not a higher threshold.
3. **Consent and licensing** (fixes 1–2) — decisions for the owner, not the code.
4. **Avatar** — absent; voice now exists (`docs/VOICE.md`), the face does not.

## On the reviewer's "not ready" verdict

Agreed for public use, sale, or a live demo to Mark. The engineering asks are mostly closed;
the two that are not (retrieval calibration, corporate-action handling) are named above rather
than buried. The legal ones cannot be closed by code.
