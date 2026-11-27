> **Archived 2026-11-27** — historical, written for an earlier build. Superseded by `README.md`,
> `docs/HANDOVER.md` and `docs/KNOWN-LIMITATIONS.md`. Its model benchmark and pricing figures were never verified, its build order was
> not the one followed, and it plans a single-tenant product.

# MM Desk — product plan

*A research assistant that answers "what would Money Mark do?" next to a conventional
institutional read, and shows you where the two disagree.*

Status: plan, not code. Written 2026-11-24 against the transcribed corpus
(`9PhWx9rzIaU`, 61 min) and current model data (Nov 2026).

---

## 1. What I actually understand from his material

Five rules, and they are not equally strong.

| Rule | His words | My judgement |
|---|---|---|
| Risk/reward score 1–10 drives position size | "the cheat code to what I do" (01:44) | **Real, but subjective.** It is a discipline for refusing bad odds, not a measurement. |
| Three-point rule | 10→7, take profits (28:57) | **Testable heuristic.** Depends entirely on the 1–10 score being reproducible. |
| Sell half when you double | "guaranteed yourself a win" (29:59) | **Sound.** It is a de-risking rule, not a return-maximising one. |
| Alert levels (green/yellow/orange/red) | yellow = indices "too expensive" (14:07) | **Weakest link.** No metric, no threshold. Not reproducible by anyone else. |
| Cash is a position | ">50% in money market at 4%" (16:54) | **Real and underrated.** Most retail never does it. |
| Hedge with RWM in orange | 1:1 against equity exposure (42:15) | **Worked once, in a fast crash.** Inverse ETFs decay; no time component given. |

**My position:** the durable part of his method is *risk-first position sizing plus a cash
reserve*. The fragile part is the alert timing and the chart-reading that produces the score.
Any app that claims to reproduce his alert levels is selling a feeling as a signal. An app that
forces you to write down your risk/reward rationale, size the position accordingly, and take
half off at 2× — that is genuinely useful, and it is what we should build.

**What I refuse to build:** an auto-trader, a "buy/sell" alert service, or anything that
pretends to be Mark Gomes. He is a real person with a disclaimer that he may reverse without
notice; a product wearing his name and not his positions is a liability, not a feature.

---

## 2. The product

**Working name: MM Desk** — "the risk/reward desk". Branded as *a lens modelled on Mark
Gomes' published method*, never as him.

### Inputs
1. A question in plain language: "Is NVDA a buy here?", "Should I trim this?"
2. Optionally a chart image — screenshot from any platform, any timeframe.
3. Optional context: account size, risk per trade, holding horizon.

### Output — one decision card
```
VERDICT        Hold / trim / add / pass — with the reason in one sentence
MM LENS        1–10 risk/reward score + which components produced it
STANDARD LENS  valuation, trend, momentum, volatility, drawdown, benchmark
DISAGREEMENT   where the two lenses split, and which evidence decides it
LEVELS         invalidation price, first target, time stop
SIZE           position size from account risk, not from conviction
CONFIDENCE     calibrated, with the sample it is based on
FALSIFIERS     what would prove this read wrong
JOURNAL ENTRY  logged, scored later against what actually happened
```

The disagreement block is the product. Anyone can get a bullish AI opinion; knowing that the
MM lens says "9/10, cheap" while the standard lens says "overvalued on 3 of 4 metrics" is what
makes a person think.

### Pipeline
1. **Ingest** — question, chart image, ticker, horizon.
2. **Ground the numbers (tools, never the model)** — price, 52-week range, ATR, volume,
   moving averages, earnings date, valuation multiples, sector performance, all fetched and
   timestamped. If a number cannot be fetched, the card says so and stops; it is not estimated.
3. **Chart parse (vision)** — extract only what is visible: instrument, timeframe, axis ranges,
   marked levels, trend structure, pattern, indicator state. Output strict JSON with the pixel
   region cited for each claim. Unreadable → "unreadable", never guessed.
4. **Chart verification** — a second pass (different model or deterministic pixel checks)
   confirms axis values and level positions. FinChart-Bench (ACL 2026, 26 models) found LVLMs
   are unreliable on financial charts and weak at spatial reasoning, so this stage is mandatory,
   not optional.
5. **MM rule engine** — deterministic code, not an LLM: applies the 1–10 rubric, the three-point
   rule, the sell-half rule, and the cash/hedge posture. The LLM explains the result; it does
   not compute it.
6. **Standard engine** — deterministic quant (valuation percentile, trend score, ATR-based
   stops, position sizing by fixed fractional risk) + a written rationale.
