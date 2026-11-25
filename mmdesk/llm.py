"""The language layer — explanation only, never arithmetic.

Design rules enforced here:
  * The model receives computed values and cited passages, never raw market access.
  * Any number in the model's prose must already exist in the supplied context.
    A response containing an ungrounded figure is rejected and retried once; if it
    fails again, the deterministic template is used instead.
  * Without an API key the app still works: it falls back to a template that says
    plainly that no model is configured.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = DEFAULT_BASE
    model: str = DEFAULT_MODEL
    timeout: float = 60.0
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Any OpenAI-compatible endpoint, not just DeepSeek.

        MMDESK_LLM_* wins; DEEPSEEK_* is kept so an existing deployment does not break.
        Groq, OpenRouter, Together, vLLM and llama.cpp's server all speak this shape.
        """
        key = (os.environ.get("MMDESK_LLM_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        base = (os.environ.get("MMDESK_LLM_BASE") or os.environ.get("DEEPSEEK_BASE_URL")
                or DEFAULT_BASE).strip()
        model = (os.environ.get("MMDESK_LLM_MODEL") or os.environ.get("DEEPSEEK_MODEL")
                 or DEFAULT_MODEL).strip()
        return cls(api_key=key, base_url=base, model=model)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def provider(self) -> str:
        host = self.base_url.split("//")[-1].split("/")[0]
        return host or "unknown"


SYSTEM = """You are the writing layer of a decision-support tool.

You do not compute anything and you never write numerals. Not one digit: no prices, no
percentages, no scores, no dates, no counts. Refer to quantities in words ("the computed
score", "the stated risk budget") and let the deterministic layer supply the figures.

You are not Mark Gomes and must never claim to speak for him or know his positions. You may
say what the corpus passages show he has said, citing only the passages given.

Write plainly, in complete sentences, with no hype and no promises about returns.
Return ONLY a JSON object with exactly these keys:
  "verdict_reason": one sentence explaining the verdict that was computed for you,
  "mm_narrative": 2-4 sentences on what the MM lens shows and which cited rule drives it,
  "standard_narrative": 2-4 sentences on the conventional read,
  "disagreement": 1-3 sentences naming the conflict and what evidence settles it,
  "caveat": one sentence on the biggest thing that could make this wrong.
"""


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


def template_narrative(card: dict) -> dict:
    """Deterministic fallback — used when no key is set or the model misbehaves."""
    mm = card.get("mm_lens") or {}
    std = card.get("standard_lens") or {}
    dis = card.get("disagreement") or {}
    return {
        "verdict_reason": card.get("verdict_reason") or "",
        "mm_narrative": (
            f"MM lens scores this {mm.get('score_10', '?')}/10 and would "
            f"{mm.get('action', '?')}. Sizing follows the score, capped at "
            f"{(mm.get('position_pct') or 0) * 100:.1f}% of equity."
        ),
        "standard_narrative": (
            f"Conventional read is {std.get('action', '?')}: trend "
            f"{'positive' if (std.get('trend') or {}).get('score', 0) > 0 else 'negative'} "
            f"with {std.get('charts_supported', 0)} of 3 measures supporting a long."
        ),
        "disagreement": dis.get("summary") or "",
        "caveat": (card.get("falsifiers") or [""])[0],
        "generated_by": "template (no model configured)",
    }


def narrate(card: dict, corpus_hits: list[dict], config: LLMConfig | None = None) -> dict:
    cfg = config or LLMConfig.from_env()
    if not cfg.configured:
        return template_narrative(card)

    context = {
        "instrument": card.get("instrument"),
        "mode": card.get("mode"),
        "verdict": card.get("verdict"),
        "mm_lens": card.get("mm_lens"),
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

    last_error = ""
    for attempt in (1, 2):
        try:
            import httpx

            r = httpx.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
                json=payload, timeout=cfg.timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content)

            prose = " ".join(str(data.get(k, "")) for k in
                             ("verdict_reason", "mm_narrative", "standard_narrative",
                              "disagreement", "caveat"))
            if contains_digits(prose):
                last_error = "model emitted numerals"
                payload["messages"] += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": (
                        "Your answer contained numerals. Rewrite it with no digits at all — "
                        "describe quantities in words only."
                    )},
                ]
                continue

            bad = ungrounded_numbers(content, context)
            if bad:
                last_error = f"ungrounded numbers: {sorted(bad)[:5]}"
                payload["messages"] += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": (
                        "Your previous answer contained numbers not present in the context: "
                        f"{sorted(bad)[:5]}. Rewrite without numerals."
                    )},
                ]
                continue
            data["generated_by"] = f"{cfg.model} (attempt {attempt})"
            return data
        except Exception as exc:  # noqa: BLE001 - never break the card on a model error
            last_error = f"{type(exc).__name__}: {exc}"

    out = template_narrative(card)
    out["generated_by"] = f"template (model failed: {last_error[:120]})"
    return out
