# Layer 2 — the second-layer decision assistant, per tenant

Layer 1 is the deterministic product: fetched numbers, a rule engine, position sizing, a journal.
It works with no model, no corpus and no adapter. Layer 2 is the part that is *yours*: a
fine-tuned assistant that argues with the card in your framework, plus the retrieval lens that
quotes your material instead of paraphrasing it.

The point of this document is that Layer 2 is a **capability**, not a one-off build. Any tenant can
go through the same five steps; none of them require the app to know who the tenant is in advance.

---

## What Layer 2 is made of

| Part | What it holds | Where it lives |
|---|---|---|
| Lens (retrieval) | the tenant's own text, quoted verbatim with timestamps | `corpus/<tenant>/passages.jsonl` |
| Adapter (weights) | the tenant's reasoning style and framework, not its facts | `models/<tenant>/` |
| Registry | which adapter is active for which tenant, and its measured evaluation | `data/desk.db` |
| Serving | one OpenAI-compatible endpoint, adapter selected by model name | `scripts/serve_lens.py` |
| Gate | promotion requires agreement ≥ 0.85 **and** a leakage-free evaluation | `mmdesk/tenants.py` |

**Facts go in retrieval, style goes in weights.** A post published today is an index update in
seconds. The same post baked into weights is a ten-minute GPU run plus a full re-evaluation, and it
cannot be selectively deleted. That is why the adapter is not retrained daily and why "the model
memorised my notes" is not a feature.

---

## The five steps

```bash
# 1. create the tenant
curl -X POST localhost:8791/api/tenants -H 'Content-Type: application/json' \
  -d '{"id":"acme","name":"Acme Capital","lens_label":"Acme lens"}'

# 2. ingest their material (.json transcripts, .jsonl, .txt, .md)
python scripts/ingest_corpus.py --tenant acme --src ./acme-material --title "Acme method"

# 3. build a training set labelled by the shipped engine (not by a model's opinion)
python scripts/build_sft_dataset.py --tenant acme --n 800 --batch 10

# 4. train the adapter, register it as a candidate (this does NOT make it live)
trainenv/Scripts/python.exe scripts/train_decision_lora.py \
  --tenant acme --epochs 5 --lr 2e-4 --rank 32 --base Qwen/Qwen2.5-3B-Instruct --register

# 5. promote it — or be told why it was refused
python scripts/promote_adapter.py --tenant acme
```

Then serve it:

```bash
trainenv/Scripts/python.exe scripts/serve_lens.py --port 8899
curl localhost:8899/v1/models          # only tenants with an *active* adapter appear
```

The app routes automatically: `mmdesk.opinion.provider(tenant)` prefers an active adapter and falls
back to the hosted model (only with `MMDESK_ALLOW_CLOUD_LLM=1`) and then to the labelled template.

---

## Why the gate is in the registry

A training script is the party with an incentive to call a run a success. `promote_adapter.py`
prints the candidate's own numbers and refuses anything below:

- **stance agreement ≥ 0.85** with the engine on held-out cases, and
- **`leakage_free: true`** — the trainer aborts if any evaluation prompt appears in the training set.

Both failures have happened here. A run scored 0.917 because the adapter had been trained on the
evaluation split; another scored 0.275 because the prompt vocabulary disagreed with the labels. A
gate that lives in the trainer would have shipped the first one.

---

## Isolation, honestly stated

- One adapter per tenant, pinned to a base model and a path recorded in the registry.
- A request names a tenant (`X-Tenant` header) — never an adapter. The mapping is server-side, so a
  tenant cannot address another tenant's weights.
- Only `status: "active"` adapters are routable. A candidate is invisible to `opinion.provider`.
- Sharing one base model and one process is **logical** isolation, not physical. A routing bug is a
  cross-tenant leak; that is why the map is not client-supplied and why every generation is logged
  against a tenant in `decisions`.

If a customer requires hardware isolation, run a separate server per tenant with one adapter loaded.
The interface does not change.

---

## What the adapter may and may not do

**May:** choose the stance word, write the reasoning, name the falsifier — always within the card's
computed numbers, never emitting a digit.

**May not:** fetch, compute, price, size, or invent a quote. Those remain Layer 1's job, and the
digit guard rejects any response that tries.

**Measured, on a 3B QLoRA trained for ~10 minutes:** 0.983 stance agreement with the engine on 60
held-out cases, 0.983 digit-free prose. That is fidelity to a rule table, **not** evidence of
investment skill — see `docs/MODEL-CARD.md`.

---

## Adding a tenant without training anything

That is the normal case, and it is fully supported:

1. `create` the tenant,
2. `ingest_corpus.py` their material,
3. skip straight to the app — the lens quotes them verbatim, and Layer 2's prose comes from the
   hosted model or the template, labelled as such.

An adapter is an optimisation for voice and framework fidelity. It is never a prerequisite.
