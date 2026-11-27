"""Where a model key is allowed to go, and what may come back.

The two prose layers used to resolve their provider separately: the narrative inferred the
vendor from the key's prefix, the opinion defaulted to one fixed vendor. With any key that was
not that vendor's, every "ask for the opinion" sent the key to a competitor.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import pytest

from mmdesk import llm, opinion, tenants

CARD = {
    "instrument": "NVDA", "mode": "swing", "verdict": "hold",
    "verdict_reason": "score six", "lens": {"score_10": 6, "action": "hold"},
    "standard_lens": {"action": "mixed"}, "risk": {"shares": 4}, "disagreement": {},
    "falsifiers": ["a close below the stop"],
}

CLEAN = json.dumps({
    "stance": "hold",
    "verdict_reason": "the odds are fair",
    "analysis": "the disciplined read is that the odds are fair",
    "reasoning": "the disciplined read is that the odds are fair",
    "lens_narrative": "a cited rule drives it",
    "lens_opinion": "the material would wait for a better price",
    "standard_narrative": "the tape is undecided",
    "disagreement": "the two voices broadly agree",
    "agreement": "the two voices broadly agree",
    "caveat": "a close below the invalidation level",
    "falsifier": "a close below the invalidation level",
})


class Reply:
    status_code = 200

    def __init__(self, content: str) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self.content}}]}


def record_calls(monkeypatch, contents: list[str] | None = None):
    """Replace `httpx.post`; return the list of (url, authorization) pairs it saw."""
    calls: list[tuple[str, str | None]] = []
    queue = list(contents or [CLEAN])

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append((url, (headers or {}).get("Authorization")))
        return Reply(queue.pop(0) if len(queue) > 1 else queue[0])

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def refuse_calls(monkeypatch):
    def no_call(url, **kwargs):
        raise AssertionError(f"unexpected model call to {url}")

    monkeypatch.setattr(httpx, "post", no_call)


@pytest.mark.parametrize("key,host", [
    ("xai-" + "1" * 20, "api.x.ai"),
    ("gsk_" + "2" * 20, "api.groq.com"),
    ("sk-or-" + "3" * 20, "openrouter.ai"),
])
def test_a_key_only_ever_goes_to_its_own_vendor(monkeypatch, key, host):
    monkeypatch.setenv("MMDESK_LLM_KEY", key)
    monkeypatch.setenv("MMDESK_ALLOW_CLOUD_LLM", "1")
    calls = record_calls(monkeypatch)
    llm.narrate(CARD, [])
    opinion.opinion(CARD, [])
    assert len(calls) == 2, "both layers called once"
    for url, authorization in calls:
        assert urlsplit(url).hostname == host
        assert authorization == f"Bearer {key}"


def test_a_legacy_deepseek_key_reaches_deepseek_from_both_layers(monkeypatch):
    """It used to work for the narrative and read as "no key configured" for the opinion."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("MMDESK_ALLOW_CLOUD_LLM", "1")
    calls = record_calls(monkeypatch)
    narrative = llm.narrate(CARD, [])
    assert "api.deepseek.com (attempt 1)" in narrative["generated_by"]   # a real call
    assert "template" not in opinion.opinion(CARD, [])["generated_by"]
    assert {urlsplit(url).hostname for url, _ in calls} == {"api.deepseek.com"}


def test_an_unrecognised_key_reaches_no_vendor(monkeypatch):
    """"sk-" is shared by several vendors, so the prefix alone does not identify one."""
    monkeypatch.setenv("MMDESK_LLM_KEY", "sk-unknown-vendor")
    monkeypatch.setenv("MMDESK_ALLOW_CLOUD_LLM", "1")
    refuse_calls(monkeypatch)
    out = llm.narrate(CARD, [])
    assert "MMDESK_LLM_BASE" in out["generated_by"]
    out = opinion.opinion(CARD, [])
    assert "MMDESK_LLM_BASE" in out["generated_by"]

    monkeypatch.setenv("MMDESK_LLM_BASE", "https://llm.example-vendor.test/v1")
    monkeypatch.setenv("MMDESK_LLM_MODEL", "vendor-model")
    calls = record_calls(monkeypatch)
    llm.narrate(CARD, [])
    opinion.opinion(CARD, [])
    assert {urlsplit(url).hostname for url, _ in calls} == {"llm.example-vendor.test"}


def test_a_named_endpoint_overrides_the_prefix_for_both_layers(monkeypatch):
    """An operator who points the base at a local server must get that server in both layers."""
    monkeypatch.setenv("MMDESK_LLM_KEY", "local")
    monkeypatch.setenv("MMDESK_LLM_BASE", "http://127.0.0.1:8899/v1")
    monkeypatch.setenv("MMDESK_ALLOW_CLOUD_LLM", "1")
    calls = record_calls(monkeypatch)
    llm.narrate(CARD, [])
    opinion.opinion(CARD, [])
    assert {url for url, _ in calls} == {"http://127.0.0.1:8899/v1/chat/completions"}


