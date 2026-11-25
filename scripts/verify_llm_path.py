"""Prove the AI-opinion path works without spending a token.

Starts a local OpenAI-compatible stub, points the app at it with MMDESK_LLM_*, and checks that
the narrative comes back from the model rather than the template. Then sends prose containing a
digit and confirms the guard rejects it and falls back.

Run: .venv/Scripts/python.exe scripts/verify_llm_path.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOOD = {
    "verdict_reason": "The computed verdict follows the position size allowed by the stated risk budget.",
    "mm_narrative": "The MM lens scores this name in the middle of the band, so the rule that sizes by score keeps the position modest and the cash floor intact.",
    "standard_narrative": "Trend and momentum disagree with the valuation read, which is why the conventional lens stops short of adding.",
    "disagreement": "The two lenses differ on timing rather than direction; a close below the invalidation level would settle it.",
    "caveat": "If the next earnings report resets the volatility regime, the stop distance is no longer realistic.",
}

BAD = dict(GOOD, mm_narrative="The score is 7 out of 10 so buy more.")


class Handler(BaseHTTPRequestHandler):
    reply = GOOD

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"content": json.dumps(self.reply)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def serve(port: int = 8899) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


CARD = {
    "instrument": "NVDA", "mode": "swing", "verdict": "hold",
    "mm_lens": {"score_10": 5.5, "action": "trail", "position_pct": 0.05, "shares": 24},
    "standard_lens": {"action": "mixed", "trend": {"score": -1}, "price": 144.6},
    "disagreement": {"summary": "MM says trail, standard says mixed."},
    "risk": {"invalidation": 132.0, "shares": 24},
    "falsifiers": ["A close below the invalidation level."],
}


def main() -> int:
    server = serve()
    os.environ.update({
        "MMDESK_LLM_KEY": "stub-key",
        "MMDESK_LLM_BASE": "http://127.0.0.1:8899/v1",
        "MMDESK_LLM_MODEL": "stub-model",
        "MMDESK_ALLOW_CLOUD_LLM": "1",
    })
    from mmdesk.llm import LLMConfig, narrate

    cfg = LLMConfig.from_env()
    print(f"provider {cfg.provider} · model {cfg.model} · configured {cfg.configured}")

    Handler.reply = GOOD
    out = narrate(CARD, [{"video_id": "abc", "stamp": "00:01:00", "text": "risk reward"}], cfg)
    print("model path:", out.get("generated_by"))
    assert "stub" in (out.get("generated_by") or ""), out
    assert out["mm_narrative"].startswith("The MM lens"), out

    Handler.reply = BAD
    out = narrate(CARD, [], cfg)
    print("digit guard:", out.get("generated_by"))
    assert "template" in (out.get("generated_by") or "").lower(), out
    print("\nBoth paths behave: model prose is used, prose with a numeral is rejected.")
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
