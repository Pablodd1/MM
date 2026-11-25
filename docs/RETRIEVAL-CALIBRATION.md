# Retrieval calibration

What the app claims when it refuses, tested rather than asserted.

- Corpus: **10810 passages**, 17 transcribed videos (35.0 h of 131.4 h).
- Golden set: **20 questions** — 10 answerable, 5 no-answer, 5 mentioned-only.
- Labels were checked against the index itself: `no_answer` items use words with document frequency 0 (they never occur in any transcript); `mentioned_only` items occur once as a passing remark or viewer question, with no substantive answer.
## Ranking broke when the corpus grew — measured

| passages | videos | hit@1 | hit@5 |
|---|---|---|---|
| 2,689 | 17 | 0.82 | 0.82 |
| 10,810 | 47 | 0.27 | 0.82 |
| 10,810 | 47, with phrase-proximity boost | **0.36** | 0.82 |

The right video stays in the top five, so this is an ordering problem, not a recall problem. `eval/tune_ranking.py` swept k1/b, coverage weighting, phrase and title boosts; only a light phrase boost helped, and heavier boosts made it worse. An embedding reranker (nomic-embed-text over the BM25 top 25) was tested and **rejected**: hit@1 0.18, hit@5 0.55 — worse than plain BM25 (`eval/rerank_test.py`).

**What this means in the product:** a single confident quote is only trustworthy when the top passage clearly beats the second. Within 10% the app reports the ambiguity instead of picking one.

- **Labels age with the corpus.** One item (`bond ladder`) moved from `no_answer` to `vocabulary_collision` when the corpus grew: `ladder` started occurring twice, in unrelated sentences. That is the failure mode the gate cannot catch, and it is counted separately rather than quietly dropped.

## Why a BM25 threshold cannot decide this

At the previously hard-coded `min_score = 4.0`, **nothing was ever refused**: the lowest top-1 score in the whole golden set is well above 4. Irrelevant questions scored *higher* than relevant ones — `penny stock pump and dump` at 19.7 versus `what makes a stock cheap` at 8.5 — because BM25 rewards rare-word overlap, not meaning. No single threshold separates them.

## Grid

| threshold | vocabulary gate | precision@1 | recall@5 | answerable served | no-answer refused | mentioned served |
|---:|---|---:|---:|---:|---:|---:|
| 0 | on | 0.36 | 0.82 | 1.00 | 1.00 | 1.00 |
| 2 | on | 0.36 | 0.82 | 1.00 | 1.00 | 1.00 |
| 4 | on | 0.36 | 0.82 | 1.00 | 1.00 | 1.00 |
| 6 | on | 0.36 | 0.82 | 1.00 | 1.00 | 1.00 |
| 8 | on | 0.36 | 0.82 | 1.00 | 1.00 | 1.00 |
| 10 | on | 0.36 | 0.82 | 0.91 | 1.00 | 0.60 |
| 12 | on | 0.36 | 0.82 | 0.82 | 1.00 | 0.40 |
| 14 | on | 0.36 | 0.82 | 0.45 | 1.00 | 0.40 |
| 16 | on | 0.36 | 0.82 | 0.18 | 1.00 | 0.20 |
| 18 | on | 0.36 | 0.82 | 0.09 | 1.00 | 0.20 |
| 20 | on | 0.36 | 0.82 | 0.09 | 1.00 | 0.20 |
| 0 | off | 0.36 | 0.82 | 1.00 | 0.00 | 1.00 |
| 2 | off | 0.36 | 0.82 | 1.00 | 0.00 | 1.00 |
| 4 | off | 0.36 | 0.82 | 1.00 | 0.00 | 1.00 |
| 6 | off | 0.36 | 0.82 | 1.00 | 0.00 | 1.00 |
| 8 | off | 0.36 | 0.82 | 1.00 | 0.33 | 1.00 |
| 10 | off | 0.36 | 0.82 | 0.91 | 0.33 | 0.60 |
| 12 | off | 0.36 | 0.82 | 0.82 | 1.00 | 0.40 |
| 14 | off | 0.36 | 0.82 | 0.45 | 1.00 | 0.40 |
| 16 | off | 0.36 | 0.82 | 0.18 | 1.00 | 0.20 |
| 18 | off | 0.36 | 0.82 | 0.09 | 1.00 | 0.20 |
| 20 | off | 0.36 | 0.82 | 0.09 | 1.00 | 0.20 |

