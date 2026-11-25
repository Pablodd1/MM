"""Pull candidate stock mentions out of a transcript.

Heuristic first pass: cashtags, explicit tickers, and the verbs that turn a
mention into a claim (buy, sold, short, target, position).  The output is a
CSV a human or an LLM can review — it is not a signal, and it never invents a
ticker that was not said.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
TEXT = ROOT / "transcripts"

# Tickers he actually trades in the corpus tend to be US-listed 2-5 letters.
TICKER_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
UPPER_RE = re.compile(r"\b([A-Z]{2,5})\b")
CLAIM_WORDS = (
    "buy", "bought", "buying", "sell", "sold", "selling", "short", "shorting",
    "long", "target", "price target", "position", "own", "added", "trimmed",
    "calls", "puts", "load", "loaded", "entry", "stop",
)
# Words that look like tickers but are ordinary speech or index names.
STOP = {
    "AI", "CEO", "IPO", "USA", "OK", "TV", "EPS", "ETF", "GDP", "FED", "SEC",
    "THE", "AND", "FOR", "YOU", "ALL", "NOT", "BUT", "CAN", "OUT", "NEW", "NOW",
    "ONE", "TWO", "GET", "GOT", "WAY", "DAY", "BIG", "LOW", "HIGH", "UP", "DOWN",
    "SPX", "SPY", "QQQ", "DOW", "CPI", "YOY", "QOQ", "EV", "IT", "IS", "IF", "OR",
}


def sentences(segments: list[dict], window: int = 1) -> list[dict]:
    """Merge short caption segments into readable windows with a start time."""
    out = []
    for i, seg in enumerate(segments):
        lo, hi = max(0, i - window), min(len(segments), i + window + 1)
        text = " ".join(s["text"] for s in segments[lo:hi]).strip()
        out.append({"start": seg["start"], "text": text})
    return out


def extract(video_id: str) -> Path:
    data = json.loads((TEXT / f"{video_id}.json").read_text(encoding="utf-8"))
    rows = []
    for s in sentences(data["segments"]):
        text = s["text"]
        low = text.lower()
        if not any(w in low for w in CLAIM_WORDS):
            continue
        found = {m.upper() for m in TICKER_RE.findall(text)}
        found |= {m for m in UPPER_RE.findall(text) if m not in STOP}
        for ticker in sorted(found):
            rows.append({
                "video_id": video_id,
                "start_s": round(s["start"], 1),
                "stamp": f"{int(s['start']//60):02d}:{int(s['start']%60):02d}",
                "ticker": ticker,
                "text": text,
            })

    out = ROOT / f"mentions-{video_id}.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["video_id", "start_s", "stamp", "ticker", "text"])
        writer.writeheader()
        writer.writerows(rows)

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["ticker"]] = counts.get(r["ticker"], 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:20]
    print(f"{len(rows)} claim-bearing mentions -> {out}")
    print("top tickers:", ", ".join(f"{t}({n})" for t, n in top))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    args = ap.parse_args()
    extract(args.video_id)


if __name__ == "__main__":
    main()
