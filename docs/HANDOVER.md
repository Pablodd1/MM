# Handover — Decision Desk

Status as of 2026-11-27. Written for three readers: the next engineer, a designer picking up the
interface, and a reviewer deciding whether it can be shown to a client or put in front of money.

---

## 1. What this is

A **decision-support desk for a single investment question**. It grounds the numbers, scores the
setup, sizes the position deterministically, shows where two lenses disagree, journals the call, and
offers a second opinion on demand — from a hosted model or from a tenant's own fine-tuned adapter.

**Intended use cases**

| Use case | Who | What it is good for |
|---|---|---|
| Analyst research desk (single tenant) | one PM / analyst | a repeatable card, a written falsifier, and a journal that grades itself later |
| Creator-method product | someone with a published, attributable method | their material quoted verbatim with timestamps, optional adapter in their voice |
| Team decision audit | small fund / RIA research team | per-tenant journals, per-tenant material, an explicit record of what was decided and why |
| Teaching / demo | internal | showing the difference between a computed decision and a model's opinion |

**Not this product, and the code should not be stretched to pretend otherwise**

- Not a trading bot: no order routing, no broker connection, no execution.
- Not real-time: daily bars from a free provider; intraday mode is labelled indicative.
- Not advice: no personalisation, no registration, no performance claim. The disclaimer is a
  statement of fact about the build, not decoration.
- Not a forecasting engine: it computes levels from volatility and structure and refuses to predict.
- Not a way to use someone's name, voice or posts without permission. Material is per tenant, and
  the repo ships none.

---

## 2. Status board

**Repository**

- Private source: `DrDraesel/mmdesk` — `bf3eb15` (this commit will be one ahead).
- Public + deploy source: `Pablodd1/MM` — `1b6aa5e7`, public, **no corpus**.
- Local working copy: `C:\Users\GAMEPOWER\projects\moneymark-research`.
- Live demo: `https://mmark-sepia.vercel.app/` — Vercel builds from the public repo.

**Size**: 6,134 lines of application code (34 modules/scripts), 1,413 lines of tests,
**121 tests passing** (108 pass + 13 skip on a clean clone without material).

### Works, verified

| Capability | Evidence |
|---|---|
| Market grounding, 2-year daily bars, `auto_adjust`, 5-min cache | `/api/analyze` returns `as_of`, provider, `missing[]`; failure → 422 INCOMPLETE |
| Rubric score (0–10) + fundamental bands | `mmdesk/rubric.py`, tested |
| Position sizing in `Decimal`, fee/open-risk aware, zero-share case | `mmdesk/rules.size_position`, combinatorial tests in `tests/test_sizing_review.py` |
| Standard lens (trend, momentum, vol, range position) | `mmdesk/standard.py` |
| Two-lens reconciliation with explicit disagreement | `mmdesk/decision.reconcile`, surfaced in every card |
| Per-tenant BM25 retrieval, vocabulary gate, adaptive citation floor | `mmdesk/retrieval_shim.py`, verified with a 1-passage tenant and an absent-topic refusal |
| Verbatim quote with timestamp, contiguous-span guarantee | `/api/lens`, `mmdesk/persona.best_span` |
| On-demand second opinion, two voices, digit ban + retry + labelled template | `/api/opinion`; live shows `deterministic template (cloud egress disabled …)` |
| Provider routing: tenant adapter → hosted model → template | `mmdesk/opinion.provider` |
| Tenancy: registry, 404 on unknown, per-tenant journals, audit rows | `mmdesk/tenants.py`, `tests/test_tenants.py` |
| Adapter gate: candidate vs active, agreement ≥ 0.85 + leakage-free | `activate_adapter`, `scripts/promote_adapter.py`, 409 on refusal |
| Layer 2 training pipeline (dataset → QLoRA → eval → register) | `scripts/build_sft_dataset.py`, `train_decision_lora.py`; measured 0.983 agreement on 60 held-out cases |
| Multi-tenant adapter server, OpenAI-compatible | `scripts/serve_lens.py`; refuses non-active adapters |
| Journal with gap-aware grading from fetched OHLC | `mmdesk/journal.py`, `/api/journal/grade` |
| Local voice loop (Whisper STT + Kokoro TTS) | `/api/voice`, `/api/voice/selftest`; **501 where there is no GPU** |
| Single-page UI, tenant switch, analyze, opinion, journal, scenario | `mmdesk/static/index.html` |

### Works partly / with caveats

