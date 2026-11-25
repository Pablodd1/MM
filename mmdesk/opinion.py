"""The MM Opinion button: one call, Grok reads the decision card and answers in two voices.

Design constraint that shapes everything here: the model NEVER produces a number. The card's
figures are computed by the deterministic layer and passed in as context; the model returns only
prose and a stance word. A response containing any numeral is rejected and redone; a second
failure returns a labelled template instead of pretending a model wrote it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

SECRETS = Path(os.path.expanduser("~/.config/mmdesk/llm.key"))

BASE = os.environ.get("MMDESK_LLM_BASE") or "https://api.x.ai/v1"
MODEL = os.environ.get("MMDESK_LLM_MODEL") or "grok-4.7"

SYSTEM = """You are the opinion layer of a decision-support tool. Two voices, one answer.

THE ANALYSIS VOICE writes the reasoning a disciplined investor would use: what the setup is, what
the risk is, what would prove it wrong. THE MM VOICE states how Mark Gomes's published framework
reads the same card. It may only reference the cited passages supplied; if no passage bears on the
question it must say so in plain words rather than inventing a position for him.

You never compute and never write a single digit. No prices, percentages, scores, dates, counts or
fractions — not even inside a phrase like "top 3". Refer to every quantity in words ("the computed
score", "the stated risk budget", "the invalidation level"). The figures are rendered separately
from typed fields; duplicating them is the failure mode this guard exists for.

Return ONLY a JSON object with exactly these keys:
  "stance": one of add, hold, trim, pass
  "analysis": three to five sentences of reasoning
  "mm_opinion": two to four sentences in Mark's voice, grounded in the cited passages
  "agreement": one sentence on whether the two voices agree, and why
  "falsifier": one sentence naming what would prove this wrong
"""


def api_key() -> str:
    env = (os.environ.get("MMDESK_LLM_KEY") or "").strip()
    if env:
        return env
    if SECRETS.exists():
        return SECRETS.read_text(encoding="utf-8").strip()
    return ""


def available() -> bool:
    return bool(api_key())


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _template(card: dict, reason: str) -> dict:
    mm = card.get("mm_lens") or {}
    sl = card.get("standard_lens") or {}
    stance = (card.get("verdict") or "hold").lower()
    return {
        "stance": stance,
        "analysis": (
            f"The MM lens reads {mm.get('action', 'unclear')} with a score of "
            f"{mm.get('score_10', 'n/a')}/10, while the conventional read is "
            f"{sl.get('action', 'unclear')}. Position size follows the computed score, "
            "not the strength of this opinion."
        ),
        "mm_opinion": (
            "No model wrote this line. The opinion shown is assembled from the card's own "
            "fields, which is why it sounds mechanical — it is."
        ),
        "agreement": (card.get("disagreement") or {}).get("summary", "") or "Not assessed.",
        "falsifier": (card.get("falsifiers") or [""])[0],
        "generated_by": f"deterministic template ({reason})",
    }


def opinion(card: dict, citations: list[dict] | None = None, timeout: float = 45.0) -> dict:
    key = api_key()
    if not key:
        return _template(card, "no key configured")

    context = {
        "instrument": card.get("instrument"),
        "mode": card.get("mode"),
        "computed_verdict": card.get("verdict"),
        "mm_lens": card.get("mm_lens"),
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
        "model": MODEL,
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
                f"{BASE.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload, timeout=timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
            prose = " ".join(str(data.get(k, "")) for k in
                             ("analysis", "mm_opinion", "agreement", "falsifier"))
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
            data["generated_by"] = f"{MODEL} @ {BASE.split('//')[-1].split('/')[0]}"
            return data
        except Exception as exc:  # noqa: BLE001 - a model failure must not break the card
            last = f"{type(exc).__name__}: {exc}"[:160]

    return _template(card, f"model failed: {last}")
