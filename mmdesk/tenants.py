"""Tenants: the boundary this app was missing.

Everything the product used to assume — one corpus, one lens, one journal — is per-tenant state.
The default tenant ships with **no corpus and no adapter**: the deterministic engine, the standard
lens and the template narrative are a complete product on their own. A tenant becomes someone in
particular only when it uploads material (the lens) and, optionally, trains a Layer 2 adapter.

Three rules this module exists to enforce:

1. A tenant id is never inferred from a model or a corpus path. It comes from the request (the
   ``X-Tenant`` header or ``?tenant=``), is looked up in the registry, and unknown ids are refused.
2. An adapter is inert until its evaluation gate passes. Storing a candidate and activating one are
   different operations, because "we trained something" is not "it is better". Recording a candidate
   never touches the adapter that is live.
3. Every path derived from an id is validated first, so an id can never name a file outside the
   ones this module intends.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TENANT = "default"

log = logging.getLogger("mmdesk.tenants")

# Lower-case only. On a case-insensitive filesystem "Acme" and "acme" are one directory and one
# journal file, while the registry would treat them as two tenants.
TENANT_ID = re.compile(r"^[a-z0-9_-]{2,32}$")
# A journal scope is a label the browser chooses to keep its own calls in their own file.
JOURNAL_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_BUSY_TIMEOUT_S = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    lens_label  TEXT NOT NULL,
    corpus_dir  TEXT NOT NULL,
    adapter     TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    candidate   TEXT,
    attribution TEXT
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

# Columns added after the first release; `ALTER TABLE` fills in registries created before them.
_ADDED_COLUMNS = ("candidate", "attribution")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_root_cache: dict[tuple[str | None, str], Path] = {}
_root_fell_back = False


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _data_root() -> Path:
    """Where writable state lives.

    A serverless bundle is read-only except for a per-instance temp dir, so the registry has to
    follow the same rule as the journal — otherwise the first request on a deployment raises
    "attempt to write a readonly database" instead of answering. If the default location is not
    writable on some other host, fall back to the OS temp dir and say so in the log rather than
    failing every request.
    """
    global _root_fell_back
    override = os.getenv("MMDESK_DATA_DIR")
    on_vercel = os.getenv("VERCEL", "")
    key = (override, on_vercel)
    cached = _root_cache.get(key)
    if cached is not None:
        return cached
    if override:
        root = Path(override)
    elif on_vercel:
        root = Path("/tmp/mmdesk")
    else:
        root = ROOT / "data"
        if not _writable(root):
            root = Path(tempfile.gettempdir()) / "mmdesk"
            _root_fell_back = True
            log.warning("default data directory is not writable; using %s", root)
    _root_cache[key] = root
    return root


def db_path() -> Path:
    override = os.getenv("MMDESK_DB")
    if override:
        return Path(override)
    return _data_root() / "desk.db"


def persistent() -> bool:
    """False when writable state will not outlive the instance that wrote it.

    A SQLite file under a serverless /tmp is not storage. The app refuses to create tenants on
    such a host instead of accepting one that will 404 on the next request.
    """
    if os.getenv("MMDESK_EPHEMERAL") == "1" or _root_fell_back:
        return False
    if not os.getenv("VERCEL"):
        return True
    try:
        root = _data_root().resolve()
        return not root.is_relative_to(Path(tempfile.gettempdir()).resolve())
    except OSError:
        return False


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_S)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        present = {row["name"] for row in conn.execute("PRAGMA table_info(tenants)")}
        for column in _ADDED_COLUMNS:
            if column not in present:
                # Another process may have added it in the meantime.
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute(f"ALTER TABLE tenants ADD COLUMN {column} TEXT")
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


@dataclass
class Tenant:
    id: str
    name: str
    lens_label: str
    corpus_dir: str
    adapter: dict[str, Any] | None = None      # the live adapter, or a pre-registry record
    candidate: dict[str, Any] | None = None    # trained, gated, not yet promoted
    attribution: str | None = None             # consent line shown with this tenant's output
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

    @property
    def pending_candidate(self) -> dict[str, Any] | None:
        """The candidate to promote: the new slot, or a legacy record that was never activated."""
        if self.candidate:
            return self.candidate
        a = self.adapter
        if a and a.get("status") != "active":
            return a
        return None

    def journal_path(self, scope: str | None = None) -> Path:
        """`journal/<id>.jsonl`, or `journal/<id>/<scope>.jsonl` for a caller that named a scope."""
        base = _data_root() / "journal"
        if scope:
            if not JOURNAL_ID.match(scope):
                raise ValueError("journal scope must be 8-64 letters, digits, '-' or '_'")
            base = base / self.id
        base.mkdir(parents=True, exist_ok=True)
        return base / (f"{scope}.jsonl" if scope else f"{self.id}.jsonl")

    def public(self) -> dict[str, Any]:
        """What any caller may see for a tenant id they already know. No filesystem paths."""
        a = self.active_adapter or {}
        return {
            "id": self.id, "name": self.name, "lens_label": self.lens_label,
            "attribution": self.attribution,
            "corpus": {"available": self.has_corpus},
            "adapter": ({"status": "active", "base": a.get("base"),
                         "trained_at": a.get("trained_at")} if a else None),
            "candidate": self.candidate is not None,
        }


def _insert_default(conn: sqlite3.Connection) -> None:
    # OR IGNORE, not select-then-insert: on a cold instance the first requests overlap, and two
    # of them racing to create the default tenant used to raise a UNIQUE constraint error.
    conn.execute(
        "INSERT OR IGNORE INTO tenants"
        " (id, name, lens_label, corpus_dir, adapter, active, created_at)"
        " VALUES (?, ?, ?, ?, NULL, 1, ?)",
        (DEFAULT_TENANT, "Decision Desk", "House lens",
         f"corpus/{DEFAULT_TENANT}", _now()),
    )
    conn.commit()


def ensure_default() -> None:
    conn = _connect()
    try:
        _insert_default(conn)
    finally:
        conn.close()


def _row_to_tenant(row: sqlite3.Row) -> Tenant:
    def load(name: str) -> dict[str, Any] | None:
        return json.loads(row[name]) if row[name] else None

    return Tenant(id=row["id"], name=row["name"], lens_label=row["lens_label"],
                  corpus_dir=row["corpus_dir"], adapter=load("adapter"),
                  candidate=load("candidate"), attribution=row["attribution"],
                  active=bool(row["active"]), created_at=row["created_at"])


def get(tenant_id: str) -> Tenant | None:
    conn = _connect()
    try:
        if tenant_id == DEFAULT_TENANT:
            _insert_default(conn)
        row = conn.execute("SELECT * FROM tenants WHERE id = ? AND active = 1",
                           (tenant_id,)).fetchone()
        return _row_to_tenant(row) if row else None
    finally:
        conn.close()


def all_tenants() -> list[Tenant]:
    conn = _connect()
    try:
        _insert_default(conn)
        return [_row_to_tenant(r)
                for r in conn.execute("SELECT * FROM tenants ORDER BY created_at, id")]
    finally:
        conn.close()


def create(tenant_id: str, name: str, lens_label: str | None = None,
           attribution: str | None = None) -> Tenant:
    if not TENANT_ID.match(tenant_id):
        raise ValueError("tenant id must be 2-32 lower-case letters, digits, '-' or '_'")
    conn = _connect()
    try:
        _insert_default(conn)
        try:
            conn.execute(
                "INSERT INTO tenants"
                " (id, name, lens_label, corpus_dir, adapter, active, created_at, attribution)"
                " VALUES (?, ?, ?, ?, NULL, 1, ?, ?)",
                (tenant_id, name, lens_label or f"{name} lens", f"corpus/{tenant_id}",
                 _now(), attribution),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"tenant_exists: {tenant_id}") from exc
    finally:
        conn.close()
    t = get(tenant_id)
    assert t is not None
    return t


def set_adapter(tenant_id: str, adapter: dict[str, Any]) -> None:
    """Store a trained candidate. It is never routable from here and never displaces a live adapter."""
    if get(tenant_id) is None:
        raise KeyError(f"unknown tenant {tenant_id}")
    candidate = dict(adapter)
    candidate["status"] = "candidate"
    conn = _connect()
    try:
        conn.execute("UPDATE tenants SET candidate = ? WHERE id = ?",
                     (json.dumps(candidate), tenant_id))
        conn.commit()
    finally:
        conn.close()


def activate_adapter(tenant_id: str, require_gate: bool = True) -> dict[str, Any]:
    """Promote a candidate only if its own recorded evaluation clears the bar.

    The gate lives here, not in the training script, because the training script is the party with
    an incentive to declare success. Promotion is the one explicit act that replaces a live adapter.
    """
    t = get(tenant_id)
    if t is None:
        raise KeyError(f"unknown tenant {tenant_id}")
    adapter = dict(t.pending_candidate or {})
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
    conn = _connect()
    try:
        conn.execute("UPDATE tenants SET adapter = ?, candidate = NULL WHERE id = ?",
                     (json.dumps(adapter), tenant_id))
        conn.commit()
    finally:
        conn.close()
    return adapter


def clear_adapter(tenant_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE tenants SET adapter = NULL WHERE id = ?", (tenant_id,))
        conn.commit()
    finally:
        conn.close()


def discard_candidate(tenant_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE tenants SET candidate = NULL WHERE id = ?", (tenant_id,))
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
