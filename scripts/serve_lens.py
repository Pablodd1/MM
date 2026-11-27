"""Serve one or more tenant adapters behind a single OpenAI-compatible endpoint.

This is the "capacity to be fine-tuned" part made operational. Each tenant can own an adapter;
the server loads it lazily, keyed by the model name ``<tenant>-lens``, and refuses any tenant
whose adapter has not been activated. One base model in VRAM, adapters swapped per request.

    trainenv/Scripts/python.exe scripts/serve_lens.py --port 8899
    curl localhost:8899/v1/models

    # the app then uses it per tenant:
    MMDESK_LLM_BASE=http://127.0.0.1:8899/v1

For production, the same adapters can be served by vLLM's multi-LoRA support
(``--enable-lora --lora-modules tenant=path ...``), which is faster and handles concurrency;
this script is the reference implementation and the thing that runs on a workstation.

Not for public exposure: no auth, one generation at a time (the lock is deliberate — a single
16 GB card cannot serve two requests without an eviction policy).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmdesk import tenants as tenant_mod  # noqa: E402

log = logging.getLogger("mmdesk.serve_lens")

_lock = threading.Lock()
_bases: dict[str, tuple[Any, Any]] = {}       # base id -> (model, tokenizer)
# One PEFT model per base model. Every tenant's adapter is loaded onto it under that tenant's
# own name and switched under the lock. The previous version wrapped the same base object once
# per tenant with the default adapter name, so the second tenant's weights replaced the first's
# on the object both requests shared.
_peft: dict[str, Any] = {}                    # base id -> PeftModel
_loaded: set[tuple[str, str]] = set()         # (base id, tenant id) already loaded
MAX_BODY_BYTES = 64 * 1024
MAX_NEW_TOKENS = 600


def _load_tokenizer(base: str, adapter_path: Path | None):
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(str(adapter_path) if adapter_path and (adapter_path / "tokenizer_config.json").exists() else base)
    if adapter_path and (adapter_path / "chat_template.jinja").exists():
        tok.chat_template = (adapter_path / "chat_template.jinja").read_text(encoding="utf-8")
    return tok


def _load_base(base: str):
    import torch
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig

    if base in _bases:
        return _bases[base]
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(base, quantization_config=bnb,
                                                 device_map={"": 0}, torch_dtype=torch.bfloat16)
    tok = _load_tokenizer(base, None)
    _bases[base] = (model, tok)
    return model, tok


def resolve(model_name: str) -> tuple[str, str]:
    """Map a requested model name to (tenant id, base id), loading the adapter if needed.

    Only an *active* adapter resolves: the registry decides, never the request.
    """
    tenant_id = model_name[:-5] if model_name.endswith("-lens") else model_name
    t = tenant_mod.get(tenant_id)
    if t is None:
        raise KeyError(f"unknown tenant {tenant_id}")
    adapter = t.active_adapter
    if not adapter:
        raise PermissionError(f"tenant {tenant_id} has no active adapter")
    path = Path(adapter["path"])
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"adapter path missing for {tenant_id}: {path}")
    base = adapter.get("base") or "Qwen/Qwen2.5-3B-Instruct"

    model, _tok = _load_base(base)
    if base not in _peft:
        from peft import PeftModel

        _peft[base] = PeftModel.from_pretrained(model, str(path), adapter_name=tenant_id)
        _peft[base].eval()
        _loaded.add((base, tenant_id))
        log.info("loaded adapter for tenant %s onto %s", tenant_id, base)
    elif (base, tenant_id) not in _loaded:
        # A second tenant on the same base: its own named adapter, not a re-wrap of the model.
        # PEFT requires adapters that share a base to share their rank and target modules; a
        # mismatch fails here, at load, rather than silently at generation.
        _peft[base].load_adapter(str(path), adapter_name=tenant_id)
        _loaded.add((base, tenant_id))
        log.info("loaded adapter for tenant %s onto %s", tenant_id, base)
    return tenant_id, base


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/").endswith("models"):
            data = []
            for t in tenant_mod.all_tenants():
                if t.active_adapter:
                    data.append({"id": f"{t.id}-lens", "object": "model", "owned_by": t.id})
            self._send(200, {"object": "list", "data": data})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("chat/completions"):
            self._send(404, {"error": {"message": "only /v1/chat/completions"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": {"message": f"body must be under {MAX_BODY_BYTES} bytes",
                                        "type": "too_large"}})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            messages = body.get("messages") or []
            max_new = int(body.get("max_tokens") or 220)
        except (ValueError, AttributeError):
            self._send(400, {"error": {"message": "malformed JSON body", "type": "bad_request"}})
            return
        # Bounded: one request could otherwise ask for an unbounded generation on one GPU.
        max_new = max(1, min(max_new, MAX_NEW_TOKENS))
        model_name = body.get("model") or f"{tenant_mod.DEFAULT_TENANT}-lens"
        temperature = float(body.get("temperature") or 0.0)

        try:
            tenant_id, base = resolve(model_name)
        except (KeyError, PermissionError, FileNotFoundError) as exc:
            self._send(404, {"error": {"message": str(exc), "type": type(exc).__name__}})
            return
        except Exception as exc:
            log.exception("adapter could not be loaded for %s", model_name)
            self._send(503, {"error": {"message": "adapter failed to load", "type": type(exc).__name__}})
            return

        import torch

        _model, tok = _load_base(base)
        try:
            prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = tok(prompt, return_tensors="pt", truncation=True, max_length=4096).to("cuda")
            kwargs = {"max_new_tokens": max_new, "do_sample": temperature > 0,
                      "pad_token_id": tok.pad_token_id or tok.eos_token_id}
            if temperature > 0:
                kwargs["temperature"] = temperature
            peft = _peft[base]
            # The adapter is selected inside the lock: `set_adapter` switches the whole model,
            # so two tenants' requests must never interleave.
            with _lock, torch.no_grad():
                peft.set_adapter(tenant_id)
                out = peft.generate(**inputs, **kwargs)
        except Exception as exc:
            log.exception("generation failed for tenant %s", tenant_id)
            self._send(503, {"error": {"message": "generation failed", "type": type(exc).__name__}})
            return
        text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        match = re.search(r"\{.*\}", text, re.S)
        content = match.group(0) if match else text.strip()
        prompt_tokens = int(inputs["input_ids"].shape[1])
        completion_tokens = int(out.shape[1]) - prompt_tokens
        log.info("tenant=%s model=%s prompt_tokens=%d completion_tokens=%d",
                 tenant_id, model_name, prompt_tokens, completion_tokens)

        self._send(200, {
            "id": "chatcmpl-lens", "object": "chat.completion", "model": model_name,
            "tenant": tenant_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": int(out.shape[1])},
        })

    def _send(self, code: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback by default; this server has no authentication")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("serving on %s with no authentication: anyone who can reach this port can "
                    "use every active adapter", args.host)

    tenant_mod.ensure_default()
    active = [t.id for t in tenant_mod.all_tenants() if t.active_adapter]
    if not active:
        print("no active adapters registered — register a candidate and promote it first")
    print(f"tenants with active adapters: {active or 'none'}", flush=True)
    print(f"listening on http://{args.host}:{args.port}/v1", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
