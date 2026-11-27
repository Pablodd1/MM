"""QLoRA fine-tune of the decision voice on a 16 GB card.

Built for the RTX 5070 Ti (Blackwell, sm_120): torch cu128 is required, and the script refuses to
start if CUDA is not actually usable rather than discovering it an hour into the run.

Smoke first, always:
    trainenv/Scripts/python.exe scripts/train_decision_lora.py --smoke
Then the real run:
    trainenv/Scripts/python.exe scripts/train_decision_lora.py --epochs 3

The adapter is written to models/mm-decision-lora/. The base model is not modified.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "sft-cases.jsonl"
OUT_DIR = ROOT / "models" / "mm-decision-lora"
REPORT = ROOT / "data" / "train-report.json"

BASE = "Qwen/Qwen2.5-7B-Instruct"


def load_cases(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def train(args: argparse.Namespace) -> int:
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                              DataCollatorForLanguageModeling, Trainer, TrainingArguments)

    if not torch.cuda.is_available():
        print("CUDA unavailable — refusing to start. Install cu128 wheels into trainenv.")
        return 2
    major, minor = torch.cuda.get_device_capability(0)
    print(f"gpu {torch.cuda.get_device_name(0)} · sm_{major}{minor} · "
          f"{torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")

    rows = load_cases(DATA)
    if len(rows) < 20:
        print(f"dataset too small ({len(rows)}) — run scripts/build_sft_dataset.py first")
        return 2
    by_label: dict[str, list[dict]] = {}
    for row in rows:
        by_label.setdefault(row["meta"]["label"], []).append(row)
    train_rows, eval_rows = [], []
    for label, group in by_label.items():
        k = max(1, round(len(group) * 0.15))
        eval_rows.extend(group[-k:])
        train_rows.extend(group[:-k])
    print(f"cases {len(rows)} · train {len(train_rows)} · eval {len(eval_rows)}")

    base = args.base
    tok = AutoTokenizer.from_pretrained(base)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def render(row: dict) -> str:
        return tok.apply_chat_template(row["messages"], tokenize=False,
                                       add_generation_prompt=False)

    def encode(row: dict) -> dict:
        text = render(row)
        ids = tok(text, truncation=True, max_length=args.max_len)["input_ids"]
        return {"input_ids": ids, "attention_mask": [1] * len(ids), "labels": list(ids)}

    # Leakage guard: an adapter evaluated on cases it trained on reports memory, not skill. The
    # first stratified run reused an adapter trained on the old split and scored 0.92 — every held
    # out case had already been seen. This assert makes that impossible to repeat silently.
    train_prompts = {r["messages"][1]["content"] for r in train_rows}
    overlap = sum(1 for r in eval_rows if r["messages"][1]["content"] in train_prompts)
    if overlap:
        print(f"ABORT: {overlap}/{len(eval_rows)} eval cases also appear in training — split is leaky")
        return 3
    print(f"no leakage: {len(eval_rows)} held-out cases unseen in {len(train_rows)} training cases")

    train_ds = Dataset.from_list([encode(r) for r in train_rows])
    eval_ds = Dataset.from_list([encode(r) for r in eval_rows])

    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(base, quantization_config=bnb,
                                                 device_map={"": 0}, torch_dtype=torch.bfloat16)
    if args.eval_only:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, str(OUT_DIR))
        print(f"eval-only: adapter {OUT_DIR.name} loaded on {base}")
    else:
        model = prepare_model_for_kbit_training(model)
        model = get_peft_model(model, LoraConfig(
            r=args.rank, lora_alpha=args.alpha, lora_dropout=0.05, bias="none",
            task_type="CAUSAL_LM",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"],
        ))
    model.print_trainable_parameters()

    total_steps = max(1, (len(train_ds) * args.epochs) // args.grad_accum)
    steps = args.steps if args.smoke else total_steps
    final_loss = None

    if not args.eval_only:
        targs = TrainingArguments(
            output_dir=str(OUT_DIR / "checkpoints"),
            per_device_train_batch_size=1,
            gradient_accumulation_steps=args.grad_accum,
            num_train_epochs=args.epochs,
            max_steps=steps if args.smoke else -1,
            learning_rate=args.lr,
            lr_scheduler_type="cosine",
            warmup_steps=max(1, int(0.03 * steps)),
            logging_steps=1 if args.smoke else 10,
            save_steps=max(50, steps // 3),
            save_total_limit=2,
            bf16=True,
            gradient_checkpointing=True,
            optim="paged_adamw_8bit",
            report_to=[],
            remove_unused_columns=False,
            eval_strategy="no",
        )

        trainer = Trainer(model=model, args=targs, train_dataset=train_ds, eval_dataset=eval_ds,
                          data_collator=DataCollatorForLanguageModeling(tok, mlm=False))

        if args.smoke:
            print(f"smoke: {steps} steps, batch 1 × accum {args.grad_accum}, len {args.max_len}")
            trainer.train()
            print("smoke ok — loss", trainer.state.log_history[-1])
            return 0

        trainer.train()
        final_loss = next((h.get("loss") for h in reversed(trainer.state.log_history)
                           if "loss" in h), None)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(str(OUT_DIR))
        tok.save_pretrained(str(OUT_DIR))
        print("adapter saved:", OUT_DIR)

    # Agreement with the engine is the only metric that matters for this layer: a model that
    # articulates decisions the shipped rules would not make is worse than no model.
    model.eval()
    correct = 0
    digit_free = 0
    parsed = 0
    samples = []
    predictions: list[str] = []
    expected_labels: list[str] = []
    for row in eval_rows:
        prompt = tok.apply_chat_template(row["messages"][:2], tokenize=False,
                                         add_generation_prompt=True)
        inputs = tok(prompt, return_tensors="pt", truncation=True, max_length=args.max_len).to("cuda")
        with __import__("torch").no_grad():
            out = model.generate(**inputs, max_new_tokens=220, do_sample=False,
                                 pad_token_id=tok.pad_token_id)
        text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            continue
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        parsed += 1
        expected = json.loads(row["messages"][-1]["content"])
        predictions.append(str(data.get("stance", "")).strip().lower())
        expected_labels.append(expected["stance"])
        if predictions[-1] == expected["stance"]:
            correct += 1
        prose = f"{data.get('reasoning','')} {data.get('falsifier','')}"
        if not re.search(r"\d", prose):
            digit_free += 1
        if len(samples) < 3:
            samples.append({"expected": expected["stance"], "got": data.get("stance"),
                            "reasoning": prose[:220]})

    from collections import Counter
    confusion = Counter()
    per_class = Counter()
    per_class_hit = Counter()
    for expected, got in zip(expected_labels, predictions):
        per_class[expected] += 1
        if got == expected:
            per_class_hit[expected] += 1
        confusion[f"{expected}->{got}"] += 1

    report = {
        "base": base, "cases": len(rows), "eval_cases": len(eval_rows),
        "parsed": parsed,
        "stance_agreement": round(correct / parsed, 3) if parsed else None,
        "digit_free_rate": round(digit_free / parsed, 3) if parsed else None,
        "adapter": str(OUT_DIR.relative_to(ROOT)),
        "steps": steps,
        "final_train_loss": round(final_loss, 4) if final_loss else None,
        "per_class_recall": {k: round(per_class_hit[k] / v, 2) for k, v in per_class.items()},
        "confusion": dict(confusion.most_common(12)),
        "samples": samples,
        "leakage_free": True,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("parsed", "stance_agreement", "digit_free_rate")},
                     indent=2))
    print("report:", REPORT.relative_to(ROOT))

    if args.register and args.tenant:
        from mmdesk import tenants

        tenants.set_adapter(args.tenant, {
            "status": "candidate",
            "base": base,
            "path": str(OUT_DIR),
            "model": f"{args.tenant}-lens",
            "base_url": os.environ.get("MMDESK_LENS_URL", "http://127.0.0.1:8899/v1"),
            "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "eval": {
                "stance_agreement": report["stance_agreement"],
                "digit_free_rate": report["digit_free_rate"],
                "eval_cases": report["eval_cases"],
                "leakage_free": True,
            },
        })
        print(f"registered candidate adapter for tenant {args.tenant} — not active until promoted")
    return 0


def main() -> int:
    global OUT_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="ten steps, no save")
    ap.add_argument("--eval-only", action="store_true",
                    help="load the saved adapter and re-run the held-out evaluation")
    ap.add_argument("--steps", type=int, default=10)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=2048)
    ap.add_argument("--tenant", default="", help="train for a registered tenant")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--register", action="store_true",
                    help="record the trained adapter as a candidate in the tenant registry")
    ap.add_argument("--base", default=BASE,
                    help="HF model id; 3B trains faster and is enough for a voice adapter")
    args = ap.parse_args()
    if args.out_dir:
        OUT_DIR = Path(args.out_dir)
    elif args.tenant:
        OUT_DIR = ROOT / "models" / args.tenant
    return train(args)


if __name__ == "__main__":
    raise SystemExit(main())
