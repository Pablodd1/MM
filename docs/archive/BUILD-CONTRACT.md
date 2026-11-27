> **Archived 2026-11-27** — historical, written for an earlier build. Superseded by `README.md`,
> `docs/HANDOVER.md` and `docs/KNOWN-LIMITATIONS.md`. Its requirements table was the founding brief; the shipped product covers a
> different scope, and its consent section predates `docs/CONSENT.md`. It also
> quotes the owner's voice note verbatim, which was never meant for publication.

# MM Desk — build contract

What the user asked for, verbatim, and what that requires. No guessing beyond what is written
here; where something is undecided it is listed in §9 as an open question rather than assumed.

## 1. Verbatim requirement (voice note, 2026-11-24)

> "My idea is to present this to Mark to tell him, hey, I made this from your reels and
> information. This area is for you to give more knowledge to talk about. It will be fine-tuned
> MM or Money Mark. And then the idea is that this platform where you ask questions and the
> avatar of his face will answer with voice, human voice, and we can chat with it back and forth
> like a phone call. I'm going to be able to download data or tell you to go online and get data.
> I'll give you graphics. The idea is it's for decision making for investments and how to put the
> money. So this platform has to have very great knowledge about investments day-to-day, day
> trading. And I don't know about anything about this, so you have to prompt yourself and get the
> information online as an investor as cryptocurrency as money maker. We're going to make the UI
> of that. All the platform has to be ready, done, the documentation so I can ask another AI
> developer to review your work. So do not guess, do not assume. Let me know when you're done."

## 2. Decisions locked by that paragraph

| # | Requirement | Decision |
|---|---|---|
| R1 | Presentable to Mark as built from his reels | Every answer cites the transcript timestamp it comes from. The corpus is the ground truth for "what MM thinks"; the model must not invent a view he never expressed. |
| R2 | Fine-tuned MM | Layered: persona prompt + cited retrieval over the full corpus + his published rules as code. A literal weight fine-tune is **optional phase 2**, gated on corpus volume (§9). |
| R3 | Ask investment questions | Text chat + question intake with ticker, horizon, account size, risk per trade. |
| R4 | His face answers with a human voice | Talking-head avatar (photo → animated lips) + licensed TTS. Identity of the face is a consent question, not a technical one (§9). |
| R5 | Back-and-forth like a phone call | Full-duplex voice loop: VAD → STT → LLM → TTS, with barge-in (user can interrupt). Target < 1.2 s to first audio. |
| R6 | Download data / "go online and get data" | Tool layer with market data, fundamentals, news and crypto providers; every fetched number is timestamped and shown in the card. CSV/JSON export of any answer. |
| R7 | "I'll give you graphics" | Chart image upload → vision parse to strict JSON → verification pass → evidence regions highlighted on the image. |
| R8 | Decision making for investments, "how to put the money" | Position sizing, risk per trade, invalidation levels, allocation across ideas — deterministic math, not model judgement. |
| R9 | Strong day-to-day and **day-trading** knowledge | Two distinct modes: Swing/Invest and Intraday. Intraday adds pre-market levels, VWAP/ATR, session risk caps, and refuses to size beyond the day's risk budget. Educational framing only (see §7). |
| R10 | User is not an investor and cannot verify claims | The app must be self-documenting: every number traceable to its source, every rule traced to a timestamp, every uncertainty stated. |
| R11 | Full platform + UI, documentation, reviewable by another AI developer | Shipped repo with architecture doc, API reference, data contracts, test suite, run instructions, known-limitations doc. |
| R12 | Do not guess, do not assume | Missing input → the app asks. Unreadable chart → "not legible". No price without a fetch. No MM view without a citation. §9 lists what we still need from the user rather than filling it in. |

## 3. System architecture

```
Browser (UI) ── WebSocket (audio + text) ── FastAPI gateway
                                              │
              ┌───────────────────────────────┼────────────────────────────┐
              ▼                               ▼                            ▼
     Voice loop                     Decision engine                  Corpus / RAG
  VAD (silero)                  rule_engine (deterministic)        BM25 + embeddings
  STT (whisper/faster)          data_grounder (providers)          timestamps cited
  LLM (reasoning)               chart_parser + verifier            ingestion scripts
  TTS (Kokoro local)            reconciler (MM vs standard)
  Avatar (lipsync)              position_sizer
```

### Components and their contracts

| Component | Input | Output | Failure behaviour |
|---|---|---|---|
| `stt` | 16 kHz mono PCM | text + confidence | low confidence → ask user to repeat |
| `llm` | question + retrieved citations + grounded numbers | JSON decision card | non-JSON → reject and retry once, then show raw with warning |
| `corpus` | query | passages + `video_id` + `start_s` | no citation above threshold → say "he has not addressed this" |
| `data_grounder` | ticker, horizon | prices, ATR, ranges, multiples, news, timestamp | provider down → card marked INCOMPLETE, no estimates |
| `chart_parser` | image | strict JSON with axis ranges + pixel regions | unreadable → refuse |
| `chart_verifier` | image + parse | agree/disagree per field | disagree on axes → drop the visual read |
| `rule_engine` | parsed inputs + numbers | score, size, level, time stop | missing input → return NEEDS_INPUT |
| `reconciler` | both lenses | disagreement + tie-break evidence | disagreement unresolvable → say so |
| `tts` | text | audio | long text → sentence chunking |
| `avatar` | audio + face asset | video frames | falls back to voice-only |

