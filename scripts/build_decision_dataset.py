"""Extract Mark's decision episodes from the transcripts.

A decision episode is: a ticker, an action he actually states, and the reasoning around it. This
is the only honest source of "how he decides" that exists — his words, timestamped. It is not a
label (we do not know whether the call worked), so anything trained on it learns his *criteria*,
not his *accuracy*. The gap between those two is the thing to keep telling the truth about.

Output: corpus/decision-episodes.jsonl  (gitignored — it quotes him at length)

Run: .venv/Scripts/python.exe scripts/build_decision_dataset.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# "I'd buy", "I'm trimming", "would add", "take profits", "cut it" — first person or imperative,
# because a third-party mention ("he bought") is not his decision.
ACTION_PATTERNS = [
    ("add", r"\b(?:i(?:'d| would| am|'m)? (?:add|adding|buy|buying|accumulat\w*)|adding here|i'd add)\b"),
    ("trim", r"\b(?:i(?:'d| would| am|'m)? (?:trim|trimming|sell|selling|lighten\w*|take profits?)|trimming here|scaling out)\b"),
    ("hold", r"\b(?:i(?:'d| would| am|'m)? (?:hold|holding|stay(?:ing)? in|sit(?:ting)? tight)|holding here|i'd hold)\b"),
    ("avoid", r"\b(?:i(?:'d| would)? (?:not touch|avoid|pass|stay away)|staying away|no thanks)\b"),
    ("stop", r"\b(?:stop(?:ped)? out|cut(?:ting)? (?:it|the position)|i'm out|getting out)\b"),
]

TICKER_RE = re.compile(r"\$?\b([A-Z]{2,5})\b")
STOP = {
    "THE", "AND", "BUT", "FOR", "YOU", "ARE", "NOT", "ALL", "CAN", "HAS", "WAS", "ONE", "OUT",
    "GET", "GOT", "NOW", "NEW", "WHY", "HOW", "WHAT", "WHO", "CEO", "CFO", "IPO", "ETF", "GDP",
    "FED", "SEC", "AI", "AND", "YET", "BUY", "SELL", "HOLD", "PUT", "CALL", "USD", "USA", "OK",
    "LOL", "TV", "PC", "AM", "PM", "EST", "PST", "YOY", "QOQ", "EPS", "PE", "PS", "ATH", "ATL",
    "DD", "TA", "FA", "IMO", "IMHO", "BTW", "FYI", "AKA", "ASAP", "EOD", "EOW", "FOMO", "YOLO",
    "SPY", "QQQ", "IWM", "DIA", "VIX", "CNBC", "WSJ", "NYSE", "NASDAQ", "RIP", "LMAO", "OMG",
    "US", "AT", "BS", "AWS", "IF", "IT", "IS", "ON", "IN", "OF", "TO", "BE", "DO", "GO", "NO",
    "SO", "UP", "WE", "ME", "MY", "OR", "AN", "AS", "BY", "HE", "SHE", "THIS", "THAT", "THEN",
    "THAN", "ALSO", "JUST", "LIKE", "WANT", "NEED", "KNOW", "THINK", "RIGHT", "WRONG", "GOOD",
    "BAD", "BIG", "SMALL", "LONG", "SHORT", "HIGH", "LOW", "MORE", "MOST", "SOME", "MANY",
    "MUCH", "VERY", "REAL", "NEXT", "LAST", "WEEK", "MONTH", "YEAR", "DAY", "TIME", "MARKET",
    "STOCK", "PRICE", "MONEY", "CASH", "RISK", "TRADE", "CHART", "LEVEL", "POINT", "BIT", "LOT",
    "NOT", "YES", "OKAY", "WELL", "SURE", "MAYBE", "GUYS", "DUDE", "BRO", "MAN", "SIR",
}


def load_segments(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segs = data.get("segments") or []
    out = []
    for s in segs:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "start": float(s.get("start") or 0.0),
            "end": float(s.get("end") or (float(s.get("start") or 0.0) + 4.0)),
        })
    return out


def stamp(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def tickers_in(text: str) -> list[str]:
    out = []
    for tok in TICKER_RE.findall(text):
        if tok in STOP or len(tok) < 2:
            continue
        out.append(tok)
    return out


SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def episodes(segments: list[dict], video_id: str, title: str, url_base: str) -> list[dict]:
    """Strict by design: the ticker and the action must be in the same sentence, and the sentence
    must be first-person. A looser rule produced episodes where "I am still richer than I was"
    was attributed to a ticker mentioned three sentences earlier — noise a model would learn."""
    found: list[dict] = []
    for i, seg in enumerate(segments):
        for sentence in SENTENCE_SPLIT.split(seg["text"]):
            if not re.search(r"i|i'\w+|i'm|i'd|i'll", sentence, re.IGNORECASE):
                continue
            action = None
            for name, pattern in ACTION_PATTERNS:
                if re.search(pattern, sentence, re.IGNORECASE):
                    action = name
                    break
            if not action:
                continue
            cands = tickers_in(sentence)
            if not cands:
                continue
            ticker = cands[-1]
            context = " ".join(s["text"] for s in segments[max(0, i - 1): i + 1])
            follow = " ".join(s["text"] for s in segments[i + 1: i + 3])
            found.append({
                "video_id": video_id,
                "video_title": title,
                "ticker": ticker,
                "action": action,
                "quote": sentence.strip(),
                "context": context,
                "follow": follow,
                "start_s": seg["start"],
                "stamp": stamp(seg["start"]),
                "url": f"{url_base}&t={int(seg['start'])}s",
            })
    return found


def dedupe(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for row in rows:
        key = f"{row['video_id']}:{row['ticker']}:{row['action']}:{row['quote'][:60]}"
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def main() -> int:
    transcripts = sorted((ROOT / "transcripts").glob("*.json"))
    rows: list[dict] = []
    for path in transcripts:
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        video_id = data.get("id") or path.stem
        title = data.get("title") or video_id
        segs = load_segments(path)
        if not segs:
            continue
        rows.extend(episodes(segs, video_id, title, f"https://www.youtube.com/watch?v={video_id}?"))

    rows = dedupe(rows)
    rows.sort(key=lambda r: (r["ticker"], r["video_id"], r["start_s"]))
    out = ROOT / "corpus" / "decision-episodes.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    actions = Counter(r["action"] for r in rows)
    tickers = Counter(r["ticker"] for r in rows)
    videos = len({r["video_id"] for r in rows})
    print(f"episodes {len(rows)} from {videos} videos")
    print("actions:", dict(actions))
    print("top tickers:", tickers.most_common(15))
    print("distinct tickers:", len(tickers))
    print("written:", out.relative_to(ROOT), f"({out.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