7. **Reconciler** — compares the two, names the conflict, and states which evidence would break
   the tie.
8. **Journal** — every card stored with its inputs; scored at +1/+3/+6/+12 months automatically.

### Non-negotiables
- Every price and ratio comes from a tool call with a timestamp. The model never states a
  number it was not given.
- Every chart claim cites a region of the image, or is labelled "not legible".
- No output without a falsifier and an invalidation level.
- Nothing is called "advice". Every card carries the same one-line risk note.
- The app never claims to know Mark's current positions, because it cannot.

---

## 3. Model choice — November 2026

Independent data, not vendor claims:

- **Surge AI Chartography** (100 professional chart types, Nov 23 2026): GPT-6.2 Astra **90.7%**,
  Claude Opus 5.5 **89.6%**, GPT-6 Astra 88.6%, GPT-6.2 Sol 87.7%, Gemini 4 Hydrogen 87.2%,
  Gemini 4 Carbon 85.5%, **Gemini 3.8 Flash 81.7%**.
- **BenchLM multimodal** (Nov 23 2026): GPT-6.2 Astra 90.3, Claude Fable 5.2 87.3,
  Gemini 4 Carbon 85.8, Claude Opus 5.5 80.6, Gemini 3.8 Flash 66.7.
- **Price per 1M tokens**: Opus 5.5 $4/$20 · GPT-6 Sol $2/$10 · Gemini 3.8 Flash $0.75/$3.75
  (promo to 31 Dec 2026, then $1.50/$7.50) · GPT-6 Luna $0.10/$0.50.
- **Local, 16 GB VRAM**: Qwen3-VL 8B Q4_K_M = 4.68 GiB weights + 1.08 GiB projector; KV cache
  144 KiB/token, so ~7 GiB total at 8K context. It fits comfortably. A 1080p screenshot costs
  ~2,025 visual tokens.

### Recommendation

| Role | Model | Why |
|---|---|---|
| **Chart extraction (best)** | **GPT-6.2 Astra** | Highest independent chart score (90.7%). Use max reasoning. |
| **Chart extraction (value)** | **Claude Opus 5.5** | 89.6% — within 1.1 points of Astra at a quarter of the blended price ($4/$20 vs ~$16 blended). **This is what I would ship.** |
| **Budget / high volume** | **Gemini 4 Carbon** ($4 blended) or **Gemini 3.8 Flash** ($1.50 blended) | 85.5% / 81.7% chart accuracy. Flash is the one already wired into Artemis here. |
| **Reasoning, reconciliation, writing** | **Claude Opus 5.5** | Best instruction-following of the group for strict JSON + rule citation; 1M context. |
| **Local / private draft** | **Qwen3-VL 8B** (Ollama) | Fits 16 GB; no cloud, no PHI-style exposure; label it "local draft, unverified". |

**Ship this:** Opus 5.5 for chart parse **and** reasoning, Gemini 3.8 Flash as the cheap second
opinion that must agree on axis values, Qwen3-VL 8B for offline mode. Expected cost **≈$0.08–0.12
per full card**; 100 cards a month ≈ $10.

Why not Astra everywhere: it is the most accurate on charts but roughly 4× the price of Opus at
comparable accuracy, and the verification pass matters more than the last 1.1 points.

Why not local-only: FinChart-Bench found LVLMs — open and closed — still misread financial
charts, fail instruction following, and are "not reliable enough to serve as automated
evaluators". Local stays for privacy, not for the final read.

---

## 4. Build order

1. **Rule engine + journal** (no AI) — deterministic MM rubric, position sizing, journal with
   automatic scoring. This alone is useful and cannot hallucinate.
2. **Data grounder** — price/fundamentals/ATR/valuation via yfinance, cached, timestamped.
3. **Chart parse + verification** — Opus 5.5 → JSON → Flash cross-check → disagreement flagged.
4. **Standard analysis** — valuation percentile, trend score, fixed-fractional sizing.
5. **Card UI** — one page, four blocks, no charts until the numbers are verified.
6. **Calibration** — 50–100 historical setups with known outcomes; measure Brier score and
   rule-adherence. Publish the score, including when it is bad.

## 5. What would make me stop

- If the 1–10 rubric cannot be made reproducible across two raters (κ < 0.6), the MM lens is
  decoration and should be dropped in favour of the plain risk-first card.
- If the chart-verification pass disagrees with the first parse more than 10% of the time, the
  vision input is not trustworthy yet and the app should accept only typed tickers.
- If the journal shows no improvement in the user's decision quality after 30 cards, the product
  is entertainment and should be priced like it.
