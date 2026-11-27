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

## 6. The narrative layer, keyed and unkeyed

The digit ban is enforced the same way wherever prose comes from: the whole completion is checked,
whatever keys the model chose (an adapter trained under `reasoning` used to slip past it). The
template fallback labels itself, and so does every model-written passage. What is still true: the
hosted and adapter paths are exercised by the suite with stand-ins, and a keyed live run should be
recorded before claiming model-written output in front of a client.

## 7. No track record

`confidence.calibration` says `uncalibrated`, and it is telling the truth. The journal is empty
of settled entries; nothing here has been scored against outcomes over months. Any performance
implication would be fabricated.

## 8. Tenants are addressed by id, not credentials

There is no login. A tenant id is an address: anyone who knows one can analyse under it, and the
deterministic endpoints are open by design. What is protected: journals (loopback, admin token, or
the per-browser `X-Journal-Id` scope), the tenant roster and adapter routes (loopback or admin
token; on a serverless host, token only), and the adapter gate. Journals have no encryption at
rest. Real multi-user needs auth with the tenant inside the token — see `docs/HANDOVER.md` §7.

## 9. Third-party content

The corpus and transcripts are not in the tree, and `.gitignore` plus `.vercelignore` keep them out
of commits and deploys; a hygiene test fails the suite if local state is ever tracked. Note that
the full transcript index remains retrievable from **git history** (commit `1d67d2c`) — that is an
owner decision recorded in `docs/HANDOVER.md` §11, not something code can fix. The app itself cites
and links rather than reproducing long excerpts.

## 10. Attribution is per tenant, never global

The shipped product names no one: the engine implements a published method and the UI, card and
docs describe it generically. Consent to use a person's name is recorded in `docs/CONSENT.md` and
is rendered only through a tenant's own `attribution` field — a global label would attribute every
tenant's output to one person, which is exactly the claim to avoid. A written release is still
required before any public, paid, or presented-as-theirs use.

## 11. Retrieval: calibrated refusal, uncalibrated relevance (updated 2026-11-24)

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

**The cross-encoder reranker is now built — and measured as not yet better (2026-11-27).**
`mmdesk/rerank.py` runs fastembed's ONNX MiniLM cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2`,
Apache-2.0) as an opt-in layer: `MMDESK_RERANKER=1` reranks BM25's candidate pool and serves
only passages the model scores as actually answering the question. On clean text it separates
sharply — the true sell-half rule scored +6.2 against −2.0 and −11.1 for the decoys. On the
real corpus it does not win: over the golden set, calibrated BM25 holds hit@1 4/11, hit@5 9/11,
while the best rerank variant trades hit@1 5/11 for hit@5 6/11, and the threshold-only variant
drops hit@5 to 6/11. None dominates, so **it ships off by default** and BM25 stays the served
path; `/health` reports which is active. It may earn its place as the corpus grows or the
passages get more structured — the measurement script is the arbiter, not the intuition.

## 12. The journal has no history yet

Grading is implemented and tested: gap fills, intrabar stops, split-adjusted data, corporate
action flags, and horizons that grade only once they have closed. What it does not have is *time*:
no 30/90/180/365-day horizon has elapsed on real calls, so any accuracy claim would be invented,
and none is made.