# ---------------------------------------------------------------- what may come back

def _tenant_with_adapter(monkeypatch, base_url="http://127.0.0.1:8899/v1") -> tenants.Tenant:
    tid = "adaptered"
    if tenants.get(tid) is None:
        tenants.create(tid, "Adapted lens")
    tenants.set_adapter(tid, {"path": "models/x", "model": "x-lens", "base_url": base_url,
                              "eval": {"stance_agreement": 0.95, "leakage_free": True}})
    tenants.activate_adapter(tid)
    return tenants.get(tid)


def test_adapter_prose_is_read_under_the_names_the_adapter_was_trained_on(monkeypatch):
    """The training schema writes `reasoning`; inference read only `analysis`. The prose arrived
    under a key nobody read — and so was never checked for digits."""
    trained = json.dumps({"stance": "hold", "reasoning": "the disciplined read is fair",
                          "falsifier": "a close below the stop"})
    record_calls(monkeypatch, [trained])
    out = opinion.opinion(CARD, [], tenant=_tenant_with_adapter(monkeypatch))
    assert out["analysis"] == "the disciplined read is fair"
    assert out["falsifier"] == "a close below the stop"
    assert out["generated_by"] == "adapter adaptered"


def test_a_digit_anywhere_in_the_completion_is_caught_whatever_the_key(monkeypatch):
    trained = json.dumps({"stance": "hold", "reasoning": "I would risk 3 percent here"})
    record_calls(monkeypatch, [trained])            # every attempt carries the digit
    out = opinion.opinion(CARD, [], tenant=_tenant_with_adapter(monkeypatch))
    assert "template" in out["generated_by"]
    assert "3 percent" not in json.dumps(out)


def test_a_digit_in_a_field_nobody_renders_still_triggers_the_retry(monkeypatch):
    first = json.dumps({"stance": "hold", "analysis": "fair", "note_to_self": "buy 100"})
    second = json.dumps({"stance": "hold", "analysis": "fair odds, no edge worth adding"})
    calls = record_calls(monkeypatch, [first, second])
    out = opinion.opinion(CARD, [], tenant=_tenant_with_adapter(monkeypatch))
    assert len(calls) == 2
    assert out["analysis"] == "fair odds, no edge worth adding"
    assert "100" not in json.dumps(out)


def test_an_out_of_enum_stance_falls_back_to_the_computed_verdict(monkeypatch):
    odd = json.dumps({"stance": "YOLO", "analysis": "fair"})
    record_calls(monkeypatch, [odd])
    assert opinion.opinion(CARD, [], tenant=_tenant_with_adapter(monkeypatch))["stance"] == "hold"


def test_a_completion_with_none_of_the_expected_fields_is_not_passed_off_as_prose(monkeypatch):
    record_calls(monkeypatch, [json.dumps({"unrelated": "text"})])
    out = opinion.opinion(CARD, [], tenant=_tenant_with_adapter(monkeypatch))
    assert "template" in out["generated_by"]


def test_failure_text_never_carries_the_endpoint_or_a_key(monkeypatch):
    """`generated_by` used to return the raw exception, URL and provider message included, to
    every caller."""
    secret = "http://10.0.0.5:8899/v1/chat/completions key sk-live-secret-value"

    def failing_post(url, **kwargs):
        raise RuntimeError(f"connect failed for '{secret}'")

    monkeypatch.setattr(httpx, "post", failing_post)
    monkeypatch.setenv("MMDESK_LLM_KEY", "xai-" + "1" * 20)      # so the narrative layer calls
    monkeypatch.setenv("MMDESK_ALLOW_CLOUD_LLM", "1")
    for out in (llm.narrate(CARD, []),
                opinion.opinion(CARD, [], tenant=_tenant_with_adapter(monkeypatch))):
        text = json.dumps(out)
        assert "10.0.0.5" not in text and "sk-live-secret-value" not in text
        assert "RuntimeError" in out["generated_by"]


def test_an_adapter_pointed_at_the_metadata_service_is_never_called(monkeypatch):
    tenant = _tenant_with_adapter(monkeypatch, base_url="http://169.254.169.254/latest")
    refuse_calls(monkeypatch)
    assert "endpoint refused" in opinion.opinion(CARD, [], tenant=tenant)["generated_by"]


def test_a_tenant_without_an_adapter_never_reaches_another_tenants_adapter(monkeypatch):
    _tenant_with_adapter(monkeypatch)
    stranger = tenants.create("strangerlens", "Stranger lens")
    assert opinion.provider(stranger)["kind"] == "hosted"