| Area | Caveat |
|---|---|
| Deployed persistence | On Vercel the registry and journals live in `/tmp` (`journal_persistent: false`). They vanish on cold start and are per-instance. Demo-only. |
| Auth | There is **no authentication**. The tenant id is an address, not a credential. Admin routes are loopback-only unless `MMDESK_ADMIN_TOKEN` is set. |
| Rate limiting | In-memory, per client IP, per instance. Not a real quota. |
| Voice | Entirely local; unavailable on the deployed host. Never claimed to work there. |
| Chart input | Image is stored as a reference and **refused as a number source** by design; typed levels required. No candlestick rendering yet. |
| Crowd simulation | Six deterministic styles + influence model (`/api/crowd/talk`); it is a sensitivity check, labelled as such, not a forecast. |
| Adapter | Trained on synthetic cases labelled by the shipped engine. That is fidelity to the rule table, **not** investment skill. Not hosted live. |

### Does not exist yet

- No screener / universe scan; one ticker per request.
- No point-in-time fundamentals; no corporate-action ledger beyond `auto_adjust`.
- No backtester; no closed journal horizons, therefore **no performance claim anywhere**.
- No VLM screenshot parsing (deliberate).
- No CI (no GitHub Actions), no linter config, no type-check gate.
- No schema migrations; SQLite is created on first use.
- No structured logging or metrics beyond `request_id` in the payload.
- No i18n (English only), no accessibility audit.
- No billing, seats, or per-tenant quota.
- No charting library; the UI draws no price chart.

---

## 3. Architecture map

```
browser (mmdesk/static/index.html)
   │  X-Tenant header
   ▼
FastAPI (mmdesk/service.py)
   ├─ data.py ............ yfinance grounder, cache, INCOMPLETE on failure
   ├─ rubric.py .......... 0–10 score (bands or computed components)
   ├─ rules.py ........... rule table, Decimal sizing, alerts, citations
   ├─ standard.py ........ trend, momentum, ATR, realised vol, range position
   ├─ decision.py ........ both lenses + reconciliation + falsifiers
   ├─ card.py ............ decision-card contract (JSON + markdown)
   ├─ persona.py ......... contiguous verbatim quote or refusal
   ├─ retrieval_shim.py .. per-tenant BM25, vocabulary gate, adaptive floor
   ├─ tenants.py ......... registry (SQLite), journal paths, adapter state + gate
   ├─ llm.py ............. narrative, digit ban, provider inference
   ├─ opinion.py ......... Layer 2: adapter → hosted → template
   ├─ journal.py ......... append, grade with gap fills, summary
   └─ voice.py ........... faster-whisper + Kokoro, local only
corpus/<tenant>/passages.jsonl   ← ingest_corpus.py (gitignored)
models/<tenant>/                 ← train_decision_lora.py (gitignored)
data/desk.db                     ← registry + audit (gitignored)
```

Ops scripts: `ingest_corpus.py`, `build_sft_dataset.py`, `train_decision_lora.py`,
`promote_adapter.py`, `serve_lens.py`, `make_review_bundle.py`, `verify_llm_path.py`.

---

## 4. Invariants — do not break these

1. **No number in model prose.** The guard is `any(ch.isdigit())`, not a comparison against context.
   A violation retries once, then falls back to a template that names itself. Every visible figure is
   rendered from a typed field.
2. **Sizing is deterministic and provably inside the risk budget.** `Decimal`, `ROUND_DOWN`, open
   risk and fees subtracted before dividing, `min(by_risk, by_conviction, by_cash)`, zero shares with
   the number in the reason when one share breaches the budget. Assert `risk_at_stop <= budget`.
3. **Quotes are contiguous.** Only adjacent sentences may be combined. A quote is either a substring
   of one passage or it is a refusal.
4. **Empty means empty.** `[]` from retrieval is "nothing in this tenant's material", never "no
   opinion". The lens says which.
5. **A candidate adapter is not routable.** Only `status: active` resolves; the tenant→adapter map is
   server-side; a request names a tenant, never an adapter.
6. **A key is not consent to egress.** Hosted providers require `MMDESK_ALLOW_CLOUD_LLM=1`.
7. **The gate lives in the registry, not the trainer.** Promotion needs agreement ≥ 0.85 **and** a
   leakage-free evaluation.
8. **Per-tenant caches.** Retrieval caches by index path. A shared cache keyed on the query serves
   one tenant's passages to another.
9. **The citation floor scales with corpus size.** An absolute floor calibrated on ten thousand
   passages refuses everything on a one-page corpus.
10. **A chart image is a reference, never a source of numbers.**

---

