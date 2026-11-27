"""Repository hygiene: the things that must never be true of a commit.

This repository is public. Tenant material, journals, adapters and keys are local state, and the
only reliable way to keep them out of the history is a test that fails the moment one is staged.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A provider's key prefix followed by enough key material to be a real key.
KEY_SHAPES = (
    re.compile(rb"\bsk-(?:ant-)?[A-Za-z0-9_\-]{24,}"),
    re.compile(rb"\bxai-[A-Za-z0-9]{24,}"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"\bAKia[0-9A-Z]{16}"),
    re.compile(rb"\bsbp_[0-9a-f]{30,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
LOCAL_STATE = ("corpus/", "data/", "models/", "trainenv/", "audio/", "transcripts/", "runtime/")
LARGE = 2 * 1024 * 1024


def _tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    return [p.decode("utf-8") for p in out.stdout.split(b"\0") if p]


def test_no_key_shaped_string_is_tracked():
    hits = []
    for relative in _tracked():
        blob = ROOT / relative
        if not blob.is_file() or blob.stat().st_size > LARGE:
            continue
        data = blob.read_bytes()
        for shape in KEY_SHAPES:
            if shape.search(data):
                hits.append(relative)
                break
    assert hits == [], f"key-shaped strings in {hits}"


def test_no_local_state_is_tracked():
    tracked = _tracked()
    leaked = [p for p in tracked if p.startswith(LOCAL_STATE) or p.endswith(".db")]
    assert leaked == [], f"local state is tracked: {leaked}"


def test_the_deploy_bundle_excludes_what_must_not_be_uploaded():
    ignored = (ROOT / ".vercelignore").read_text(encoding="utf-8")
    for line in ("corpus/", "data/", "models/", "trainenv/", ".env"):
        assert line in ignored, line


def test_a_command_line_deploy_would_not_need_an_unpinned_server():
    """The README's install line must install what its run and test lines execute."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    development = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "uvicorn" in requirements
    assert "pytest" in development


def test_bare_pytest_finds_the_package():
    """`pytest` from the repository root, without `python -m`, used to fail to import mmdesk."""
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["pytest"]["ini_options"]["pythonpath"] == ["."]
