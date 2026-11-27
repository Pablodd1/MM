"""Test isolation for tenancy.

Each run gets its own SQLite registry and journal directory. Without this, a test that creates a
tenant would persist into the developer's desk and the next run would start with state it did not
create — the kind of leak that makes tenancy tests pass for the wrong reason.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_state(tmp_path_factory):
    os.environ["MMDESK_DB"] = str(tmp_path_factory.mktemp("db") / "desk.db")
    os.environ["MMDESK_DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    os.environ.pop("MMDESK_ADMIN_TOKEN", None)
    yield