## 5. Run and verify

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe -m uvicorn mmdesk.service:app --port 8791
.venv/Scripts/python.exe -m pytest tests/ -q        # 121 passing
```

Five-minute smoke test:

1. `/health` → `tenants[0].id == "default"`, `corpus.available false`.
2. `POST /api/lens {"question":"…"}` → `refused: true` with a sentence about absent material.
3. `POST /api/tenants` (loopback) then `ingest_corpus.py --tenant <id> --src …`.
4. `POST /api/lens` with `X-Tenant: <id>` → verbatim quote + `stamp`.
5. `POST /api/analyze` → verdict, sized position, `observability.tenant`.
6. `POST /api/opinion` → `generated_by` naming adapter / hosted model / template.

Deploy notes: `asgi.py` is the entrypoint; `pyproject.toml` mirrors dependencies; `vercel.json` is
`$schema` only; `corpus/`, `tests/`, `eval/`, venvs and secrets are excluded. On a read-only bundle
both the registry and journals resolve under `/tmp/mmdesk`.

---

## 6. Operations

**Add a tenant (no training needed)**

```bash
curl -X POST localhost:8791/api/tenants -H 'Content-Type: application/json' \
  -d '{"id":"acme","name":"Acme Capital","lens_label":"Acme lens"}'
python scripts/ingest_corpus.py --tenant acme --src ./material --title "Acme method"
```

**Give a tenant its own assistant**

```bash
python scripts/build_sft_dataset.py --tenant acme --n 800 --batch 10
trainenv/Scripts/python.exe scripts/train_decision_lora.py --tenant acme --epochs 5 \
    --rank 32 --base Qwen/Qwen2.5-3B-Instruct --register
