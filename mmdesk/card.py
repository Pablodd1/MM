"""The decision card: one structure the rule engine, data layer and UI all share.

The card is assembled from deterministic parts. The narrative fields carry an
explicit `generated_by` marker so a reviewer can always tell which sentences came
from a language model and which came from maths.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

DISCLAIMER = (
    "Educational tool, not investment advice. Rules are computed deterministically from stated "
    "inputs; the lens score is a documented proxy, not a rating by any person. Positions may be "
    "entered, exited or reversed without notice. Verify every number before acting."
)

VERSION = "0.1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Citation:
    rule: str
    video_id: str
    start_s: float
    url: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"https://www.youtube.com/watch?v={self.video_id}&t={int(self.start_s)}s"

    @property
    def stamp(self) -> str:
        s = int(self.start_s)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


@dataclass
class Card:
    question: str = ""
    instrument: str = ""
    mode: str = "swing"
    verdict: str = ""
    verdict_reason: str = ""
    lens: dict[str, Any] = field(default_factory=dict)
    standard_lens: dict[str, Any] = field(default_factory=dict)
    disagreement: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    confidence: dict[str, Any] = field(default_factory=dict)
    falsifiers: list[str] = field(default_factory=list)
    grounding: dict[str, Any] = field(default_factory=dict)
    corpus_hits: list[dict[str, Any]] = field(default_factory=list)
    narrative: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    version: str = VERSION
    disclaimer: str = DISCLAIMER
    # A consent or attribution line that belongs to one tenant, never to the product.
    attribution: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_markdown(self) -> str:
        """Human-readable card — the thing a person actually argues with."""
        lines = [f"# {self.instrument} — {self.mode}", "", f"**Verdict:** {self.verdict}"]
        if self.verdict_reason:
            lines.append(f"_{self.verdict_reason}_")
        if self.lens:
            lines += ["", "## Reference lens (proxy score)",
                      f"Score **{self.lens.get('score_10', '?')}/10**; "
                      f"action **{self.lens.get('action', '?')}**"]
            for c in self.lens.get("cited_rules", []):
                lines.append(f"- rule `{c.get('rule')}` — [{c.get('stamp', '')}]({c.get('url', '')})")
        if self.standard_lens:
            sl = self.standard_lens
            lines += ["", "## Standard lens", f"**{sl.get('action', '?')}** — trend {sl.get('trend', {}).get('score', '?')}, "
                      f"vol {sl.get('volatility_annualised', '?')}"]
        if self.disagreement:
            d = self.disagreement
            lines += ["", "## Disagreement", f"exists: **{d.get('exists')}**"]
            if d.get("summary"):
                lines.append(str(d["summary"]))
        if self.risk:
            r = self.risk
            lines += ["", "## Risk",
                      f"invalidation {r.get('invalidation')} · target {r.get('first_target')} · "
                      f"shares {r.get('shares')} ({r.get('pct_of_equity', 0) * 100:.1f}% of equity)"]
        if self.corpus_hits:
            lines += ["", "## Cited from the corpus"]
            for h in self.corpus_hits[:5]:
                lines.append(f"- [{h['stamp']}]({h['url']}) _{h['video_title'][:50]}_ — {h['text'][:180]}…")
        if self.falsifiers:
            lines += ["", "## What would prove this wrong"]
            lines += [f"- {f}" for f in self.falsifiers]
        lines += ["", "---"]
        if self.attribution:
            lines.append(f"_{self.attribution}_")
        lines.append(f"_{self.disclaimer}_")
        return "\n".join(lines)
