# Persona fidelity

Does “Mark says…” actually say what he said?

- Golden set: **20 questions** (11 answerable, 3 never discussed, 5 mentioned only).
- A quote is scored by how much of its text appears **verbatim** in the cited transcript. 1.0 means nothing was added, reordered or paraphrased.

## Results

- **Verbatim rate: 11/11 served quotes are 100% his words** (100%).
- **Number fidelity: 11/11 quotes contain only numbers present in the transcript.**
- **Refusal on never-discussed topics: 3/3.**
- **Mentioned-only topics served: 5/5** — correct, and labelled as a mention rather than as his view.

- **Question coverage: mean 0.80** of the asked terms appear in the quote; 5/11 quotes cover every term. This is the honest measure of whether the quote answers the question — verbatim rate only proves it is really his sentence.

## Per question

| id | label | refused | verbatim | numbers ok | source |
|---|---|---|---:|---|---|
| q01 | answerable | no | 1.00 | yes | e1alOtev_rc @ 01:11:13 |
| q02 | answerable | no | 1.00 | yes | Oj9-8r7-Pqg @ 02:21:07 |
| q03 | answerable | no | 1.00 | yes | 6aKAJj2SKZA @ 01:52:02 |
| q04 | answerable | no | 1.00 | yes | dIpkIXfH2q4 @ 00:02:52 |
| q05 | answerable | no | 1.00 | yes | nyaSsJ8OD3M @ 01:19:02 |
| q06 | answerable | no | 1.00 | yes | T0Nd_fnoV3E @ 01:02:33 |
| q07 | answerable | no | 1.00 | yes | mv_IYbUm7co @ 01:16:43 |
| q08 | answerable | no | 1.00 | yes | jCS3eoQiXHI @ 00:11:40 |
| q09 | answerable | no | 1.00 | yes | Pw77OyeeLqg @ 00:16:19 |
| q10 | answerable | no | 1.00 | yes | Oj9-8r7-Pqg @ 02:13:41 |
| q11 | answerable | no | 1.00 | yes | 6aKAJj2SKZA @ 01:42:59 |
| n01 | mentioned_only | no | 1.00 | yes | L6dL2AbNJDM @ 00:26:20 |
| n02 | no_answer | yes | 1.00 | yes | — |
| n03 | no_answer | yes | 1.00 | yes | — |
| n04 | vocabulary_collision | no | 1.00 | yes | v86Gah_8ziI @ 00:02:16 |
| n05 | no_answer | yes | 1.00 | yes | — |
| m01 | mentioned_only | no | 1.00 | yes | ZEkZXFw9Ujo @ 01:09:27 |
| m02 | mentioned_only | no | 1.00 | yes | T2lNhX479Pg @ 01:13:22 |
| m03 | mentioned_only | no | 1.00 | yes | RJ8zTCJaJhU @ 00:15:35 |
| m04 | mentioned_only | no | 1.00 | yes | ZGqWNyjLzAk @ 00:52:13 |

## Quotes as returned

- **q01** [e1alOtev_rc @ 01:11:13]: “That's the buy and hold technique. You can use a 10 point, a three point rule.”
- **q02** [Oj9-8r7-Pqg @ 02:21:07]: “I don't like what they're doing. I think they should take half of their energy assets and sell it to somebody today and use that to fund building out the other half of their energy assets because they're only using 4% of”
- **q03** [6aKAJj2SKZA @ 01:52:02]: “Talk Live, tickers, risk reward, position sizing, NYSE, NASDAQ. They don't see much reason to hold cash.”
- **q04** [dIpkIXfH2q4 @ 00:02:52]: “Well. Talk Live, tickers, risk reward, position sizing, NASDAQ.”
- **q05** [nyaSsJ8OD3M @ 01:19:02]: “RWM is just your hedge, your protection. So if you have...”
- **q06** [T0Nd_fnoV3E @ 01:02:33]: “And your possible reward is $6 of upside. You have a 10 to 1 risk reward ratio in the scenario I just gave you.”

## What this does and does not prove

- It proves the persona **does not invent his words**: the quote is a substring of the
  transcript, and every number in it is his, not ours. Note that this is structural — the
  quote is *selected*, not generated — so 100% verbatim is a regression guard, not a result
  to boast about. The number that carries information is question coverage.
- It does **not** prove the quote answers the question. Extraction can pick a sentence that
  mentions the topic without addressing it — `missing_terms` records which words of the
  question the quote never covers, and that is shown rather than hidden.
- It says nothing about whether following his rules makes money. That needs the journal's
  horizons to close.

Reproduce with `python eval/persona_fidelity.py`.
