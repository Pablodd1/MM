> **Archived 2026-11-27** — historical, written for an earlier build. Superseded by `README.md`,
> `docs/HANDOVER.md` and `docs/KNOWN-LIMITATIONS.md`. It documents a deleted entrypoint, a private repo that is not this one, stale test
> counts and stale endpoint behaviour. Deployment facts now live in `README.md`.

# Deploy

Two targets: **GitHub** (done, private) and **Vercel** (prepared; needs one authenticated
command — see "Why this last step needs you").

## Verified before writing this

| Check | Result |
|---|---|
| `pytest -q` | 57 passed |
| `api.index:app` imports as Vercel imports it | FastAPI, 10 routes |
| Serverless simulation (`VERCEL=1`, TestClient) | `/health` → `data_dir=/tmp/mmdesk`, `journal_persistent=false`; `/api/analyze` 200 with 6 shares and 5 cited passages; `/api/chart` 501 |
| Local server | `http://127.0.0.1:8791`, same behaviour |
| Corpus bundled | `corpus/passages.jsonl`, 4,279 passages, 5.4 MB |

## GitHub

```bash
git remote -v          # origin → github.com/DrDraesel/mmdesk (private)
git push origin main
```

## Vercel — one command

```bash
npx vercel@latest link      # choose the existing project or create "mmdesk"
npx vercel@latest --prod
```

Non-interactive, if you prefer a token:

```bash
# create at https://vercel.com/account/tokens
export VERCEL_TOKEN=...
npx vercel@latest --prod --yes --token "$VERCEL_TOKEN"
```

## Why this last step needs you

Deployment requires an authenticated Vercel account. On this machine:

- no `vercel` CLI session exists (`~/.vercel` absent),
- no `VERCEL_TOKEN` in the environment, vault, or any project `.env`,
- neither vercel.com nor github.com is signed in in the browser session the agent can drive.

I will not guess or borrow credentials. One login (or one token) unblocks it, and I can run the
deploy immediately after.

## What runs on Vercel

- `api/index.py` — ASGI entry that puts the repo root on `sys.path` and exposes `app`.
- `vercel.json` — `@vercel/python`, all routes to the function, 1 GB memory, 30 s max.
- `requirements.txt` — runtime only (fastapi, pydantic, httpx, multipart, yfinance).
  `requirements-dev.txt` holds transcription and test tooling, which Vercel does not need.
- Static UI is served by the FastAPI `/` route from `mmdesk/static/index.html`.

## Serverless constraints, stated plainly

1. **The journal is ephemeral.** Vercel's bundle is read-only; `VERCEL` switches the journal to
   `/tmp/mmdesk`, which is per-instance and disappears. `/health` reports
   `journal_persistent: false`. A demo is fine; a real journal needs a database (Postgres/KV).
2. **Market data comes from a data-centre IP.** yfinance may rate-limit or block Vercel's egress.
   If that happens the app returns **422 with the missing fields**, by design — it does not
   invent a price. The fix is a keyed provider (Polygon, Tiingo, Alpha Vantage), not a fallback
   to stale numbers.
3. **No model key on Vercel** unless you add `DEEPSEEK_API_KEY` to the project env. Without it
   the narrative is the deterministic template and says so.
4. **Cold starts** add ~1–2 s; the first yfinance call can add 2–4 s.

## Post-deploy checks (run these, don't assume)

```bash
curl -s https://<deployment>/health
curl -s -X POST https://<deployment>/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"question":"Is NVDA a buy here?","ticker":"NVDA","account_equity":50000,"risk_pct":0.01}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<deployment>/api/chart -F 'file=@x.png'
```

Expect: `/health` 200 with `journal_persistent:false`; analyze 200 with `grounding.sources`
non-empty; chart **501** with `capability_not_implemented`.
If analyze returns 422, read `detail.missing` — that is yfinance being blocked from the
data-centre IP, not a bug in the app.
