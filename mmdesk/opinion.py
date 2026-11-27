"""Layer 2: an on-demand second opinion that argues with the computed card.

Design constraint that shapes everything here: the model NEVER produces a number. The card's
figures are computed by the deterministic layer and passed in as context; the model returns only
prose and a stance word. A response containing any numeral anywhere is redone; a second failure
returns a labelled template instead of pretending a model wrote it.

Two ways to serve Layer 2, same interface:

* a tenant's **own adapter** (the fine-tuned second-layer assistant), addressed by the registry;
* a hosted model, resolved through `LLMConfig` — the same resolver the narrative uses, so a key
  is only ever sent to the vendor that issued it.

The distinction is not cosmetic: ``generated_by`` names which one wrote the words, and an adapter
that has not passed its gate is never reachable from here.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from .llm import LLMConfig

log = logging.getLogger("mmdesk.opinion")

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

# Keys the opinion reads, and the name a decision adapter trained on the SFT schema uses for
# the same field. Prose that arrived under the training name used to be dropped unread — and so
# to escape the digit check.
_OPINION_KEYS = {
    "analysis": ("analysis", "reasoning"),
    "lens_opinion": ("lens_opinion", "lens"),
    "agreement": ("agreement", "agrees"),
    "falsifier": ("falsifier",),
}
STANCES = {"add", "hold", "trim", "pass"}

# Cloud metadata endpoints. An adapter's base_url is an operator-supplied request target, so it
# is never allowed to point at the instance's own metadata service.
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata.azure.internal", "100.100.100.200"}


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
            "problem": "",
        }
    cfg = LLMConfig.from_env()
    return {"base": cfg.base_url, "model": cfg.model, "key": cfg.api_key, "kind": "hosted",
            "tenant": tenant.id if tenant is not None else None, "problem": cfg.problem}


def available(tenant: Any | None = None) -> bool:
    return bool(provider(tenant)["key"])


def endpoint_allowed(base: str) -> bool:
    """http(s) only, and never a link-local or metadata address."""
    try:
        parts = urlsplit(base if "//" in base else "//" + base)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return False
    if host in _BLOCKED_HOSTS:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)


def _pick(data: dict, names: tuple[str, ...]) -> str:
    """The first non-empty string the model supplied under any of `names`."""
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
        return _template(card, prov.get("problem") or "no key configured")
    # A key is not consent to send data out. Hosted providers need the explicit opt-in flag;
    # a tenant's own adapter runs on infrastructure the tenant already controls, so it does not.
    if prov["kind"] == "adapter" and not endpoint_allowed(prov["base"]):
        log.warning("refused adapter endpoint for tenant %s", prov.get("tenant"))
        return _template(card, "adapter endpoint refused")
    if prov["kind"] == "hosted" and os.environ.get("MMDESK_ALLOW_CLOUD_LLM") != "1":
        return _template(card, "cloud model disabled")

    # The question is the caller's text and the passages are the tenant's material; both are
    # sent as data inside one JSON document and the system prompt fixes the output keys.
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
    url = f"{prov['base'].rstrip('/')}/chat/completions"
    if prov["kind"] == "adapter":
        label = f"adapter {prov['tenant']}"
    else:
        label = f"{prov['model']} @ {prov['base'].split('//')[-1].split('/')[0]}"

    last = ""
    for attempt in (1, 2):
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {prov['key']}",
                         "Content-Type": "application/json"},
                json=payload, timeout=timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("completion is not text")
            # The digit check runs on the whole completion, whatever keys the model chose.
            if _has_digit(content):
                last = "model emitted numerals"
                payload["messages"] += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "Remove every numeral. Describe quantities in words."},
                ]
                continue
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("completion is not an object")
            out = {key: _pick(data, names) for key, names in _OPINION_KEYS.items()}
            stance = str(data.get("stance", "")).strip().lower()
            out["stance"] = stance if stance in STANCES else (card.get("verdict") or "hold").lower()
            if not any(out[k] for k in _OPINION_KEYS):
                raise ValueError("completion carried none of the expected fields")
            out["generated_by"] = label if attempt == 1 else f"{label} (retry {attempt})"
            return out
        except Exception as exc:  # noqa: BLE001 - a model failure must not break the card
            # Type name only: exception text can carry the endpoint URL and the provider's
            # message, and used to be returned to every caller in `generated_by`.
            last = type(exc).__name__
            log.warning("opinion attempt %s via %s failed: %s", attempt, prov["kind"], exc)

    return _template(card, f"model failed: {last}")
