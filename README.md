# Decision Desk

Decision support for a single investment question. It grounds the numbers from a market-data
provider, scores the setup, sizes the position with deterministic arithmetic, shows where two
lenses disagree, records every call in a journal that grades itself later, and offers a
labelled second opinion on demand.

It is not a trading bot, not real-time, and not investment advice. It never estimates a price:
if the provider fails, the card stops and says what was missing.

## Quickstart

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python.exe -m uvicorn mmdesk.service:app --port 8791
```

Open <http://127.0.0.1:8791/>. Without a model key the page still works: the narrative and the
second opinion are deterministic templates that say no model wrote them.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q        # 304 passed, 2 skipped
```

The two skips are calibration tests that need `corpus/passages.jsonl`. That file is tenant
material and is deliberately not committed; the rest of the suite runs against a small
synthetic corpus that ships with the repo, so a fresh clone is green. `ruff check .` is clean
and runs in CI (`.github/workflows/ci.yml`).

## How a question becomes a card

1. **Ground.** Daily bars and fundamentals come from yfinance with a timestamp. The date of
   the bar whose close was used is recorded, so the journal can anchor on it later. Nothing is
   estimated from memory.
2. **Score.** Either you supply two valuation lines (green = cheap, red = expensive) and the
   score interpolates between them — the mechanic the source material actually describes — or
   a documented technical proxy scores support, reward/risk, trend and earnings risk. The card
   names which produced the number.
3. **Size.** `Decimal` arithmetic, end to end: shares = min(risk budget, score cap, stated
   cash), with fees, slippage and open risk subtracted first. Zero shares is a valid, explained
   answer that names the cap that bound. A property test asserts risk at the stop never exceeds
   the budget across a grid of inputs.
4. **Reconcile.** The lens verdict and a conventional read (trend, momentum, volatility) are
   compared, and the disagreement block — the point of the product — names the conflict and
   what evidence would settle it. All twelve lens/standard pairings are covered.
5. **Narrate.** A hosted model may write the prose, or a tenant's own fine-tuned adapter, or a
   deterministic template. Each is labelled. Models are barred from writing a single digit;
   any numeral retries once, then the template takes over.
6. **Journal.** Every card is stored. `/api/journal/grade` later fetches split-adjusted bars
   itself, rescales stored levels if the series was adjusted since the call, simulates gap and
   intrabar stops, and grades horizons only once they have closed. A horizon is never frozen
   from a partial window.
7. **Second opinion.** `/api/opinion` re-reads the card and answers in two voices. It does not
   journal: asking twice must not count the question twice in the track record.

### Semantic reranker (optional, measured)

`MMDESK_RERANKER=1` (with `pip install -r requirements-rerank.txt`) adds an open-source
cross-encoder — fastembed's ONNX MiniLM over MS MARCO, Apache-2.0 — that re-scores the
retrieval candidate pool and serves only passages it judges to actually answer the question.
The first question downloads the model once.

**It is off by default because it was measured, and measurement said not yet.** On clean text
it separates sharply (the true sell-half rule scored +6.2 against −2.0 and −11.1 for decoys),
but over this corpus's golden set, calibrated BM25 holds hit@1 4/11 with hit@5 9/11 while no
rerank variant dominates (the best trades hit@1 5/11 for hit@5 6/11). `/health` reports which
path is active. `MMDESK_RERANKER_MODEL` overrides the model id; nothing else is needed, and
serverless hosts are unaffected because the dependency is deliberately not in
`requirements.txt`.

## Endpoints

| Route | Method | Notes |
|---|---|---|
| `/` | GET | the single-page UI |
| `/health` | GET | version, persistence, voice availability (no tenant roster, no paths) |
| `/api/analyze` | POST | the card |
| `/api/opinion` | POST | second opinion; does not journal |
| `/api/lens` | POST | verbatim quote from the tenant's material, or a refusal |
| `/api/scenario` | POST | six rule-based styles; labelled "not a forecast" |
| `/api/crowd/talk` | POST | influence between the styles; no language model |
| `/api/journal` | GET | this scope's entries and summary |
| `/api/journal/grade` | POST | grade from fetched bars; the only writer |
| `/api/journal/score` | POST | what a supplied series *would* grade; never writes |
| `/api/chart` | POST | image accepted as a reference only; typed levels required |
| `/api/voice`, `/api/voice/selftest` | POST | local speech loop; 501 where the models are absent |
| `/api/tenants` | GET/POST | roster is admin-only; creation is admin/loopback only |
| `/api/tenants/{id}`, `/api/tenants/{id}/adapter[...]` | | public facts per known id; adapter registration and activation are admin-gated |

## Tenancy

A tenant is named by the `X-Tenant` header or `?tenant=`. Unknown ids 404 and are never
defaulted. **A tenant id is an address, not a credential** — anyone who knows it can analyse
under it. What is protected:

- **Journals.** They name positions and, through the sizing, the account behind them. Readable
  from loopback, with `MMDESK_ADMIN_TOKEN`, or under an `X-Journal-Id` scope; anyone else
  works under their own scope and sees only their own calls. The web UI does this
  automatically with an identifier kept in the browser.
