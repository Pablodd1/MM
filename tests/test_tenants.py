"""Tenancy: the properties that make this a product rather than one client's demo.

The failures these tests exist for:

* a request for an unknown tenant silently served the default tenant's data;
* two tenants' corpora sharing one index cache;
* an adapter that never passed its evaluation answering as if it had;
* recording a new candidate quietly retiring the adapter that was live;
* one odd tenant row taking `/health` down for everyone;
* the product's own surface still naming one particular investor.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import write_index
from sample_corpus import PASSAGES

from retrieval import tokens
from mmdesk import opinion, retrieval_shim, service, tenants

ROOT = Path(__file__).resolve().parent.parent
REMOTE = {"client": ("203.0.113.9", 50000)}       # a caller that is not on the host


def _seed_tenant(tmp_path: Path, tenant_id: str, text: str) -> str:
    from collections import Counter

    corpus = tmp_path / tenant_id
    corpus.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------- isolation

def test_default_tenant_ships_with_no_material_and_no_adapter():
    t = tenants.get(tenants.DEFAULT_TENANT)
    assert t is not None
    assert t.has_corpus is False
    assert t.active_adapter is None
    assert t.attribution is None


def test_corpora_do_not_leak_between_tenants(tmp_path):
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
    r = TestClient(service.app).post("/api/lens", json={"question": "what is the method"},
                                     headers={"X-Tenant": "does-not-exist"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_tenant"


def test_lens_refuses_politely_with_no_material():
    r = TestClient(service.app).post("/api/lens", json={"question": "what is the method"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert body["refusal_reason"] == "corpus_missing"
    assert "No material is loaded" in body["quote"]
    assert body["coverage"]["available"] is False


# ---------------------------------------------------------------- the adapter gate

PASSING = {"stance_agreement": 0.92, "leakage_free": True}
FAILING = {"stance_agreement": 0.61, "leakage_free": True}
LEAKY = {"stance_agreement": 0.97, "leakage_free": False}
CANDIDATE = {"path": "models/x", "base_url": "http://127.0.0.1:8899/v1", "model": "x-lens"}


def _tenant(tmp_path, tenant_id):
    return _seed_tenant(tmp_path, tenant_id, f"{tenant_id.title()} buys strength.")


def test_adapter_gate_refuses_an_unproven_candidate(tmp_path):
    tid = _tenant(tmp_path, "gamma")
    tenants.set_adapter(tid, CANDIDATE | {"eval": FAILING})
    with pytest.raises(ValueError):
        tenants.activate_adapter(tid)
    stored = tenants.get(tid)
    assert stored.active_adapter is None
    assert stored.candidate["status"] == "candidate"


def test_adapter_gate_refuses_a_leaky_evaluation(tmp_path):
    tid = _tenant(tmp_path, "delta")
    tenants.set_adapter(tid, CANDIDATE | {"eval": LEAKY})
    with pytest.raises(ValueError):
        tenants.activate_adapter(tid)


def test_adapter_gate_promotes_a_proven_candidate(tmp_path):
    tid = _tenant(tmp_path, "epsilon")
    tenants.set_adapter(tid, CANDIDATE | {"model": "epsilon-lens", "eval": PASSING})
    assert tenants.get(tid).active_adapter is None, "a candidate is not routable"
    adapter = tenants.activate_adapter(tid)
    assert adapter["status"] == "active"
    stored = tenants.get(tid)
    assert stored.active_adapter is not None
    assert stored.candidate is None, "promotion consumes the candidate"


def test_a_registered_adapter_is_always_a_candidate_whatever_it_claims(tmp_path):
    tid = _tenant(tmp_path, "kappa")
    tenants.set_adapter(tid, CANDIDATE | {"status": "active", "eval": FAILING})
    assert tenants.get(tid).active_adapter is None
    assert opinion.provider(tenants.get(tid))["kind"] == "hosted"


def test_reregister_does_not_deactivate_the_active_adapter(tmp_path):
    tid = _tenant(tmp_path, "zeta")
    tenants.set_adapter(tid, CANDIDATE | {"model": "zeta-v1", "eval": PASSING})
    tenants.activate_adapter(tid)
    tenants.set_adapter(tid, CANDIDATE | {"model": "zeta-v2", "eval": FAILING})
    stored = tenants.get(tid)
    assert stored.active_adapter["model"] == "zeta-v1", "the live adapter stays live"
    assert stored.candidate["model"] == "zeta-v2"
    with pytest.raises(ValueError):
        tenants.activate_adapter(tid)            # and the weaker candidate cannot replace it
    assert tenants.get(tid).active_adapter["model"] == "zeta-v1"


def test_opinion_layer_ignores_a_candidate_adapter(tmp_path):
    """A candidate is not routable: Layer 2 falls back to the hosted model, not to the weights."""
    tid = _tenant(tmp_path, "eta")
    tenants.set_adapter(tid, CANDIDATE | {"base_url": "http://127.0.0.1:8899/v1"})
    prov = opinion.provider(tenants.get(tid))
    assert prov["kind"] == "hosted"


def test_active_adapter_is_routed_for_its_own_tenant(tmp_path):
    tid = _tenant(tmp_path, "theta")
    tenants.set_adapter(tid, CANDIDATE | {"model": "theta-lens", "eval": PASSING})
    tenants.activate_adapter(tid)
    prov = opinion.provider(tenants.get(tid))
    assert prov["kind"] == "adapter"
    assert prov["model"] == "theta-lens"


# ---------------------------------------------------------------- the tenancy endpoints

def test_register_endpoint_cannot_store_an_active_adapter(tmp_path):
    """"status": "active" used to be accepted here, which made an adapter routable with no
    evaluation at all."""
    tid = _tenant(tmp_path, "iota")
    client = TestClient(service.app)
    r = client.post(f"/api/tenants/{tid}/adapter",
                    json=CANDIDATE | {"status": "active", "eval": FAILING})
    assert r.status_code == 200
    assert r.json()["adapter"]["status"] == "candidate"
    assert tenants.get(tid).active_adapter is None
    assert client.post(f"/api/tenants/{tid}/adapter", json={}).status_code == 422
    assert client.post(f"/api/tenants/{tid}/adapter", json={"path": "p", "base_url": "file:///etc"}
                       ).status_code == 422
    assert client.post("/api/tenants/no-such/adapter", json=CANDIDATE).status_code == 404


@pytest.mark.parametrize("base", [
    "http://169.254.169.254/latest/meta-data",     # cloud metadata
    "http://100.100.100.200/", "http://metadata.google.internal/",
    "ftp://127.0.0.1/x", "http://user:pw@10.0.0.1/v1", "not a url",
])
def test_adapter_endpoint_cannot_target_metadata_or_odd_schemes(base):
    assert opinion.endpoint_allowed(base) is False


@pytest.mark.parametrize("base", [
    "http://127.0.0.1:8899/v1", "http://localhost:8899/v1", "http://10.0.0.5:8899/v1",
    "https://adapters.example.com/v1",
])
def test_adapter_endpoint_allows_ordinary_hosts(base):
    assert opinion.endpoint_allowed(base) is True


def test_activate_endpoint_enforces_the_gate(tmp_path):
    tid = _tenant(tmp_path, "lamda")
    client = TestClient(service.app)
    tenants.set_adapter(tid, CANDIDATE | {"eval": FAILING})
    assert client.post(f"/api/tenants/{tid}/adapter/activate").status_code == 409
    assert client.post(f"/api/tenants/{tid}/adapter/activate?force=true").status_code == 403
    tenants.set_adapter(tid, CANDIDATE | {"eval": PASSING})
    assert client.post(f"/api/tenants/{tid}/adapter/activate").status_code == 200
    assert client.post("/api/tenants/no-such/adapter/activate").status_code == 404


def test_admin_routes_are_loopback_or_token(monkeypatch, tmp_path):
    client = TestClient(service.app)
    stranger = TestClient(service.app, **REMOTE)
    body = {"id": "acme", "name": "Acme"}
    assert client.post("/api/tenants", json=body).status_code == 200      # loopback
    assert stranger.post("/api/tenants", json=body | {"id": "other"}).status_code == 403

    monkeypatch.setenv("MMDESK_ADMIN_TOKEN", "operator-token")
    assert stranger.post("/api/tenants", json=body | {"id": "other"}).status_code == 401
    assert stranger.post("/api/tenants", json=body | {"id": "other"},
                         headers={"X-Admin-Token": "operator-token"}).status_code == 200
    assert stranger.post("/api/tenants", json=body,
                         headers={"X-Admin-Token": "operator-token"}).status_code == 409
    assert stranger.post(f"/api/tenants/acme/adapter", json=CANDIDATE).status_code == 401


def test_tenant_roster_is_admin_only_but_a_known_id_is_public(tmp_path):
    _tenant(tmp_path, "mu")
    client = TestClient(service.app)
    stranger = TestClient(service.app, **REMOTE)

    roster = stranger.get("/api/tenants")
    assert roster.status_code == 403
    health = stranger.get("/health").json()
    assert isinstance(health["tenants"], int), "the roster is not published with health"

    known = stranger.get("/api/tenants/mu")
    assert known.status_code == 200
    assert known.json()["id"] == "mu"
    assert "corpus_dir" not in known.json() and "path" not in known.json()["corpus"]
    assert stranger.get("/api/tenants/nobody").status_code == 404
    assert [t["id"] for t in client.get("/api/tenants").json()["tenants"]]  # loopback sees it


def test_creating_a_tenant_on_an_ephemeral_registry_is_refused(monkeypatch):
    """On a serverless host the registry is per-instance scratch: a tenant created there 404s on
    the next instance. Refusing beats losing it quietly."""
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("MMDESK_ADMIN_TOKEN", "operator-token")   # authorised, and still refused
    r = TestClient(service.app).post("/api/tenants", json={"id": "acme", "name": "Acme"},
                                     headers={"X-Admin-Token": "operator-token"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "registry_ephemeral"
    assert TestClient(service.app).get("/health").json()["registry_persistent"] is False


def test_health_reports_persistence_and_no_filesystem_paths(tmp_path):
    body = TestClient(service.app).get("/health").json()
    assert body["ok"] is True
    assert body["registry_persistent"] is True and body["journal_persistent"] is True
    assert "data_dir" not in body
    assert "corpus_coverage" in body and "read_from" not in body["corpus_coverage"]
    assert body["llm"]["configured"] is False and body["llm"]["layer2"] == "template"


def test_health_and_tenant_list_survive_an_external_corpus_dir(tmp_path):
    """A corpus outside the repository used to raise in `public()`, taking both endpoints down."""
    _seed_tenant(tmp_path, "nu", "Nu buys strength.")
    _seed_tenant(tmp_path, "xi", "Xi sells weakness.")
    client = TestClient(service.app)
    assert client.get("/health").status_code == 200
    listing = client.get("/api/tenants")
    assert listing.status_code == 200
    assert {t["id"] for t in listing.json()["tenants"]} >= {"default", "nu", "xi"}


def test_tenant_ids_cannot_collide_by_case(tmp_path):
    """"Acme" and "acme" are one directory and one journal file on a case-insensitive disk."""
    with pytest.raises(ValueError):
        tenants.create("Acme", "Acme")
    r = TestClient(service.app).post("/api/tenants", json={"id": "Acme", "name": "Acme"})
    assert r.status_code == 422
    for bad in ("../x", "a", "x" * 33, "with space"):
        assert TestClient(service.app).post("/api/tenants",
                                            json={"id": bad, "name": "n"}).status_code == 422


def test_attribution_is_a_property_of_one_tenant(tmp_path):
    tid = "consented"
    line = "Built with Example Author's permission; they are not responsible for its output."
    tenants.create(tid, "Example lens", "Example lens", line)
    _seed_tenant(tmp_path, tid, "Example buys strength.")
    public = TestClient(service.app).get(f"/api/tenants/{tid}").json()
    assert public["attribution"] == line
    assert tenants.get(tenants.DEFAULT_TENANT).attribution is None


# ---------------------------------------------------------------- robustness of the registry itself

def test_the_default_tenant_survives_concurrent_first_requests(monkeypatch, tmp_path):
    """`ensure_default` was select-then-insert. On a cold instance the first requests overlap and
    two of them raced to create the same row."""
    monkeypatch.setenv("MMDESK_DB", str(tmp_path / "fresh" / "desk.db"))
    errors: list[BaseException] = []

    def hammer():
        try:
            for _ in range(40):
                assert tenants.get(tenants.DEFAULT_TENANT) is not None
        except BaseException as exc:  # noqa: BLE001 - reported through `errors`
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert len(tenants.all_tenants()) == 1


def test_a_registry_from_before_the_candidate_column_still_works(monkeypatch, tmp_path):
    path = tmp_path / "old" / "desk.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, lens_label TEXT NOT NULL,
            corpus_dir TEXT NOT NULL, adapter TEXT, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL);
        CREATE TABLE decisions (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, ticker TEXT,
            verdict TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX idx_decisions_tenant ON decisions(tenant_id, created_at);
        INSERT INTO tenants VALUES ('legacy', 'Legacy', 'Legacy lens', 'corpus/legacy', NULL, 1,
            '2026-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setenv("MMDESK_DB", str(path))

    assert tenants.get("legacy") is not None
    tenants.set_adapter("legacy", CANDIDATE | {"eval": PASSING})
    assert tenants.get("legacy").candidate["status"] == "candidate"
    tenants.activate_adapter("legacy")
    assert tenants.get("legacy").active_adapter is not None


def test_an_unwritable_default_directory_falls_back_instead_of_failing(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "data").write_text("in the way", encoding="utf-8")      # a file where the dir goes
    monkeypatch.setattr(tenants, "ROOT", root)
    monkeypatch.setattr(tenants, "_root_cache", {})
    monkeypatch.setattr(tenants, "_root_fell_back", False)
    monkeypatch.delenv("MMDESK_DB")                                  # use the default location
    monkeypatch.delenv("MMDESK_DATA_DIR")
    fallback = tmp_path / "fallback-tmp"
    monkeypatch.setattr(tenants.tempfile, "gettempdir", lambda: str(fallback))

    assert tenants.get(tenants.DEFAULT_TENANT) is not None
    assert tenants.db_path().is_relative_to(fallback)
    assert tenants.persistent() is False


# ---------------------------------------------------------------- the surface names no one person

SURFACE = ("service.py", "card.py", "decision.py", "rules.py", "rubric.py",
           "persona.py", "llm.py", "opinion.py", "voice.py", "scenarios.py")


def _prose(path: Path) -> list[tuple[int, str]]:
    """Every string literal that is not a docstring: these reach a reader or a model."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def test_product_surface_does_not_name_one_investor():
    """The build is generic. A name in the output is not a style choice; it is a consent claim,
    and one that belongs to a single tenant. Attribution is a property of the tenant record."""
    page = (ROOT / "mmdesk" / "static" / "index.html").read_text(encoding="utf-8")
    lowered = page.lower()
    for banned in ("mark gomes", "money mark", "mm desk", "mm lens", "mm opinion"):
        assert banned not in lowered, banned

    for name in SURFACE:
        for lineno, text in _prose(ROOT / "mmdesk" / name):
            low = text.lower()
            assert not re.search(r"\b(he|his|him)\b", low), f"{name}:{lineno} attributes to 'he'"
            for banned in ("mark gomes", "money mark", "mm desk", "authoret"):
                assert banned not in low, f"{name}:{lineno} names {banned}"
