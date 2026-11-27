"""The language layer — explanation only, never arithmetic.

Design rules enforced here:
  * The model receives computed values and cited passages, never raw market access.
  * The model may not write a single digit. Every number a reader sees is injected by the
    deterministic layer, which knows its source. A response with a numeral is redone once; if it
    fails again, the deterministic template is used instead.
  * Without a usable key the app still works: it falls back to a template that says plainly
    that no model wrote it.

`LLMConfig.from_env()` is the one place that decides which vendor a key belongs to. Both prose
layers (the narrative here and the second opinion in `opinion.py`) resolve through it. They used
to default to different vendors, so one vendor's key was sent to another vendor's endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("mmdesk.llm")

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

# A key prefix identifies the vendor well enough to pick its endpoint, so a pasted key works
# without the operator also knowing the base URL. A prefix that is not recognised is NOT sent
# anywhere unless the operator names the endpoint with MMDESK_LLM_BASE: "sk-" is shared by
# several vendors, and guessing would hand one of them another's key.
PROVIDERS = {
    "gsk_": ("https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
    "xai-": ("https://api.x.ai/v1", "grok-4.7"),
    "sk-or-": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct"),
}

KEY_FILES = (
    Path(os.path.expanduser("~/.config/decisiondesk/llm.key")),
    Path(os.path.expanduser("~/.config/mmdesk/llm.key")),
)

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _read_key() -> tuple[str, str]:
    """The key and where it came from: env beats file, as does the current name over legacy."""
    for name in ("MMDESK_LLM_KEY", "DEEPSEEK_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    for path in KEY_FILES:
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value, str(path)
        except OSError:
            continue
    return "", ""


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: float = 60.0
    temperature: float = 0.2
    problem: str = ""     # why this layer cannot call a model, when it cannot

    @classmethod
    def from_env(cls) -> LLMConfig:
        """Any OpenAI-compatible endpoint. MMDESK_LLM_* wins; DEEPSEEK_* is kept so an
        existing deployment does not break."""
        key, origin = _read_key()
        if not key:
            return cls(problem="no model key configured")
        base = (os.environ.get("MMDESK_LLM_BASE") or os.environ.get("DEEPSEEK_BASE_URL")
                or "").strip().rstrip("/")
        model = (os.environ.get("MMDESK_LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL")
                 or "").strip()
        if base:
            inferred_model = _infer_model(key) or DEFAULT_MODEL
        elif origin == "DEEPSEEK_API_KEY":
            base, inferred_model = DEFAULT_BASE, DEFAULT_MODEL
        else:
            base, inferred_model = _infer(key)
            if not base:
                return cls(problem=(
                    "key prefix not recognised: set MMDESK_LLM_BASE to the vendor's endpoint"
                ))
        return cls(api_key=key, base_url=base, model=model or inferred_model)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @property
    def provider(self) -> str:
        host = self.base_url.split("//")[-1].split("/")[0]
        return host or "unknown"

    def discover_model(self) -> str:
        """Ask the provider what it serves. Model names retire; a hard-coded default then 404s.

        This never ran before: `httpx` was imported only inside `narrate`, so the NameError was
        swallowed by the except below and the stale name was returned.
        """
        try:
            resp = httpx.get(f"{self.base_url}/models", headers=self.headers, timeout=30)
            if resp.status_code != 200:
                return self.model
            names = [m.get("id", "") for m in resp.json().get("data", [])]
            grok = sorted((n for n in names if n.startswith("grok-")), reverse=True)
            return grok[0] if grok else self.model
        except Exception:
            log.warning("model discovery failed for %s", self.provider, exc_info=True)
            return self.model


def _infer(key: str) -> tuple[str, str]:
    for prefix, (base_url, model_name) in PROVIDERS.items():
        if key.startswith(prefix):
            return base_url, model_name
    return "", ""


def _infer_model(key: str) -> str:
    return _infer(key)[1]


SYSTEM = """You are the writing layer of a decision-support tool.