python scripts/promote_adapter.py --tenant acme
trainenv/Scripts/python.exe scripts/serve_lens.py --port 8899
```

Full detail: `docs/LAYER2.md`. Measured results and their limits: `docs/MODEL-CARD.md`.

**Environment**

| Variable | Purpose |
|---|---|
| `MMDESK_LLM_KEY` | hosted model key (never enables egress alone) |
| `MMDESK_ALLOW_CLOUD_LLM=1` | explicit egress opt-in |
| `MMDESK_LLM_BASE`, `MMDESK_LLM_MODEL` | provider override |
| `MMDESK_DATA_DIR`, `MMDESK_DB` | writable state location |
| `MMDESK_ADMIN_TOKEN` | enables remote admin with `X-Admin-Token` |
| `MMDESK_RATE_LIMIT` | requests/minute per client |

---

## 7. Defects and debt, in the order a reviewer will hit them

1. **No auth.** Anyone who can reach the service can use any tenant id they know. Fix: JWT/session,
   tenant from the token, not the header. `mmdesk/service._tenant_from`.
2. **Ephemeral state on the host.** Registry and journals in `/tmp`. Fix: Postgres (provider of the
   deployer's choice) with a migrations table; keep SQLite for local.
3. **Tenant registry is not concurrency-safe on multiple instances.** SQLite + serverless = separate
   copies. Same fix as (2).
4. **No quota per tenant.** A single tenant can exhaust the model budget. Fix: token ledger keyed by
   tenant, hard cap, 429 with the number.
5. **Audit rows are written after the card, not with it.** A crash between the two loses the audit
   record. Fix: single transaction, decision id generated before the write.
6. **`/api/analyze` calls the provider synchronously per request.** No batching, no circuit breaker.
   Fix: provider timeout + breaker + last-good-cache with an explicit staleness badge.
7. **Public repo name and deploy host still say "MM".** Rename repo to `decision-desk`, reconnect
   Vercel, update docs. Old URLs redirect.
8. **No CI.** Add a workflow: `pytest -q` on push, plus `python -m compileall` and a secret scan.
9. **Eval fixtures reference the old corpus.** `tests/test_retrieval.py` skips when it is absent, but
   `eval/retrieval_golden.jsonl` was labelled against that corpus; a new tenant needs its own golden
   set. Fix: make the golden file per tenant.
10. **UI is one static file with inline CSS/JS.** No build step (a feature), no component boundaries
    (a cost). See the designer brief.
11. **Scenario and crowd endpoints are unauthenticated and uncached.** Cheap to compute, but they
    leak activity patterns. Fold into the auth work.
12. **No structured logs.** Add JSON logs with `tenant_id`, `request_id`, `latency_ms`, `model`,
    `prompt_tokens` — enough to answer "what did it cost and who used it".
13. **Voice returns 501 on the deployed host.** Correct behaviour, poor UX: the button should hide
    itself when `/health.voice.available` is false.
14. **Chart upload stores nothing.** It validates and refuses; no storage, no later review. Decide:
    drop the feature or persist the reference image with the journal entry.

---

## 8. Designer brief

**The screen is an argument, not a dashboard.** The product's value is the *disagreement* between two
lenses and the evidence that settles it. Design around that.

Order of information (current card already encodes it):

1. **Verdict** — one word and one sentence.
2. **Two lenses side by side** — tenant lens (score + cited rules) vs standard lens.
3. **Disagreement** — where they split and what evidence decides.
4. **Levels and size** — invalidation, first target, shares, risk at stop.
5. **Falsifiers** — what would prove this wrong.
6. **Second opinion** — on demand only; label which layer wrote it.
7. **Provenance strip** — `as_of`, provider, corpus coverage, layer names.

Hard requirements

- **Never** render a number that did not come from a typed field; the model is forbidden digits.
- Always show **which layer spoke**: adapter / hosted model / deterministic template.
- Empty states are first-class: no material, no key, provider down, data incomplete. Each has its own
  sentence, and none of them should look like an error dialog.
- The disclaimer is content, not footer noise: "not investment advice" must survive scroll and print.
- Colour cannot be the only signal (alert levels, green/red). Pair with text and shape.

Current UI debt

- Inline CSS/JS in `mmdesk/static/index.html`; no design tokens, no spacing scale.
- Tenant switch is a raw text input in the header — a developer affordance. Replace with a proper
  selector or a URL route (`/t/<tenant>`), persisted per session.
- No price chart. If added: `lightweight-charts` (Apache-2.0) for price/levels, ECharts for
  distributions. Levels must be rendered from typed values with their `as_of`.
- Mobile layout unverified; long quotes and the two-lens comparison will be the hard cases.
- No print stylesheet. A one-page PDF of a card + journal entry is the artefact a professional
  actually keeps.
- No accessibility pass: focus order, contrast, ARIA on the opinion panel, keyboard access to the
  mic button.

---

## 9. Reviewer checklist

- [ ] Can you make the app state a number the model invented? (Try: ask for a price target.)
- [ ] Does a refusal name what is missing, or does it bluff?
- [ ] Does an unknown tenant fall back to the default? (It must 404.)
- [ ] Can a candidate adapter answer? (It must not.)
- [ ] Is `risk_at_stop` ever above the stated budget? (Property tests cover the grid; extend it.)
- [ ] With no key, does the card still render and say who wrote it?
- [ ] Are journal entries graded from fetched bars rather than caller-supplied prices?
- [ ] Is any claim of performance, accuracy or prediction made in the UI or docs? (It must not be.)
- [ ] Consent and licensing: every corpus and every adapter has a documented basis; none ships in
      the repo.

---

## 10. Next priorities

**P0 — before another client or another demo**

1. Auth + tenant from token (`mmdesk/service.py`, new `mmdesk/auth.py`).
2. Durable state: Postgres for registry, journals, audit (`mmdesk/tenants.py`, `mmdesk/journal.py`).
3. Per-tenant quota + cost ledger (`mmdesk/llm.py`, `mmdesk/opinion.py`).
4. CI: tests on push, no secrets, compile check (`.github/workflows/ci.yml`).
5. Rename repo/host away from "MM".

**P1 — makes it a product**

6. Chart rendering from typed levels (`lightweight-charts`).
7. Claim-test endpoint (`POST /api/selftest/claims`) running the reviewer checklist as assertions.
8. Provider circuit breaker + staleness badge (`mmdesk/data.py`).
9. Per-tenant golden set for retrieval, generated from that tenant's corpus (`eval/`).
10. Mobile + print stylesheet; hide the mic when voice is unavailable.

**P2 — the interesting work**

11. LoRA-vs-RAG blind A/B harness: does the adapter beat base model + prompt + retrieved examples on
    voice fidelity and decision agreement? Ship the adapter only if it wins.
12. Screener (Qlib, MIT) as an offline nightly job, never in the request path.
13. Probabilistic cone as a labelled artefact with coverage tests; never an input to sizing.
14. Trajectory export (ShareGPT) from journal + opinion pairs for monthly retraining.

---

## 11. Open decisions (owner)

- **Product name and domain.** "Decision Desk" is a placeholder; the repo still says MM.
- **Who is the first paying tenant,** and do they need SSO?
- **Hosting shape for Layer 2:** hosted adapter (HF Endpoint / RunPod / Together) vs a GPU box the
  tenant owns. Pricing follows from this.
- **Is per-tenant fine-tuning sold as a feature,** or is it internal-only until the LoRA-vs-RAG test
  says it earns its cost?
- **Deletion policy:** how does a tenant's data get removed from an adapter? (Retrain without it is
  the only honest answer today.)