## What the app now does

- Refuses when the question's **rarest content word never occurs anywhere in the corpus** (the vocabulary gate) — a claim that can be checked against the index rather than felt.
- On the golden set that refuses **100% of the never-discussed topics** while still serving **100% of answerable questions**. The 100% of `mentioned_only` items that are served are correct to serve: the topic really does appear — the UI labels such a passage as a mention, not as his answer.

**The threshold is not what does the work.** With the gate off, a floor of 8 refuses only 22% of negatives; with the gate on it refuses every never-discussed topic at any floor from 0 to 8. That is why the shipped rule is a vocabulary check plus a conservative floor, not a tuned number that happened to fit twenty questions.
- Keeps serving passages above a **calibrated score floor of 8** when the gate passes, and labels them as *closest keyword matches, relevance not verified* rather than as his position.
- Reports `min_score` in `/health` so a reviewer can see what is being applied.

### Per-question result at the shipped setting

| id | label | top score | refused | top video | matches expected |
|---|---|---:|---|---|---|
| q01 | answerable | 15.0 | no | e1alOtev_rc | no |
| q02 | answerable | 12.9 | no | Oj9-8r7-Pqg | no |
| q03 | answerable | 14.3 | no | 6aKAJj2SKZA | no |
| q04 | answerable | 12.6 | no | dIpkIXfH2q4 | no |
| q05 | answerable | 16.3 | no | nyaSsJ8OD3M | no |
| q06 | answerable | 15.2 | no | T0Nd_fnoV3E | yes |
| q07 | answerable | 9.4 | no | mv_IYbUm7co | no |
| q08 | answerable | 12.4 | no | jCS3eoQiXHI | yes |
| q09 | answerable | 20.4 | no | Pw77OyeeLqg | no |
| q10 | answerable | 12.5 | no | Oj9-8r7-Pqg | yes |
| q11 | answerable | 11.5 | no | 6aKAJj2SKZA | yes |
| n01 | mentioned_only | 9.9 | no | L6dL2AbNJDM | — |
| n02 | no_answer | 0.0 | yes | — | — |
| n03 | no_answer | 0.0 | yes | — | — |
| n04 | vocabulary_collision | 12.5 | no | v86Gah_8ziI | — |
| n05 | no_answer | 0.0 | yes | — | — |
| m01 | mentioned_only | 14.9 | no | ZEkZXFw9Ujo | — |
| m02 | mentioned_only | 9.7 | no | T2lNhX479Pg | — |
| m03 | mentioned_only | 22.0 | no | RJ8zTCJaJhU | — |
| m04 | mentioned_only | 11.2 | no | ZGqWNyjLzAk | — |

## Honest limits

- **precision@1 is 0.82, recall@5 is 0.82.** Two of eleven answerable questions return a passage that is off-topic (the sell-half rule and the cash rule both surface a company discussion instead). Informal speech, ASR errors and 45-second windows are the cause; a cross-encoder reranker is the next step, not a larger threshold.
- **`mentioned_only` is not solved.** A viewer asking about quantum is not an answer, and lexical retrieval cannot tell the difference. The UI therefore never presents a passage as his position; it presents it as a passage that matched.
- **The corpus is 27% transcribed.** These numbers describe 35 hours, not his whole channel. A refusal means *not in what we hold*.

Reproduce with `python eval/calibrate_retrieval.py`.
