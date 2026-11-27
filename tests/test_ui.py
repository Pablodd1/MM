"""The page and the API have to agree on field names.

The last rewrite of the card renamed `mm_lens` to `lens` and the prose keys to `lens_*`. The page
kept reading the old names, so every card rendered an empty lens block and an empty second
opinion — and no test noticed, because nothing exercised the page. These tests read the page as
text and check it against a real response.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mmdesk import service

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "mmdesk" / "static" / "index.html").read_text(encoding="utf-8")

# Every field the page reads, and that the API must therefore return for an ordinary card.
CARD_FIELDS = (
    "verdict", "instrument", "mode", "verdict_reason", "disclaimer",
    "lens.score_10", "lens.action", "lens.position_pct", "lens.shares", "lens.label",
    "lens.score_meta.source", "lens.posture",
    "standard_lens.action", "standard_lens.trend.score", "standard_lens.price",
    "standard_lens.volatility_20d_annualised", "standard_lens.volatility_60d_annualised",
    "standard_lens.atr", "standard_lens.charts_supported", "standard_lens.measures_evaluated",
    "disagreement.exists", "disagreement.summary", "disagreement.tie_break",
    "risk.invalidation", "risk.first_target", "risk.reward_risk", "risk.risk_at_stop",
    "risk.risk_budget",
    "grounding.price_bar", "grounding.as_of", "grounding.level_notes", "grounding.missing",
    "corpus_hits", "falsifiers",
    "narrative.lens_narrative", "narrative.standard_narrative", "narrative.generated_by",
)
OPINION_FIELDS = ("stance", "analysis", "lens_opinion", "agreement", "falsifier",
                  "generated_by", "citations")
CROWD_FIELDS = ("label", "agents", "modal_stance", "dispersion", "views", "what_this_is_not")


def _get(payload: dict, path: str):
    value = payload
    for part in path.split("."):
        assert isinstance(value, dict) and part in value, f"{path} is missing at {part!r}"
        value = value[part]
    return value


def test_every_field_the_page_reads_is_in_the_response(stub_market):
    client = TestClient(service.app)
    card = client.post("/api/analyze", json={"ticker": "MSFT", "question": "is it a buy"}).json()
    for path in CARD_FIELDS:
        assert _get(card, path) is not None, path
    opinion = client.post("/api/opinion", json={"ticker": "MSFT", "question": "is it a buy"}).json()
    for path in OPINION_FIELDS:
        assert _get(opinion, path) is not None, path
    crowd = client.post("/api/scenario", json={"ticker": "MSFT"}).json()
    for path in CROWD_FIELDS:
        assert _get(crowd, path) is not None, path


def test_the_page_reads_those_fields_by_their_current_names():
    for token in ("d.lens", "lens.score_10", "lens.score_meta", "n.lens_narrative",
                  "n.standard_narrative", "d.lens_opinion", "d.generated_by", "g.price_bar",
                  "d.corpus_refusal", "g.level_notes", "sl.measures_evaluated",
                  "r.reward_risk_gross", "d.attribution", "h.synthetic"):
        assert token in PAGE, token
    for stale in ("mm_lens", "mm_opinion", "mm_narrative", "35 hours",
                  "run locally on this machine"):
        assert stale not in PAGE, stale
    assert not re.search(r"\b(Mark|Gomes|he|his|him)\b", PAGE), "the page attributes to a person"


def test_the_default_form_is_submittable():
    """`value="50000" min="1" step="1000"` is a step mismatch, so the browser refused to submit
    the page's own defaults. A number input's valid values are `min + k * step`."""
    for attrs in re.findall(r"<input\b[^>]*type=\"number\"[^>]*>", PAGE):
        get = lambda name: re.search(name + r'="([^"]*)"', attrs)
        value, minimum, step = get("value"), get("min"), get("step")
        if not value or not minimum:
            continue
        increment = 1.0 if not step or step.group(1) == "any" else float(step.group(1))
        offset = float(value.group(1)) - float(minimum.group(1))
        assert offset % increment == 0 or abs(offset % increment - increment) < 1e-9, attrs


def test_every_control_has_a_label():
    ids = re.findall(r'<(?:input|select|textarea)\b[^>]*\bid="([^"]+)"', PAGE)
    assert ids
    for control_id in ids:
        assert f'for="{control_id}"' in PAGE, f"no label for #{control_id}"
    for banned in ("mark gomes", "money mark", "mm desk", "authoret"):
        assert banned not in PAGE.lower()
    assert "Hold to talk" not in PAGE, "the control toggles; call it what it does"


def test_dynamic_text_is_never_written_as_markup():
    """Model prose and tenant material reach the page only as text nodes."""
    for assignment in re.finditer(r"\.innerHTML\s*=\s*([^;]+);", PAGE):
        assert assignment.group(1).strip() in ("''", '""'), assignment.group(0)
    assert "insertAdjacentHTML" not in PAGE and "document.write" not in PAGE
    assert "outerHTML" not in PAGE


def test_links_from_tenant_material_are_limited_to_http():
    assert "function safeUrl" in PAGE
    assert re.search(r"\^https\?:\\/\\//i\.test", PAGE), "the scheme check must be anchored"
    # Every href taken from a response goes through it.
    calls = [line for line in PAGE.splitlines() if "link(" in line and "function link" not in line]
    assert len(calls) == 2
    for call in calls:
        assert "safeUrl(" in call, call


def test_the_page_has_the_accessibility_basics():
    assert '<html lang="en"' in PAGE
    assert 'aria-live="polite"' in PAGE
    assert 'role="status"' in PAGE
    assert ":focus-visible" in PAGE
    assert "min-height:44px" in PAGE
    assert "prefers-reduced-motion" in PAGE
    assert "<noscript>" in PAGE
    assert "tabindex=\"-1\"" in PAGE and "out.focus()" in PAGE


def test_every_request_from_the_page_declares_its_body_and_its_journal_scope():
    """Found in a real browser, by nothing else. The page's request helper sent no Content-Type,
    so the server could not read the JSON body and refused every Analyse with a 422 — and it did
    not send the journal scope, so the panel never would have shown the browser's own calls. The
    test client sets the header itself, which is why no other test caught it."""
    helper = PAGE[PAGE.index("async function api("):PAGE.index("function explain(")]
    assert "'Content-Type'" in helper and "application/json" in helper
    assert "'X-Journal-Id': JOURNAL_ID" in helper
    sent = helper[helper.index("fetch(url(path)"):]
    assert "headers" in sent[:160], "the headers must reach fetch itself, not stop in a variable"


@pytest.mark.parametrize("path", ["/", "/health"])
def test_the_page_and_health_are_served(path):
    r = TestClient(service.app).get(path)
    assert r.status_code == 200
    if path == "/":
        assert "Decision Desk" in r.text and r.headers["content-type"].startswith("text/html")