You do not compute anything and you never write numerals. Not one digit: no prices, no
percentages, no scores, no dates, no counts. Refer to quantities in words ("the computed
score", "the stated risk budget") and let the deterministic layer supply the figures.

You are not the author of the supplied material and must never claim to speak for them or know
their positions. You may say only what the supplied passages show, citing those passages.

Write plainly, in complete sentences, with no hype and no promises about returns.
Return ONLY a JSON object with exactly these keys:
  "verdict_reason": one sentence explaining the verdict that was computed for you,
  "lens_narrative": 2-4 sentences on what the reference lens shows and which cited rule drives it,
  "standard_narrative": 2-4 sentences on the conventional read,
  "disagreement": 1-3 sentences naming the conflict and what evidence settles it,
  "caveat": one sentence on the biggest thing that could make this wrong.
"""

# The keys a narrative needs, and the names a second-opinion adapter uses for the same thing.
_NARRATIVE_KEYS = {
    "verdict_reason": ("verdict_reason", "reasoning"),
    "lens_narrative": ("lens_narrative", "lens_opinion"),
    "standard_narrative": ("standard_narrative", "analysis"),
    "disagreement": ("disagreement", "agreement"),
    "caveat": ("caveat", "falsifier"),
}


def _numbers_in(text: str) -> set[str]:
    out = set()
    for raw in NUMBER_RE.findall(text or ""):
        try:
            out.add(f"{float(raw):.4f}")
        except ValueError:
            continue
    return out


def ungrounded_numbers(output: str, context: dict) -> set[str]:
    """Numbers in the model's text that never appeared in its context."""
    allowed = _numbers_in(json.dumps(context, default=str))
    return {n for n in _numbers_in(output) if n not in allowed}


def contains_digits(output: str) -> bool:
    """True if the text contains ANY digit.

    The defence-in-depth rule is blunt on purpose: the model is not allowed to emit
    numerals at all. Spelling out ("about seven dollars"), unicode digits, notation and
    derived arithmetic all become irrelevant when the model cannot print a digit. Every
    number a reader sees is injected by the deterministic layer, which knows its source.
    """
    return any(ch.isdigit() for ch in output)


def _pick(data: dict, names: tuple[str, ...]) -> str:
    """The first non-empty string the model supplied under any of `names`."""
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def template_narrative(card: dict, reason: str = "no model configured") -> dict:
    """Deterministic fallback — used when no key is set or the model misbehaves."""
    lens = card.get("lens") or {}
    std = card.get("standard_lens") or {}
    dis = card.get("disagreement") or {}
    trend_score = (std.get("trend") or {}).get("score") or 0
    trend_word = "positive" if trend_score > 0 else "negative" if trend_score < 0 else "flat"
    measures = std.get("measures_evaluated", 3)
    return {
        "verdict_reason": card.get("verdict_reason") or "",
        "lens_narrative": (
            f"Reference lens scores this {lens.get('score_10', '?')}/10 and would "
            f"{lens.get('action', '?')}. Sizing follows the score, capped at "
            f"{(lens.get('position_pct') or 0) * 100:.1f}% of equity."
        ),
        "standard_narrative": (
            f"Conventional read is {std.get('action', '?')}: trend "
            f"{trend_word} with {std.get('charts_supported', 0)} of {measures} measures "
            "supporting a long."
        ),
        "disagreement": dis.get("summary") or "",
        "caveat": (card.get("falsifiers") or [""])[0],
        "generated_by": f"deterministic template ({reason})",
    }


def narrate(card: dict, corpus_hits: list[dict], config: LLMConfig | None = None,
            endpoint: dict[str, Any] | None = None) -> dict:
    """Prose for a computed card.

    `endpoint` routes the call to a tenant's own adapter instead of the hosted provider, so a
    tenant's material never has to leave infrastructure that tenant controls.
    """
    if endpoint is not None:
        kind = "adapter"
        cfg = LLMConfig(api_key=endpoint["key"], base_url=endpoint["base"],
                        model=endpoint["model"])
        label = f"adapter {endpoint.get('tenant') or ''}".strip()
    else:
        kind = "hosted"
        cfg = config or LLMConfig.from_env()
        label = f"{cfg.model} @ {cfg.provider}"
    if not cfg.configured:
        return template_narrative(card, cfg.problem or "no model configured")

    context = {
        "instrument": card.get("instrument"),
        "mode": card.get("mode"),
        "verdict": card.get("verdict"),
        "lens": card.get("lens"),
        "standard_lens": card.get("standard_lens"),
        "disagreement": card.get("disagreement"),
        "risk": card.get("risk"),
        "falsifiers": card.get("falsifiers"),
        "corpus": [
            {"video_id": h.get("video_id"), "stamp": h.get("stamp"), "text": h.get("text")}
            for h in corpus_hits[:6]
        ],
    }

    payload = {
        "model": cfg.model,
        "temperature": cfg.temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(context, default=str)},
        ],
    }
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"

    last_error = ""
    for attempt in (1, 2):
        try:
            r = httpx.post(url, headers=cfg.headers, json=payload, timeout=cfg.timeout)
            if r.status_code == 404 and "model" in r.text.lower():
                fresh = cfg.discover_model()
                if fresh and fresh != payload["model"]:
                    payload["model"] = fresh
                    cfg.model = fresh
                    r = httpx.post(url, headers=cfg.headers, json=payload, timeout=cfg.timeout)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("completion is not text")
            if contains_digits(content):
                last_error = "model emitted numerals"
                payload["messages"] += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": (
                        "Your answer contained numerals. Rewrite it with no digits at all — "
                        "describe quantities in words only."
                    )},
                ]
                continue
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("completion is not an object")
            out = {key: _pick(data, names) for key, names in _NARRATIVE_KEYS.items()}
            if not any(out.values()):
                raise ValueError("completion carried none of the expected fields")
            out["generated_by"] = f"{label} (attempt {attempt})"
            return out
        except Exception as exc:  # noqa: BLE001 - never break the card on a model error
            # The exception text can contain the endpoint URL and the provider's message; it
            # goes to the server log, never into the card.
            last_error = type(exc).__name__
            log.warning("narrative attempt %s via %s failed: %s", attempt, kind, exc)
    return template_narrative(card, f"model failed: {last_error}")
