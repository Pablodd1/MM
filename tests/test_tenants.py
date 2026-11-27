"""Tenancy: the properties that make this a product rather than one client's demo.

The failures these tests exist for:

* a request for an unknown tenant silently served the default tenant's data;
* two tenants' corpora sharing one index cache;
* an adapter that never passed its evaluation answering as if it had;
* the product's own surface still naming one particular investor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _seed_tenant(tmp_path: Path, tenant_id: str, text: str) -> str:
    from mmdesk import tenants

    corpus = tmp_path / tenant_id
    corpus.mkdir(parents=True, exist_ok=True)
    from collections import Counter

    from retrieval import tokens

    rows = [{"id": f"{tenant_id}-65", "video_id": f"{tenant_id}01",
             "video_title": f"{tenant_id} note", "start_s": 65, "stamp": "00:01:05",
             "text": text, "url": f"https://example.invalid/{tenant_id}",
             "tf": dict(Counter(tokens(text)))}]
    (corpus / "passages.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    if tenants.get(tenant_id) is None:
        tenants.create(tenant_id, tenant_id.title())
    conn = tenants._connect()
    try:
        conn.execute("UPDATE tenants SET corpus_dir = ? WHERE id = ?", (str(corpus), tenant_id))
        conn.commit()
    finally:
        conn.close()
    return tenant_id


def test_default_tenant_ships_with_no_material_and_no_adapter():
    from mmdesk import tenants

    t = tenants.get(tenants.DEFAULT_TENANT)
    assert t is not None
    assert t.has_corpus is False
    assert t.active_adapter is None


def test_corpora_do_not_leak_between_tenants(tmp_path):
    from mmdesk import retrieval_shim

    a = _seed_tenant(tmp_path, "alpha", "Alpha's method buys strength into a rising trend.")
    b = _seed_tenant(tmp_path, "beta", "Beta's method sells weakness when volatility expands.")
    retrieval_shim.reset_cache()

    hits_a = retrieval_shim.search("method strength trend", tenant_id=a)
    hits_b = retrieval_shim.search("method weakness volatility", tenant_id=b)

    assert hits_a and hits_a[0]["video_id"].startswith("alpha")
    assert hits_b and hits_b[0]["video_id"].startswith("beta")
    assert all(h["video_id"].startswith("alpha") for h in hits_a)
    assert all(h["video_id"].startswith("beta") for h in hits_b)

    # A word that only one tenant uses cannot be answered from the other tenant's material.
    assert retrieval_shim.search("strength", tenant_id=b) == []


def test_unknown_tenant_is_refused_not_defaulted():
    from fastapi.testclient import TestClient

    from mmdesk import service

    r = TestClient(service.app).post("/api/lens", json={"question": "what is the method"},
                                     headers={"X-Tenant": "does-not-exist"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_tenant"


def test_lens_refuses_politely_with_no_material():
    from fastapi.testclient import TestClient

    from mmdesk import service

    r = TestClient(service.app).post("/api/lens", json={"question": "what is the method"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert body["coverage"]["available"] is False


def test_adapter_gate_refuses_an_unproven_candidate(tmp_path):
    from mmdesk import tenants

    tid = _seed_tenant(tmp_path, "gamma", "Gamma buys strength.")
    tenants.set_adapter(tid, {"status": "candidate", "path": "/tmp/nope",
                              "eval": {"stance_agreement": 0.61, "leakage_free": True}})
    with pytest.raises(ValueError):
        tenants.activate_adapter(tid)
    assert tenants.get(tid).active_adapter is None
    assert tenants.get(tid).adapter["status"] == "candidate"


def test_adapter_gate_refuses_a_leaky_evaluation(tmp_path):
    from mmdesk import tenants

    tid = _seed_tenant(tmp_path, "delta", "Delta buys strength.")
    tenants.set_adapter(tid, {"status": "candidate", "path": "/tmp/nope",
                              "eval": {"stance_agreement": 0.97, "leakage_free": False}})
    with pytest.raises(ValueError):
        tenants.activate_adapter(tid)


def test_adapter_gate_promotes_a_proven_candidate(tmp_path):
    from mmdesk import tenants

    tid = _seed_tenant(tmp_path, "epsilon", "Epsilon buys strength.")
    tenants.set_adapter(tid, {"status": "candidate", "path": "models/epsilon",
                              "model": "epsilon-lens", "base_url": "http://127.0.0.1:8899/v1",
                              "eval": {"stance_agreement": 0.92, "leakage_free": True}})
    adapter = tenants.activate_adapter(tid)
    assert adapter["status"] == "active"
    assert tenants.get(tid).active_adapter is not None


def test_opinion_layer_ignores_a_candidate_adapter(tmp_path):
    """A candidate is not routable: Layer 2 falls back to the hosted model, not to the weights."""
    from mmdesk import opinion, tenants

    tid = _seed_tenant(tmp_path, "zeta", "Zeta buys strength.")
    tenants.set_adapter(tid, {"status": "candidate", "path": "models/zeta",
                              "base_url": "http://127.0.0.1:8899/v1"})
    prov = opinion.provider(tenants.get(tid))
    assert prov["kind"] == "hosted"


def test_active_adapter_is_routed_for_its_own_tenant(tmp_path):
    from mmdesk import opinion, tenants

    tid = _seed_tenant(tmp_path, "eta", "Eta buys strength.")
    tenants.set_adapter(tid, {"status": "active", "path": "models/eta", "model": "eta-lens",
                              "base_url": "http://127.0.0.1:8899/v1",
                              "eval": {"stance_agreement": 0.9, "leakage_free": True}})
    prov = opinion.provider(tenants.get(tid))
    assert prov["kind"] == "adapter"
    assert prov["model"] == "eta-lens"


def test_product_surface_does_not_name_one_investor():
    """The build is generic. A name in the UI is not a style choice; it is a consent claim."""
    surface = (ROOT / "mmdesk" / "static" / "index.html").read_text(encoding="utf-8")
    service = (ROOT / "mmdesk" / "service.py").read_text(encoding="utf-8")
    card = (ROOT / "mmdesk" / "card.py").read_text(encoding="utf-8")
    for text in (surface, service, card):
        lowered = text.lower()
        assert "mark gomes" not in lowered
        assert "money mark" not in lowered
        assert "mm desk" not in lowered
