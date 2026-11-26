# Fine-tune plan — Model A (decision) + Model B (MM second opinion)

Status: build started. Hardware verified: RTX 5070 Ti, 16,303 MiB, driver 610.88 (Blackwell,
needs CUDA 12.8 wheels). Corpus: 45 videos, 794,271 words ≈ 1.06M tokens.

## What each model is

**A — decision model.** Takes a situation (ticker, price, bands, trend, position, risk budget) and
returns a structured decision: stance, conviction, size, invalidation, thesis, falsifiers. Trained
on investment theory and practice, not on Mark's transcripts. This is the layer being fine-tuned.

**B — MM second opinion.** Retrieval over his 45 videos. Reads A's card and responds with his
verbatim words, a timestamp, or an explicit "he never addressed this." Not a fine-tune — his
judgment is not in the data as decisions (see below), so it must not be simulated.

## The data finding that shapes everything

Strict extraction (ticker + first-person action in the same sentence) over 45 videos:

- loose matching: 119 "episodes" — most misattributed, e.g. "I am still richer than I was" tagged
  with a ticker named three sentences earlier
- strict matching: **6 clean episodes**, 5 tickers

Commentary is not a ledger. Six examples cannot train judgment. Therefore:

1. A is trained on theory + generated cases with derivable answers, not on "what Mark did".
2. B stays retrieval, so every "Mark would…" carries a citation.
3. "Thinks like Mark" requires his trade history (entries, exits, sizes, dates). Until that CSV
   exists, no model may be described as trained on his decisions.

## Training data for A

| Source | Use | Licence constraint |
|---|---|---|
| SEC EDGAR filings | real filings, risk factors, financial statements | US government work, public |
| Federal Reserve / BIS / IMF publications | rate, credit, macro mechanics | public |
| Damodaran course material + datasets | valuation, cost of capital | free, attribution required |
| CC-BY or public-domain finance texts | theory, accounting identities | permissive only; no NC/ND |
| Generated cases | sizing, stops, risk of ruin with derivable answers | produced by us |

Explicitly excluded: copyrighted books (Graham, Minervini), NC-licensed OER (LibreTexts),
anything scraped from paid courses.

Case generation rule: every case has an answer that can be computed (e.g. "risk 1% of 50k, entry
10.00, stop 9.00 → 50 shares"). The model is trained to produce the *reasoning card*, and the
numbers in it are checked against the deterministic engine. A case whose answer cannot be verified
does not enter the set.

## Training

- Base: Qwen3-8B-Instruct (Apache-2.0) — or Qwen2.5-7B-Instruct if Blackwell kernels misbehave.
- Method: QLoRA 4-bit, r=16, alpha=32, dropout 0.05, target all linear projections.
- Sequence 4,096; batch 1 with gradient accumulation 8; lr 1e-4 cosine; 2–3 epochs.
- Estimated 8–12 h unattended on the 5070 Ti; adapter output ~50–120 MB.
- Evaluation: (a) schema validity — every output parses into the card; (b) numeric discipline —
  no digits in prose, all figures match the engine; (c) sizing correctness on a held-out grid;
  (d) refusal behaviour when bands are missing; (e) blind human review of 30 cards.

No accuracy claim is made from training. Accuracy needs closed journal horizons.

## Serving (the constraint that decides the demo)

A fine-tuned 8B cannot run on Vercel. Options, in order of cost:

1. Hosted LoRA inference (Together / Fireworks / HF Endpoint) — ~$0.20–0.60/1M tokens, no idle GPU.
2. Serverless GPU (RunPod) — cheaper at rest, cold starts of 20–60 s.
3. Local GPU + tunnel — free, but the PC must stay on; rejected by the user for the demo.

The app calls it exactly like any OpenAI-compatible endpoint (`MMDESK_LLM_BASE` / `_MODEL`), so
switching between a hosted base model and the fine-tune is configuration, not code.

## Order of work

1. Theory corpus + case generator (needs one API key for labelling assistance only).
2. SFT set build + validation harness.
3. QLoRA run, unattended overnight.
4. Eval, then a blind A/B against the base model.
5. Wire A into `/api/analyze` as the "decision" layer; B answers it in the second-opinion panel.
6. Only then: hosted endpoint, and the demo switches from template prose to the trained model.

## Blocked / open

- **Trade history (Mark's CSV)** — required for any "trained on his decisions" claim. Not optional.
- **One API key** — for teacher labelling and interim prose.
- **Consent scope** — `docs/CONSENT.md` covers name, likeness, voice and content for the demo;
  it does not cover a trained voice or a model presented as his judgment. Written release needed
  before either ships publicly.
