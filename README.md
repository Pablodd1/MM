# MM Desk

Decision support that answers an investment question **twice** — once through the rules
Money Mark teaches publicly, once through a conventional quantitative read — and then shows
you exactly where the two disagree.

It is not a trading bot and not a signal service. Built with Mark Gomes's permission; see `docs/CONSENT.md`.

## What it actually does

1. **Grounds the numbers.** Price, ATR, trend, volatility, valuation multiples come from a
   provider with a timestamp. If a provider fails, the card marks the data incomplete and
   stops — the app never estimates a price.
2. **Scores risk/reward on a documented rubric** (0–10), so position size follows the score.
   The rubric is an approximation of his chart judgement, labelled as such in every card.
3. **Runs both lenses:** his rules (score, three-point rule, sell-half-at-2×, alert posture,
   cash floor) and the standard lens (trend, momentum, ATR, valuation percentile).
4. **Reconciles them.** If one lens is bullish and the other is not, the card names the
   conflict and what evidence would settle it.
5. **Sizes the position deterministically** — risk per trade, capped by conviction and by a
   cash floor, never by the language model.
6. **Cites the corpus.** Any claim about what he thinks carries a video id and timestamp.
   If the corpus has no passage, the card says so instead of inventing a view.
7. **Journals every call** and grades it at 30/90/180/365 days, counting the losers.

## Quick start

Linux / macOS:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn mmdesk.service:app --port 8791      # open http://127.0.0.1:8791
pytest -q
```

Windows (PowerShell or cmd):

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python.exe -m uvicorn mmdesk.service:app --port 8791
.venv\Scripts\python.exe -m pytest -q
```

Optional model-written narrative (without it a deterministic template is used and says so):

```bash
export DEEPSEEK_API_KEY=...      # Windows: set DEEPSEEK_API_KEY=...
```

Persona (verbatim quotes, never paraphrase): `POST /api/persona` — see `docs/PERSONA-FIDELITY.md`.
Synthetic crowd (six styles, labelled not-a-forecast): `POST /api/scenario` — see `docs/SCENARIOS.md`.

Voice (push-to-talk, local): see `docs/VOICE.md` — `uv pip install -r requirements-voice.txt`.

Deployment: GitHub is pushed; Vercel needs one authenticated command — see `docs/DEPLOY.md`.

The app runs with no API key: numbers, scoring, sizing, reconciliation and citations all still
work. Only the prose changes — and it says which layer wrote it.

## Corpus

```bash
python batch_transcribe.py          # resumable; GPU via CUDA DLLs, CPU fallback
python build_index.py               # time-windowed passages with citations
python retrieval.py "position sizing"
```

`health` reports how many passages are indexed. Zero passages is a valid state: the app then
answers from his general rules and says the corpus is silent on the specific question.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | single-page UI |
| GET | `/health` | corpus size, model/vision configuration, intraday support |
| POST | `/api/analyze` | the decision card |
| GET | `/api/journal` | stored calls + aggregate outcomes |
| POST | `/api/journal/score` | grade entries against supplied price series |
| POST | `/api/chart` | chart image → 503 until a vision provider is configured |

## Design invariants

- No number in a card originates from a language model.
- Position sizing, stops and targets are computed in tested functions.
- A persona claim requires a corpus citation; absence is reported, not filled.
- Every card carries a falsifier and the same plain disclaimer.
- Nothing is claimed about Mark's current positions — the app cannot know them.

## Status

Version 0.1.0. Deterministic core complete and tested; corpus engine working; service running.
Chart parsing and the voice/avatar loop are not implemented yet — see `docs/ROADMAP.md`.

Educational tool, not investment advice.