## 4. The decision card (API contract)

```json
{
  "id": "card_...",
  "created_at": "ISO-8601",
  "question": "...",
  "instrument": {"ticker": "NVDA", "venue": "NASDAQ", "asset_class": "equity"},
  "mode": "swing | intraday",
  "grounding": {
    "as_of": "ISO-8601",
    "sources": [{"provider": "yfinance", "field": "close", "value": 0.0, "ts": "..."}],
    "missing": ["earnings_date"]
  },
  "chart": {"used": true, "legible": true, "timeframe": "1d", "levels": [{"price": 0.0, "kind": "support", "pixels": [x1,y1,x2,y2]}]},
  "mm_lens": {
    "score_10": 7,
    "components": {"support_proximity": 3, "upside_to_resistance": 2, "trend_quality": 1, "event_risk": 1},
    "cited_rules": [{"rule": "risk_reward_10scale", "video_id": "9PhWx9rzIaU", "start_s": 1694}],
    "action": "add | hold | trim | pass",
    "position_pct": 0.03
  },
  "standard_lens": {"valuation_pct": 0.0, "trend": "...", "momentum": "...", "volatility": "...", "action": "..."},
  "disagreement": {"exists": true, "summary": "...", "tie_break": "..."},
  "risk": {"invalidation": 0.0, "first_target": 0.0, "time_stop_days": 0, "risk_per_trade_pct": 1.0, "shares": 0},
  "confidence": {"level": "low|medium|high", "basis": "...", "calibration_note": "..."},
  "falsifiers": ["..."],
  "disclaimer": "Educational, not advice. ..."
}
```

## 5. Rules as code (from the transcript, not interpreted)

| Rule | Implementation | Citation |
|---|---|---|
| R/R score 1–10 | Rubric table in `rules/risk_reward.md`, scored per component, sum = score | 01:44, 27:51 |
| Size by score | `pct = clamp(score/10 * max_pct, 0, max_pct)` where `max_pct` comes from user risk settings | 28:16 |
| Three-point rule | if `score_now <= score_entry - 3` → suggest trim | 28:57 |
| Sell half at 2× | if `price >= 2 * entry` → suggest selling 50% | 29:59 |
| Yellow alert | `no speculative positions; require score >= 8` (the trigger itself is user-set, not invented) | 41:49 |
| Orange alert | `net equity exposure <= favourites only; hedge 1:1 with inverse benchmark` | 42:02 |
| Cash floor | configurable minimum cash %, default shown, not assumed | 16:54 |

The alert **trigger** is the one place he gives no definition. The app therefore lets the user
define the trigger (index valuation percentile, drawdown from high, breadth, or manual) and shows
which trigger produced the posture. We do not invent his threshold.

## 6. Local-first, with cloud only where it earns its place

- **Voice loop local:** whisper.cpp for STT, Kokoro (Apache-2.0) for TTS — already installed and
  proven on this machine. No audio leaves the box by default.
- **Reasoning: configurable** — local Qwen3-VL for draft, cloud for final read (see model table).
- **Market data:** free tier (yfinance) as default, keyed providers optional.
- **No account, no telemetry, no analytics by default.**

## 7. Compliance posture (this is a product for the public, so it matters)

- Educational/informational framing, not personalised investment advice. If this is ever sold,
  the person giving advice needs to be a registered investment adviser in the relevant
  jurisdiction; the app must not present itself as one.
- **Mark's identity:** his face, voice and name are his. Publishing a face+voice avatar requires
  his written consent before any public release. Until then: private demo only, labelled
  "prototype built from public content".
- **His disclaimers travel with the output** — he says he may enter, exit or reverse without
  notice; the app says the same about its own output.
- No performance claims without a published track record of the app's own calls.

## 8. Build phases

1. **P0 — corpus + evidence engine**: full transcript ingestion (130.7 h), search with timestamps,
   "what has he said about X" answered only from the corpus. Testable without AI.
2. **P1 — data grounder + rule engine + card (text)**: numbers, sizing, invalidation, journal.
3. **P2 — chart parse + verifier** with pixel evidence.
4. **P3 — voice loop** (STT → LLM → TTS, barge-in) in the browser.
5. **P4 — avatar** lipsync, lip-sync quality gate.
6. **P5 — UI polish, documentation, review pack, test suite.**

## 9. Open questions — I need these, I will not assume them

1. **Is Mark a partner?** If yes, his face and voice are licensed and the app can be branded MM.
   If not yet, P4 ships with a generic analyst avatar until consent exists.
2. **Whose account data?** Local single-user only, or multi-user with logins?
3. **Does "day trading" mean real-time intraday data?** That needs a paid feed (Polygon, Alpaca,
   Databento) — free data is delayed and cannot power intraday decisions.
4. **Crypto scope** — spot only, or futures/perps? Different risk rules entirely.
5. **Which LLM key do we have?** An Anthropic key (Opus 5.5) or a Google key (Gemini 3.8 Flash)?
6. **Delivery target** — desktop install on this PC, or a web app for multiple people?
