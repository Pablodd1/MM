"""Layer 2: an on-demand second opinion that argues with the computed card.

Design constraint that shapes everything here: the model NEVER produces a number. The card's
figures are computed by the deterministic layer and passed in as context; the model returns only
prose and a stance word. A response containing any numeral is rejected and redone; a second failure
returns a labelled template instead of pretending a model wrote it.

Two ways to serve Layer 2, same interface:

* a tenant's **own adapter** (the fine-tuned second-layer assistant), addressed by the registry;
* a hosted **frontier model**, when the tenant has no adapter or it is not active.

The distinction is not cosmetic: ``generated_by`` names which one wrote the words, and an adapter
that has not passed its gate is never reachable from here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

SECRETS = Path(os.path.expanduser("~/.config/decisiondesk/llm.key"))
LEGACY_SECRETS = Path(os.path.expanduser("~/.config/mmdesk/llm.key"))

DEFAULT_BASE = os.environ.get("MMDESK_LLM_BASE") or "https://api.x.ai/v1"
DEFAULT_MODEL = os.environ.get("MMDESK_LLM_MODEL") or "grok-4.7"

SYSTEM = """You are the opinion layer of a decision-support tool. Two voices, one answer.

THE ANALYSIS VOICE writes the reasoning a disciplined investor would use: what the setup is, what
the risk is, what would prove it wrong. THE LENS VOICE states how the supplied material's framework
reads the same card. It may only reference the cited passages supplied; if no passage bears on the
question it must say so in plain words rather than inventing a position for the author.

You never compute and never write a single digit. No prices, percentages, scores, dates, counts or
fractions — not even inside a phrase like "top 3". Refer to every quantity in words ("the computed
score", "the stated risk budget", "the invalidation level"). The figures are rendered separately
from typed fields; duplicating them is the failure mode this guard exists for.

Return ONLY a JSON object with exactly these keys:
  "stance": one of add, hold, trim, pass
  "analysis": three to five sentences of reasoning
  "lens_opinion": two to four sentences in the material's voice, grounded in the cited passages
  "agreement": one sentence on whether the two voices agree, and why
  "falsifier": one sentence naming what would prove this wrong
"""


def _read_key() -> str:
    env = (os.environ.get("MMDESK_LLM_KEY") or "").strip()
    if env:
        return env
    for path in (SECRETS, LEGACY_SECRETS):
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def provider(tenant: Any | None = None) -> dict[str, Any]:
    """Where Layer 2 should send this request.

    An active adapter wins over the hosted model. The registry is the only place that decides
    this; a request cannot name an adapter, so a tenant cannot reach another tenant's weights.
    """
    adapter = tenant.active_adapter if tenant is not None else None
    if adapter:
        return {
            "base": adapter.get("base_url") or "http://127.0.0.1:8899/v1",
            "model": adapter.get("model") or f"{tenant.id}-lens",
            "key": adapter.get("key") or "local",
            "kind": "adapter",
            "tenant": tenant.id,
        }
    return {"base": DEFAULT_BASE, "model": DEFAULT_MODEL, "key": _read_key(),
            "kind": "hosted", "tenant": tenant.id if tenant is not None else None}


def available(tenant: Any | None = None) -> bool:
    return bool(provider(tenant)["key"])


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _template(card: dict, reason: str) -> dict:
    lens = card.get("lens") or {}
    sl = card.get("standard_lens") or {}
    stance = (card.get("verdict") or "hold").lower()
    return {
        "stance": stance,
        "analysis": (
            f"The reference lens reads {lens.get('action', 'unclear')} with a score of "
            f"{lens.get('score_10', 'n/a')}/10, while the conventional read is "
            f"{sl.get('action', 'unclear')}. Position size follows the computed score, "
            "not the strength of this opinion."
        ),
        "lens_opinion": (
            "No model wrote this line. The opinion shown is assembled from the card's own "
            "fields, which is why it sounds mechanical — it is."
        ),
        "agreement": (card.get("disagreement") or {}).get("summary", "") or "Not assessed.",
        "falsifier": (card.get("falsifiers") or [""])[0],
        "generated_by": f"deterministic template ({reason})",
    }


def opinion(card: dict, citations: list[dict] | None = None, timeout: float = 45.0,
            tenant: Any | None = None) -> dict:
    prov = provider(tenant)
    if not prov["key"]:
        return _template(card, "no key configured")
    # A key is not consent to send data out. Hosted providers need the explicit opt-in flag;
    # a tenant's own adapter runs on infrastructure the tenant already controls, so it does not.
    if prov["kind"] == "hosted" and os.environ.get("MMDESK_ALLOW_CLOUD_LLM") != "1":
        return _template(card, "cloud egress disabled (MMDESK_ALLOW_CLOUD_LLM != 1)")

    context = {
        "instrument": card.get("instrument"),
        "mode": card.get("mode"),
        "computed_verdict": card.get("verdict"),
        "lens": card.get("lens"),
        "standard_lens": card.get("standard_lens"),
        "risk": card.get("risk"),
        "disagreement": card.get("disagreement"),
        "falsifiers": card.get("falsifiers"),
        "cited_passages": [
            {"video_id": c.get("video_id"), "stamp": c.get("stamp"), "text": c.get("text")}
            for c in (citations or [])[:4]
        ],
    }

    payload = {
        "model": prov["model"],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(context, default=str)},
        ],
    }

    last = ""
    for _ in range(2):
        try:
            r = httpx.post(
                f"{prov['base'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {prov['key']}", "Content-Type": "application/json"},
                json=payload, timeout=timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            prose = " ".join(str(data.get(k, "")) for k in
                             ("analysis", "lens_opinion", "agreement", "falsifier"))
            if _has_digit(prose):
                last = "model emitted numerals"
                payload["messages"] += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "Remove every numeral. Describe quantities in words."},
                ]
                continue
            data["stance"] = str(data.get("stance", "")).strip().lower()
            if data["stance"] not in {"add", "hold", "trim", "pass"}:
                data["stance"] = (card.get("verdict") or "hold").lower()
            if prov["kind"] == "adapter":
                data["generated_by"] = f"adapter {prov['tenant']} @ {prov['base'].split('//')[-1].split('/')[0]}"
            else:
                data["generated_by"] = f"{prov['model']} @ {prov['base'].split('//')[-1].split('/')[0]}"
            return data
        except Exception as exc:  # noqa: BLE001 - a model failure must not break the card
            last = f"{type(exc).__name__}: {exc}"[:160]

    return _template(card, f"model failed: {last}")
