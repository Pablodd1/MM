# Known limitations

Written from actual runs, not from imagination. Each item says what happens now and what would
fix it.

## 1. The rubric can reward a falling knife

**Observed:** a live MSFT run scored **8/10 ("add")** while the standard lens was *unfavourable
on 2 of 3 measures*. Price was $494 with a 60-bar high of $558, so the mechanical target produced
a 4.1:1 reward/risk, and `reward_risk` scored 10/10.

**Why it matters:** the deeper a stock falls, the further away its old high — so the rubric
rewards more damage with a better score. That is the opposite of risk control, and it is
precisely the failure mode his own method is designed to avoid by reading the chart structure a
formula cannot see.

**Fix (not yet applied):** score reward/risk against the *nearest tested resistance* rather than
the 60-bar high, and cap the R:R contribution when price sits below the 200-day and the trend
score is negative. Until then, treat any `score ≥ 8` that coincides with
`standard_lens.trend.score < 0` as suspect — the card already shows both, which is why the
disagreement block exists.

## 2. Levels are mechanical when the user does not supply them

Support = 20-bar low × 0.995; target = 60-bar high or price + 2×ATR. These are labelled in
`grounding.level_notes` on every card, but they are not chart structure — no volume shelf, no
prior reaction point, no measured move. A real implementation needs P2 chart parsing.

## 3. Intraday mode runs on daily bars

`intraday_supported()` returns False and the card appends a falsifier, but the mode still runs,
which could mislead. It should refuse outright until a real-time feed exists.

## 4. Corpus coverage is partial during transcription

`/health` reports the passage count. An answer built on 121 passages from one video is not the
same product as one built on 130 hours. Any "he would say" claim inherits that thinness, and the
card's confidence stays `low`/`medium` accordingly.

## 5. BM25 retrieval has no synonym handling

"position sizing" finds his words; "how much to put in" may not. There is a refusal path, but no
embeddings yet, so recall is weaker than it should be for a 130-hour corpus. This will produce
false "he has not addressed this" answers.

## 6. The narrative layer is unverified against a key

`llm.py` is written and its number-grounding check is unit-tested, but no live call has been made
in this build — no API key is configured on this machine. The template fallback is what produced
every card so far. **Do not claim the model path works until a keyed run is recorded.**

## 7. No track record

`confidence.calibration` says `uncalibrated`, and it is telling the truth. The journal is empty
of settled entries; nothing here has been scored against outcomes over months. Any performance
implication would be fabricated.

## 8. Single user, no auth

The service binds to 127.0.0.1 and has no authentication. It must not be exposed on a network
interface as-is, and the journal has no encryption at rest.

## 9. Third-party content

Transcripts are not committed (see `.gitignore`). The corpus is derived from public videos and is
for research; republishing transcripts would be a copyright problem. The app cites and links
rather than reproducing long excerpts.

## 10. The person it imitates has not consented

The product is labelled an unofficial prototype. It uses Mark's *published rules*, not his face,
voice or current positions. That labelling is a hard requirement, not a nicety — see
`docs/ROADMAP.md`, P4.

## 6. Retrieval: calibrated refusal, uncalibrated relevance (updated 2026-11-24)

The refusal threshold is no longer a guess. `docs/RETRIEVAL-CALIBRATION.md` records the
measurement: at the old `min_score = 4.0`, **nothing was ever refused**, and irrelevant
questions scored higher than relevant ones because BM25 rewards rare-word overlap.

What ships now:

- a **vocabulary gate** — if the question's rarest content word never occurs in any transcript,
  the app refuses and names the word;
- a **score floor of 8** — below it, matched passages are not presented as his view.

Measured on 20 labelled questions: every never-discussed topic refused, every answerable
question served, precision@1 0.82, recall@5 0.82. The gap is real: two of eleven answerable
questions return an off-topic passage (the sell-half and cash rules both surface a company
discussion instead). A cross-encoder reranker is the fix; a bigger threshold is not.

## 7. The journal has no history yet

Grading is implemented and tested: gap fills, intrabar stops, split-adjusted data, corporate
action flags. What it does not have is *time*. Every entry in the journal was created on the day
the app was built, so no 30/90/180/365-day horizon has elapsed and `POST /api/journal/grade`
correctly returns `graded: 0` against live data. Any accuracy claim before those horizons close
would be invented, so none is made.
