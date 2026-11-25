# Roadmap

Status as of 2026-11-24: **core, voice, persona, scenarios and journal grading built and tested
(101 tests). Corpus transcription resumed — 19 of 82 videos indexed. Deployment prepared, blocked
on one Vercel login.**

| Phase | Scope | State | Gate before moving on |
|---|---|---|---|
| P0 | Corpus: transcribe 82 videos, index passages with timestamps | **in progress — 19/82** | A question he never addresses must return zero citations, not the nearest-sounding passage. |
| P1 | Deterministic core: rules, rubric, lenses, card, journal, API, UI | **done** | Position size never exceeds the stated risk at the stop, in any input combination. |
| P2 | Chart input | **partial, by design** — typed levels accepted, pixels never read | Axis values verified by a second pass; disagreement drops the visual read. |
| P3 | Voice: STT → deterministic reply → TTS | **done as push-to-talk** (7.5 s round trip); no barge-in | Under ~1.2 s to first audio before calling it conversational. |
| P4 | Avatar | **unblocked, not built** — consent granted 2026-11-24 | Written terms, plus recordings from Mark if the voice is to be cloned. |
| P5 | Calibration: 50–100 historical setups, published Brier score | **not built** — journal has no elapsed horizons | Publish the score even when it is bad. |
| P6 | Persona fidelity | **done and measured** — verbatim quotes, 5/5 refusals | Question coverage above 0.8 before claiming it "knows what he thinks". |
| P7 | Synthetic crowd | **done** — six styles, labelled not-a-forecast | No persona count increases without a scoring rule attached. |
| P8 | Deployment | **prepared** — `api/index.py`, `vercel.json`, serverless simulation verified | One authenticated `vercel` command. |

## P0 — corpus

Transcription is GPU-batched (`large-v3`, ~97× realtime). YouTube's bot check blocked 65 videos
for a day; it has lifted and the batch is running again. Coverage is reported honestly in
`/health` as **transcribed**, never as the catalogue size.

## P2 — why pixels are refused

Financial-chart benchmarks (ACL 2026, 26 models) found vision models weak on spatial reasoning
and unreliable as evaluators. Reading a price axis from an image is exactly where a plausible
number gets invented. So `/api/chart` takes the image as a *reference* and requires typed levels;
the response says `numbers_from: typed levels only`.

## P3 — voice, honestly scoped

Push-to-talk works: Whisper `large-v3` on GPU (3.1 s), deterministic reply, Kokoro TTS (2.2 s).
Missing is full duplex — VAD, echo cancellation, barge-in, streaming. That is a separate
real-time project and is not promised.

## P4 — avatar

Consent exists (`docs/CONSENT.md`). Nothing is built. A voice clone needs recordings from Mark and
an explicit agreement about who owns the model; until then Kokoro's licensed voices are used.

## P5 — calibration, the one that decides whether this is real

- 50–100 dated setups from the corpus with known forward outcomes.
- Hit rate, excess return vs SPY, worst decile, Brier score of stated confidences.
- Published in the README including the quarters where it underperforms.

Until then: the journal grades every call, and no accuracy claim is made.

## P6 — persona fidelity

`eval/persona_fidelity.py` measures whether "Mark says…" is really his: 11/11 answerable served,
100% verbatim, 100% of numbers from the transcript, 5/5 never-discussed topics refused. Question
coverage is 0.68 — the persona quotes faithfully but does not always answer the question. That
number is the work item.

## Explicitly out of scope

- Auto-trading or broker connectivity.
- Personalised advice presented as professional advice.
- Any claim about Mark's current positions.
- Multi-user accounts until single-user behaviour is calibrated.
