"""Tenants: the boundary this app was missing.

Everything the product used to assume — one corpus, one lens, one journal — is per-tenant state.
The default tenant ships with **no corpus and no adapter**: the deterministic engine, the standard
lens and the template narrative are a complete product on their own. A tenant becomes someone in
particular only when they upload material (the lens) and, optionally, train a Layer 2 adapter.

Two rules this module exists to enforce:

1. A tenant id is never inferred from a model or a corpus path. It comes from the request (the
   ``X-Tenant`` header or ``?tenant=``), is looked up in the registry, and unknown ids are refused.
2. An adapter is inert until its evaluation gate passes. Storing a candidate and activating one are
   different operations, because "we trained something" is not "it is better".
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TENANT = "default"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    lens_label  TEXT NOT NULL,
    corpus_dir  TEXT NOT NULL,
    adapter     TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    ticker      TEXT,
    verdict     TEXT,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_tenant ON decisions(tenant_id, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    override = os.getenv("MMDESK_DB")
    if override:
        return Path(override)
    return Path(os.getenv("MMDESK_DATA_DIR", str(ROOT / "data"))) / "desk.db"


def persistent() -> bool:
    """A SQLite file under /tmp is not storage; say so instead of pretending."""
    try:
        p = db_path().resolve()
        return "tmp" not in p.parts[:3] and str(p).replace("\\", "/").startswith(str(ROOT).replace("\\", "/"))
    except Exception:
        return False


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


@dataclass
class Tenant:
    id: str
    name: str
    lens_label: str
    corpus_dir: str
    adapter: dict[str, Any] | None = None
    active: bool = True
    created_at: str = field(default_factory=_now)

    @property
    def corpus_path(self) -> Path:
        p = Path(self.corpus_dir)
        return p if p.is_absolute() else ROOT / p

    @property
    def passages_file(self) -> Path:
        return self.corpus_path / "passages.jsonl"

    @property
    def has_corpus(self) -> bool:
        return self.passages_file.is_file()

    @property
    def active_adapter(self) -> dict[str, Any] | None:
        a = self.adapter or {}
        return a if a.get("status") == "active" and a.get("path") else None

    def journal_path(self) -> Path:
        base = Path(os.getenv("MMDESK_DATA_DIR", str(ROOT / "data"))) / "journal"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{self.id}.jsonl"

    def public(self) -> dict[str, Any]:
        a = self.adapter or {}
        return {
            "id": self.id, "name": self.name, "lens_label": self.lens_label,
            "corpus": {"available": self.has_corpus, "path": str(self.passages_file.relative_to(ROOT)) if self.has_corpus else None},
            "adapter": {"status": a.get("status"), "base": a.get("base"), "trained_at": a.get("trained_at"),
                        "eval": a.get("eval")} if a else None,
        }


def ensure_default() -> None:
    conn = _connect()
    try:
        row = conn.execute("SELECT id FROM tenants WHERE id = ?", (DEFAULT_TENANT,)).fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO tenants (id, name, lens_label, corpus_dir, adapter, active, created_at)"
            " VALUES (?, ?, ?, ?, NULL, 1, ?)",
            (DEFAULT_TENANT, "Decision Desk", "House lens",
             f"corpus/{DEFAULT_TENANT}", _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_tenant(row: sqlite3.Row) -> Tenant:
    adapter = json.loads(row["adapter"]) if row["adapter"] else None
    return Tenant(id=row["id"], name=row["name"], lens_label=row["lens_label"],
                  corpus_dir=row["corpus_dir"], adapter=adapter, active=bool(row["active"]),
                  created_at=row["created_at"])


def get(tenant_id: str) -> Tenant | None:
    ensure_default()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tenants WHERE id = ? AND active = 1", (tenant_id,)).fetchone()
        return _row_to_tenant(row) if row else None
    finally:
        conn.close()


def all_tenants() -> list[Tenant]:
    ensure_default()
    conn = _connect()
    try:
        return [_row_to_tenant(r) for r in conn.execute("SELECT * FROM tenants ORDER BY created_at")]
    finally:
        conn.close()


def create(tenant_id: str, name: str, lens_label: str | None = None) -> Tenant:
    ensure_default()
    if not tenant_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("tenant id must be alphanumeric, dash or underscore")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO tenants (id, name, lens_label, corpus_dir, adapter, active, created_at)"
            " VALUES (?, ?, ?, ?, NULL, 1, ?)",
            (tenant_id, name, lens_label or f"{name} lens", f"corpus/{tenant_id}", _now()),
        )
        conn.commit()
    finally:
        conn.close()
    t = get(tenant_id)
    assert t is not None
    return t


def set_adapter(tenant_id: str, adapter: dict[str, Any]) -> None:
    """Store a trained candidate. It stays inert until activated."""
    conn = _connect()
    try:
        cur = conn.execute("UPDATE tenants SET adapter = ? WHERE id = ?",
                           (json.dumps(adapter), tenant_id))
        if cur.rowcount == 0:
            raise KeyError(f"unknown tenant {tenant_id}")
        conn.commit()
    finally:
        conn.close()


def activate_adapter(tenant_id: str, require_gate: bool = True) -> dict[str, Any]:
    """Flip a candidate to active only if its own recorded evaluation clears the bar.

    The gate lives here, not in the training script, because the training script is the party with
    an incentive to declare success.
    """
    t = get(tenant_id)
    if t is None:
        raise KeyError(f"unknown tenant {tenant_id}")
    adapter = dict(t.adapter or {})
    if not adapter:
        raise ValueError("no adapter candidate recorded")
    if require_gate:
        ev = adapter.get("eval") or {}
        agreement = ev.get("stance_agreement")
        leak = ev.get("leakage_free", False)
        if agreement is None or agreement < 0.85 or not leak:
            raise ValueError(
                f"adapter did not clear the gate: stance_agreement={agreement}, leakage_free={leak}"
            )
    adapter["status"] = "active"
    adapter["activated_at"] = _now()
    set_adapter(tenant_id, adapter)
    return adapter


def clear_adapter(tenant_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE tenants SET adapter = NULL WHERE id = ?", (tenant_id,))
        conn.commit()
    finally:
        conn.close()


def record_decision(tenant_id: str, decision_id: str, ticker: str, verdict: str,
                    payload: dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO decisions (id, tenant_id, ticker, verdict, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, tenant_id, ticker, verdict, json.dumps(payload, default=str), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def decisions(tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, ticker, verdict, created_at FROM decisions WHERE tenant_id = ?"
            " ORDER BY created_at DESC LIMIT ?", (tenant_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
