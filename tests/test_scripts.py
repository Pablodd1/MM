"""The tooling around the app, exercised as far as it can be without a GPU.

The training and serving scripts import torch, transformers and peft inside their functions.
The parts that decide *what* gets trained, scored and served do not need any of them, and those
are the parts that were wrong: a builder that failed on import, a training target full of
numerals, an agreement rate that ignored unparseable output, and an adapter server that wrapped
one shared model once per tenant.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from conftest import write_index
from sample_corpus import PASSAGES

from mmdesk import tenants

ROOT = Path(__file__).resolve().parent.parent
PASSING = {"stance_agreement": 0.95, "leakage_free": True}
FAILING = {"stance_agreement": 0.61, "leakage_free": True}


def _load(name: str, relative: str):
    """Import a script as a fresh module, so its process state starts empty."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # dataclasses and typing resolve through sys.modules
    spec.loader.exec_module(module)
    return module


def _run(script: str, *args) -> subprocess.CompletedProcess:
    """Run a script in this test's own registry: the suite's isolation fixture has already
    pointed the environment at a throwaway database."""
    return subprocess.run(
        [sys.executable, str(ROOT / script), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(os.environ), cwd=ROOT, timeout=180,
    )


# ---------------------------------------------------------------- the training set

def _targets(path: Path) -> list[dict]:
    return [json.loads(json.loads(line)["messages"][-1]["content"])
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_training_set_builder_runs_offline_and_no_target_has_a_numeral(tmp_path):
    """Step one of the documented pipeline failed on an import. Offline, and whenever the
    teacher model is unavailable, its reasoning also fell back to the engine's display text,
    which carries the score and the ratio — so every target had a digit in it."""
    out = tmp_path / "sft.jsonl"
    result = _run("scripts/build_sft_dataset.py", "--n", 24, "--batch", 8,
                   "--dry-run", "--out", out)
    assert result.returncode == 0, result.stderr[-800:]
    targets = _targets(out)
    assert len(targets) == 24
    assert {t["stance"] for t in targets} == {"add", "hold", "trail", "pass"}
    assert all(not re.search(r"\d", t["reasoning"] + t["falsifier"]) for t in targets)


def test_the_trainer_counts_an_unparseable_output_as_disagreement():
    """Agreement was `correct / parsed`. A model whose output would not parse half the time
    scored as though it had agreed on everything, and passed the promotion gate."""
    trainer = _load("trainer_under_test", "scripts/train_decision_lora.py")
    expected = ["add"] * 5 + ["hold"] * 5
    predicted = ["add"] * 5 + [trainer.UNPARSED] * 5
    report = trainer.agreement_metrics(expected, predicted, digit_free=5)
    assert report["stance_agreement"] == 0.5          # the old formula gave 1.0
    assert report["parse_rate"] == 0.5
    assert report["digit_free_rate"] == 0.5           # what cannot be checked cannot pass
    assert report["per_class_recall"] == {"add": 1.0, "hold": 0.0}
    assert trainer.agreement_metrics(["add", "add", "hold"], ["add", "hold", "hold"], 3)[
        "stance_agreement"] == 0.667
    assert trainer.agreement_metrics([], [], 0)["stance_agreement"] is None


def test_the_trainer_reads_the_tenants_own_dataset():
    """The builder writes data/<tenant>/; the trainer read one fixed file regardless."""
    source = (ROOT / "scripts" / "train_decision_lora.py").read_text(encoding="utf-8")
    assert '"--data"' in source and 'folder = ROOT / "data" / (args.tenant or "")' in source
    assert (ROOT / "scripts" / "build_sft_dataset.py").read_text(encoding="utf-8").count(
        'str(Path("data") / args.tenant / "sft-cases.jsonl")') == 1


# ---------------------------------------------------------------- ingest

def _material(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text(
        "Risk reward is the whole method. Size the position from the score, never from "
        "conviction.\n\nCash is a position too.\n", encoding="utf-8")
    return folder


def test_ingest_refuses_a_tenant_id_that_is_not_an_id(tmp_path):
    """The id becomes a directory name; `../../x` wrote the index outside corpus/."""
    _material(tmp_path / "material")
    result = _run("scripts/ingest_corpus.py", "--tenant", "../../escaped/x",
                  "--src", tmp_path / "material")
    assert result.returncode == 2
    assert "lower-case letters" in result.stdout + result.stderr


def test_ingest_marks_note_timestamps_as_placeholders(tmp_path):
    """Notes have no recording to point into. Their stamps are sequential placeholders and the
    index says so, rather than presenting them as positions in a video."""
    _material(tmp_path / "material")
    out = tmp_path / "corpus" / "notes"
    result = _run("scripts/ingest_corpus.py", "--tenant", "notes", "--src",
                  tmp_path / "material", "--out", out)
    assert result.returncode == 0, result.stderr[-800:]
    rows = [json.loads(line) for line in (out / "passages.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["synthetic"] is True and row["url"] == "" for row in rows)
    assert json.loads((out / "video-meta.json").read_text(encoding="utf-8"))[rows[0]["video_id"]]["synthetic"]


# ---------------------------------------------------------------- promotion

def _tenant(tmp_path, tenant_id: str, evaluation: dict) -> None:
    tenants.create(tenant_id, tenant_id.title())
    weights = tmp_path / "models" / tenant_id
    weights.mkdir(parents=True)
    tenants.set_adapter(tenant_id, {"path": str(weights), "base_url": "http://127.0.0.1:8899/v1",
                                    "key": "adapter-secret-value", "eval": evaluation})


def test_the_promoter_reports_the_registry_verdict(tmp_path):
    """It kept a second copy of the 0.85 bar and read the wrong slot for the candidate."""
    _tenant(tmp_path, "weak", FAILING)
    refused = _run("scripts/promote_adapter.py", "--tenant", "weak")
    assert refused.returncode == 1 and "REFUSED by the registry" in refused.stdout
    assert tenants.get("weak").active_adapter is None

    _tenant(tmp_path, "strong", PASSING)
    accepted = _run("scripts/promote_adapter.py", "--tenant", "strong")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert tenants.get("strong").active_adapter is not None


def test_the_promoter_never_prints_an_adapter_key(tmp_path):
    _tenant(tmp_path, "secretive", PASSING)
    shown = _run("scripts/promote_adapter.py", "--tenant", "secretive", "--show")
    assert shown.returncode == 0
    assert "adapter-secret-value" not in shown.stdout + shown.stderr
    assert "<not shown>" in shown.stdout


# ---------------------------------------------------------------- the adapter server

class _FakePeft:
    """Stands in for PEFT on a base model. Records how each adapter was attached."""

    def __init__(self, log: dict, model) -> None:
        self.log, self.model = log, model

    @classmethod
    def attach(cls, log):
        def from_pretrained(model, path, adapter_name):
            log["from_pretrained"].append((id(model), path, adapter_name))
            return cls(log, model)

        return from_pretrained

    def eval(self):
        return self

    def load_adapter(self, path, adapter_name):
        self.log["load_adapter"].append((id(self.model), path, adapter_name))


def test_the_adapter_server_loads_each_tenant_under_its_own_name(tmp_path, monkeypatch):
    """Two tenants sharing a base model must not share weights.

    The server wrapped the same base object once per tenant under PEFT's default adapter name,
    so the second tenant's weights replaced the first's on the object both requests used. It
    also passed a keyword that does not exist, so loading crashed. Exercised with stand-ins for
    torch, transformers and peft: the structure is what was wrong, and that is testable here.
    """
    log = {"from_pretrained": [], "load_adapter": [], "config": []}
    fake_torch = ModuleType("torch")
    fake_torch.bfloat16 = "bfloat16"

    fake_transformers = ModuleType("transformers")

    def bits_and_bytes(**kwargs):
        log["config"].append(kwargs)
        return SimpleNamespace(**kwargs)

    fake_transformers.BitsAndBytesConfig = bits_and_bytes
    fake_transformers.AutoModelForCausalLM = SimpleNamespace(from_pretrained=lambda *a, **k: object())
    fake_transformers.AutoTokenizer = SimpleNamespace(from_pretrained=lambda *a, **k: object())
    fake_peft = ModuleType("peft")
    fake_peft.PeftModel = SimpleNamespace(from_pretrained=_FakePeft.attach(log))
    for name, module in (("torch", fake_torch), ("transformers", fake_transformers),
                         ("peft", fake_peft)):
        monkeypatch.setitem(sys.modules, name, module)

    for tenant_id in ("servera", "serverb"):
        tenants.create(tenant_id, tenant_id.title())
        weights = tmp_path / "models" / tenant_id
        weights.mkdir(parents=True)
        tenants.set_adapter(tenant_id, {"path": str(weights), "base": "fake/base", "eval": PASSING})

    serve = _load("serve_lens_under_test", "scripts/serve_lens.py")

    # A candidate is not routable: only an active adapter resolves.
    with pytest.raises(PermissionError):
        serve.resolve("servera-lens")
    tenants.activate_adapter("servera")
    tenants.activate_adapter("serverb")

    assert serve.resolve("servera-lens") == ("servera", "fake/base")
    assert serve.resolve("serverb-lens") == ("serverb", "fake/base")
    assert serve.resolve("servera-lens") == ("servera", "fake/base")     # no third load

    assert len(serve._peft) == 1, "one model per base, holding every tenant's adapter"
    assert [(path.split(os.sep)[-1], name) for _, path, name in log["from_pretrained"]] == [
        ("servera", "servera")]
    assert [(path.split(os.sep)[-1], name) for _, path, name in log["load_adapter"]] == [
        ("serverb", "serverb")]
    assert log["config"] == [log["config"][0]]                      # the base loaded once
    assert "bnb_4bit_quant_type" in log["config"][0], "the keyword that crashed loading"
    with pytest.raises(KeyError):
        serve.resolve("nobody-lens")


# ---------------------------------------------------------------- what is no longer in the repo

@pytest.mark.parametrize("gone", [
    "scripts/apply_journal.py", "scripts/apply_review_fixes.py", "scripts/apply_sizing.py",
    "scripts/apply_voice.py", "scripts/_size_position.py", "scripts/push_via_api.py",
    "docs/REVIEW-BUNDLE.md",
], ids=lambda p: p)
def test_the_scripts_that_damaged_the_tree_are_gone(gone):
    """Four "apply fix" scripts rewrote source by string replacement and corrupted it when run
    against the current code; a fallback in push_via_api uploaded .env and tenant material."""
    assert not (ROOT / gone).exists()


def test_write_index_helper_matches_what_ingest_produces(tmp_path):
    """The tests' index helper and the real ingest script must write the same row shape."""
    real = tmp_path / "real"
    _material(tmp_path / "material")
    _run("scripts/ingest_corpus.py", "--tenant", "shape", "--src", tmp_path / "material",
         "--out", real)
    ingested = [json.loads(line) for line in
                (real / "passages.jsonl").read_text(encoding="utf-8").splitlines()]
    helper = write_index(tmp_path / "helper", PASSAGES)
    made = [json.loads(line) for line in helper.read_text(encoding="utf-8").splitlines()]
    for key in ("id", "video_id", "video_title", "start_s", "stamp", "url", "text", "tf"):
        assert key in ingested[0] and key in made[0], key
    assert all(isinstance(row["tf"], dict) and row["tf"] for row in ingested + made)
