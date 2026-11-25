"""Generate docs/REVIEW-BUNDLE.md: everything a reviewer asked for, in one file."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "REVIEW-BUNDLE.md"

FILES = [
    "mmdesk/__init__.py",
    "mmdesk/rules.py",
    "mmdesk/rubric.py",
    "mmdesk/standard.py",
    "mmdesk/decision.py",
    "mmdesk/card.py",
    "mmdesk/journal.py",
    "mmdesk/data.py",
    "mmdesk/llm.py",
    "mmdesk/service.py",
    "mmdesk/retrieval_shim.py",
    "mmdesk/gpu.py",
    "mmdesk/voice.py",
    "mmdesk/persona.py",
    "mmdesk/scenarios.py",
    "docs/VOICE.md",
    "docs/SCENARIOS.md",
    "docs/PERSONA-FIDELITY.md",
    "eval/persona_fidelity.py",
    "eval/calibrate_retrieval.py",
    "eval/retrieval_golden.jsonl",
    "docs/RETRIEVAL-CALIBRATION.md",
    "retrieval.py",
    "build_index.py",
    "tests/test_core.py",
    "tests/test_journal.py",
    "tests/test_sizing_review.py",
    "tests/test_voice.py",
    "tests/test_retrieval.py",
    "tests/test_scenarios.py",
    "mmdesk/static/index.html",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/KNOWN-LIMITATIONS.md",
    "docs/ROADMAP.md",
    "docs/FINDING-rubric-is-fundamental.md",
    "docs/DEPLOY.md",
    "docs/REVIEW-RESPONSE.md",
    "docs/CONSENT.md",
]

SAMPLES = ROOT / "docs" / "_samples"


def fence(path: str, text: str) -> str:
    lang = {
        ".py": "python", ".html": "html", ".md": "markdown", ".json": "json",
    }.get(Path(path).suffix, "")
    return f"### `{path}`\n\n```{lang}\n{text.rstrip()}\n```\n"


def main() -> int:
    parts: list[str] = []
    parts.append("# MM Desk — review bundle\n")
    parts.append(
        "Everything requested for a line-by-line review, in one file: source, tests, sample "
        "responses, and the pytest list.\n\n"
        "Generated from commit:\n\n```\n"
        + subprocess.run(["git", "log", "-1", "--oneline"], cwd=ROOT, capture_output=True,
                         text=True).stdout.strip()
        + "\n```\n"
    )

    tests = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                           cwd=ROOT, capture_output=True, text=True)
    names = [ln.split("::", 1)[-1] for ln in tests.stdout.splitlines() if "::" in ln]
    parts.append(f"## Test inventory ({len(names)} tests)\n\n```\n" + "\n".join(names) + "\n```\n")

    health = (SAMPLES / "health.json").read_text(encoding="utf-8") if (SAMPLES / "health.json").exists() else "{}"
    card = (SAMPLES / "card.json").read_text(encoding="utf-8") if (SAMPLES / "card.json").exists() else "{}"
    journal = (SAMPLES / "journal.json").read_text(encoding="utf-8") if (SAMPLES / "journal.json").exists() else "{}"
    retrieval = (SAMPLES / "retrieval.json").read_text(encoding="utf-8") if (SAMPLES / "retrieval.json").exists() else "{}"

    parts.append("## Sample outputs\n")
    parts.append(fence("GET /health", health))
    parts.append(fence("POST /api/analyze", card))
    parts.append(fence("GET /api/journal", journal))
    parts.append(fence("retrieval.Corpus.search()", retrieval))

    parts.append("## Source\n")
    for rel in FILES:
        path = ROOT / rel
        if path.exists():
            parts.append(fence(rel, path.read_text(encoding="utf-8")))
        else:
            parts.append(f"### `{rel}`\n\n_missing_\n")

    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