- **Admin routes.** Tenant creation and adapter registration/activation are loopback-only
  unless `MMDESK_ADMIN_TOKEN` is set (then `X-Admin-Token`, constant-time compared). On a
  serverless host loopback admin is refused outright, because the peer address there belongs
  to the platform, not the caller.
- **Adapter gate.** A trained adapter is stored as a candidate and becomes routable only
  through activation, which requires evaluation agreement ≥ 0.85 and a leakage-free run.
  Registration cannot store an active adapter, whatever the payload claims.

The web UI addresses the default tenant unless you put `?tenant=<id>` in the URL.

## Layer 2 — a tenant's own adapter

```bash
python scripts/build_sft_dataset.py --tenant acme --n 800 --batch 10
trainenv/Scripts/python.exe scripts/train_decision_lora.py --tenant acme --epochs 5 \
    --rank 32 --base Qwen/Qwen2.5-3B-Instruct --register
python scripts/promote_adapter.py --tenant acme       # refuses below the gate
trainenv/Scripts/python.exe scripts/serve_lens.py --port 8899
```

The training target is labelled by the shipped engine, so the adapter learns the app's own
policy; the training-set builder refuses to write any target containing a numeral. See
`docs/LAYER2.md` and `docs/MODEL-CARD.md`. The GPU paths were reviewed and compile; the
routing is tested with stand-ins, but the server itself has not been run end to end on this
machine.

## Configuration

| Variable | Meaning |
|---|---|
| `MMDESK_LLM_KEY` | hosted model key; the vendor is inferred from the key prefix |
| `MMDESK_LLM_BASE`, `MMDESK_LLM_MODEL` | explicit endpoint, overriding the inference |
| `MMDESK_ALLOW_CLOUD_LLM=1` | explicit opt-in before anything is sent to a hosted provider |
| `MMDESK_ADMIN_TOKEN` | enables remote admin with `X-Admin-Token` |
| `MMDESK_DATA_DIR`, `MMDESK_DB` | where journals and the registry live |
| `MMDESK_RATE_LIMIT` | requests per minute per caller (default 30) |
| `MMDESK_RERANKER` | `1` enables the semantic reranker (off by default; see above) |
| `MMDESK_RERANKER_MODEL` | reranker model id override (default `Xenova/ms-marco-MiniLM-L-6-v2`) |

A key whose prefix is not recognised is not sent anywhere until you name the endpoint:
`sk-` is shared by several vendors, and guessing would hand one of them another's key.

## Showing it as "What would Money Mark do?" (the mm tenant)

The shipped page names no one. A tenant can present itself under a persona's name, because the
second-opinion heading, the lens heading and the attribution line all render from the tenant
record — no name is hard-coded anywhere in the source. The tenant is data:

```bash
# 1. the tenant record carries the consent line
python -c "from mmdesk import tenants; tenants.create('mm', 'Money Mark', 'Money Mark', \
  'Built with Money Mark\'s permission; he is not responsible for its output.')"
# 2. the material becomes the lens (verbatim quotes with timestamps)
python scripts/ingest_corpus.py --tenant mm --src corpus/mm-source.jsonl --title "Money Mark — published videos"
# 3. open the page as that tenant
#    http://127.0.0.1:8791/?tenant=mm
```

Then the page reads "Money Mark (proxy score)", the second-opinion panel asks "What would
Money Mark do?", the card quotes his actual passages with clickable timestamps, and the
attribution line appears in the banner. The lens quotes verbatim or refuses; a question about
something the material never covers is refused by name, not answered by invention.

**This demo is local-first.** The corpus is un-gitted by design, and on a serverless host the
tenant registry does not persist — so the branded experience works on a machine that holds the
material, while the public deployment stays generic. A hosted branded demo needs either the
owner's decision to ship the corpus (public) or a host with persistent storage.

**The fine-tuned adapter ("trained on his material")** is a separate step — see Layer 2 below.
The retrieval lens needs no training: it quotes. The adapter learns the *decision framework*
and requires the GPU training environment; the pipeline is documented and repaired, but the
training and serving scripts have not been executed end to end on the review machine.

## Deployment

`asgi.py` is the Vercel entrypoint (declared in `pyproject.toml`); `requirements.txt` carries
the runtime dependencies including `uvicorn`. On a serverless host the registry and journals
live in a per-instance temp directory and **do not survive**: `/health` reports
`registry_persistent: false`, tenant creation is refused rather than accepted-and-lost, and
the UI says the journal is temporary. A demo is fine; a real record needs a database.

The deployed demo is `https://mmark-sepia.vercel.app/` (built from this repo).

## Known limits

In `docs/KNOWN-LIMITATIONS.md`, maintained from actual runs: the score is an unvalidated
proxy, market data is daily and delayed, no journal horizon has closed so no performance claim
is made anywhere, and voice needs a local machine. The repo name and demo host still say "MM"
— renaming is an open decision for the owner.
