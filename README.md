# Decision Desk

Decision support that answers an investment question **twice** — once through the rules a tenant
supplies, once through a conventional quantitative read — and then shows exactly where the two
disagree. It is not a trading bot and not a signal service.

The product is **multi-tenant and generic**: it ships with no corpus, no persona and no fine-tuned
model. The default tenant is the deterministic engine plus the standard lens, which is a complete
answer. A tenant becomes specific only when it uploads its own material and, optionally, trains a
second-layer adapter on its own framework.

## What it does

1. **Grounds the numbers.** Price, ATR, trend, volatility and the fundamental bands come from a
   provider with a timestamp. If the provider fails, the card is marked INCOMPLETE and stops — the
   app never estimates a price.
2. **Scores risk/reward on a documented rubric** (0–10), so position size follows the score rather
   than the strength of an opinion.
3. **Runs two lenses:** the tenant's rules (score, staged exits, alert posture, cash floor) and the
   standard lens (trend, momentum, ATR, valuation percentile).
4. **Reconciles them.** If one lens is bullish and the other is not, the card names the conflict and
   what evidence would settle it. That disagreement block is the product.
5. **Sizes the position deterministically** in `Decimal` — risk per trade, capped by conviction and
   cash, with open risk and fees subtracted first. Zero shares is a valid, explained answer.
6. **Quotes, never paraphrases.** The lens endpoint returns a contiguous verbatim run from one
   cited passage with its timestamp, or refuses. See `/api/lens`.
7. **Asks for a second opinion on demand.** `/api/opinion` sends the already-computed card to
   Layer 2 — the tenant's own adapter when one is active, otherwise a hosted model, otherwise a
   labelled template — and returns two readings plus where they disagree.
8. **Journals every card and grades it later** against fetched, split-adjusted bars, simulating gap
   fills at the stop.

## Quickstart

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe -m uvicorn mmdesk.service:app --port 8791
```

Open <http://127.0.0.1:8791/>. Without a model key the opinion panel renders a deterministic
template and says so. With `MMDESK_LLM_KEY` **and** `MMDESK_ALLOW_CLOUD_LLM=1` it uses the hosted
model; a key alone never enables egress.

## Tenants

Everything is scoped by a tenant id sent as `X-Tenant` (or `?tenant=`). Unknown ids are refused with
404, never defaulted.

```bash
curl -X POST localhost:8791/api/tenants -H 'Content-Type: application/json' \
  -d '{"id":"acme","name":"Acme Capital","lens_label":"Acme lens"}'

python scripts/ingest_corpus.py --tenant acme --src ./material --title "Acme method"
```

Then send `X-Tenant: acme`. The default tenant has no material, so `/api/lens` refuses with a
sentence that says exactly that instead of inventing a position.

Admin routes (`POST /api/tenants`, adapter registration and activation) are loopback-only unless
`MMDESK_ADMIN_TOKEN` is set, in which case `X-Admin-Token` is required.

## Layer 2 — a fine-tuned second-layer assistant, per tenant

The capability is the point: any tenant can go through the same pipeline, and an adapter is never a
prerequisite for using the product.

```bash
python scripts/build_sft_dataset.py --tenant acme --n 800 --batch 10
trainenv/Scripts/python.exe scripts/train_decision_lora.py --tenant acme --epochs 5 \
    --rank 32 --base Qwen/Qwen2.5-3B-Instruct --register
python scripts/promote_adapter.py --tenant acme      # refuses below 0.85 or on a leaky eval
trainenv/Scripts/python.exe scripts/serve_lens.py --port 8899
```

Design rules that are enforced, not aspirational:

- **Facts are retrieved; style is trained.** New material is an index update in seconds. Weights
  never carry facts that might need deleting.
- **The gate lives in the registry**, not in the training script. `promote_adapter.py` prints the
  candidate's numbers and refuses when agreement < 0.85 or the evaluation was not leakage-free.
- **A request names a tenant, never an adapter.** Only `status: active` adapters are routable, so a
  candidate cannot answer by accident.
- **The model never emits a digit.** Sizing, levels and prices are rendered from typed fields.

Full detail and the honest limits: `docs/LAYER2.md`, `docs/MODEL-CARD.md`.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q      # 121 passing
```

## Known limits

- Daily bars from a free provider, not real-time. Intraday mode is labelled indicative.
- No point-in-time fundamentals; a backtest of today's inputs against yesterday's prices is
  lookahead, and the app does not pretend otherwise.
- No closed journal horizons yet, so no performance claim is made anywhere.
- A 3B adapter cannot run on a serverless host; hosting Layer 2 needs a GPU endpoint.
