"""The deterministic narrative: what the card says when no model wrote it."""

from __future__ import annotations

from mmdesk.llm import template_narrative
from mmdesk.standard import Bars, standard_lens


def test_template_narrative_does_not_count_measures_that_were_not_read():
    """No valuation history is supplied in production, so only two of the three measures
    exist; the narrative used to say "2 of 3" regardless."""
    closes = [80 + i * 0.5 for i in range(210)]
    lens = standard_lens(Bars(close=closes, source="test"))
    assert lens["measures_evaluated"] == 2
    out = template_narrative({"standard_lens": lens})
    assert "2 of 2 measures" in out["standard_narrative"]


def test_template_narrative_calls_a_flat_trend_flat():
    """A trend score of zero used to be narrated as negative."""
    flat = {"trend": {"score": 0}, "action": "mixed", "charts_supported": 1,
            "measures_evaluated": 2}
    out = template_narrative({"standard_lens": flat})
    assert "trend flat" in out["standard_narrative"]
    assert "negative" not in out["standard_narrative"]
