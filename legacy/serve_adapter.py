"""Serve the fine-tuned decision adapter as an OpenAI-compatible endpoint.

The point is integration, not novelty: the app already talks to any OpenAI-shaped provider, so
pointing MMDESK_LLM_BASE at this server means the fine-tune drops into the MM Opinion button with
no code change. That also keeps the local model and the hosted model interchangeable — which is
what makes "host it later" a deployment decision rather than a rewrite.

    trainenv/Scripts/python.exe legacy/serve_adapter.py --port 8899
    MMDESK_LLM_KEY=local MMDESK_LLM_BASE=http://127.0.0.1:8899/v1 MMDESK_LLM_MODEL=mm-decision-3b

Not for public exposure: no auth, no rate limiting, single request at a time.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "models" / "mm-decision-lora"
BASE = "Qwen/Qwen2.5-3B-Instruct"

_model = None
_tok = None
_lock = threading.Lock()


def load():
    global _model, _tok
    if _model is not None:
        return
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    print(f"loading {BASE} + adapter {ADAPTER.name} …", flush=True)
    _tok = AutoTokenizer.from_pretrained(str(ADAPTER) if (ADAPTER / "tokenizer.json").exists() else BASE)
    if ADAPTER.joinpath("chat_template.jinja").exists():
        _tok.chat_template = ADAPTER.joinpath("chat_template.jinja").read_text(encoding="utf-8")
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(BASE, quantization_config=bnb,
                                                  device_map={"": 0}, torch_dtype=torch.bfloat16)
    _model = PeftModel.from_pretrained(model, str(ADAPTER))
    _model.eval()
    print("ready", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if not self.path.rstrip("/").endswith("chat/completions"):
            self._send(404, {"error": {"message": "only /v1/chat/completions"}})
            return

        messages = body.get("messages", [])
        max_new = int(body.get("max_new_tokens") or body.get("max_tokens") or 220)
        temperature = float(body.get("temperature") or 0.0)

        import torch
        prompt = _tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = _tok(prompt, return_tensors="pt").to("cuda")
        kwargs = {"max_new_tokens": max_new, "do_sample": temperature > 0,
                  "pad_token_id": _tok.pad_token_id or _tok.eos_token_id}
        if temperature > 0:
            kwargs["temperature"] = temperature

        with _lock, torch.no_grad():
            out = _model.generate(**inputs, **kwargs)
        text = _tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)

        # The product's contract is a JSON object; strip fences and trailing prose if the model
        # adds them, then verify it parses. A non-JSON reply is a bug worth seeing, not hiding.
        match = re.search(r"\{.*\}", text, re.S)
        content = match.group(0) if match else text.strip()

        self._send(200, {
            "id": "chatcmpl-local", "object": "chat.completion", "model": "mm-decision-3b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": int(inputs["input_ids"].shape[1]),
                      "completion_tokens": int(out.shape[1] - inputs["input_ids"].shape[1]),
                      "total_tokens": int(out.shape[1])},
        })

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("models"):
            self._send(200, {"object": "list", "data": [
                {"id": "mm-decision-3b", "object": "model", "owned_by": "local"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

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
    args = ap.parse_args()
    if not ADAPTER.exists():
        print(f"adapter not found at {ADAPTER} — train first")
        return 2
    load()
    print(f"listening on http://127.0.0.1:{args.port}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
