"""Build a searchable evidence index over the MM corpus.

Every passage keeps its provenance: video id, video title, start second and a
human-readable timestamp. Nothing in the app may state a view "Money Mark" holds
without one of these rows backing it.

Chunking is by time, not by character, so a phone-style answer can jump straight
to the moment it cites. Overlapping windows keep sentences from being cut in half.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
TEXT = ROOT / "transcripts"
INDEX = ROOT / "corpus" / "passages.jsonl"

WINDOW_S = 45.0
HOP_S = 30.0

STOP = set("""a an the and or but if then than that this these those i you he she it we they
me him her us them my your his its our their is are was were be been being do does did
doing have has had having will would can could should shall may might must of in on at
to for with by from as about into over under again further once here there when where why
how all any both each few more most other some such no nor not only own same so too very
s t just don now know like get got going want well right okay ok yeah see think say said
""".split())

WORD_RE = re.compile(r"[a-z0-9$][a-z0-9$.\-]{1,24}")


def tokens(text: str) -> list[str]:
    out = []
    for w in WORD_RE.findall(text.lower()):
        w = w.strip(".-")
        if len(w) < 2 or w in STOP:
            continue
        out.append(w)
    return out


def passages(segments: list[dict]) -> list[dict]:
    out = []
    i = 0
    start = segments[0]["start"] if segments else 0.0
    end = start + WINDOW_S
    buf: list[dict] = []
    for seg in segments:
        while seg["start"] >= end:
            if buf:
                out.append({"start": buf[0]["start"], "end": buf[-1]["end"], "segments": buf})
            # slide forward, keeping overlap
            start += HOP_S
            end = start + WINDOW_S
            buf = [s for s in buf if s["start"] >= start]
        buf.append(seg)
    if buf:
        out.append({"start": buf[0]["start"], "end": buf[-1]["end"], "segments": buf})
    return out


def stamp(seconds: float) -> str:
    return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"


def main() -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    meta_path = ROOT / "corpus" / "video-meta.json"
    meta = {}
    for name in ("catalog-streams.json", "catalog-videos.json"):
        p = ROOT / name
        if p.exists():
            for e in json.loads(p.read_text(encoding="utf-8")).get("entries", []):
                if e.get("id"):
                    meta[e["id"]] = {
                        "title": e.get("title"),
                        "duration": e.get("duration"),
                        "views": e.get("view_count"),
                        "upload_date": e.get("upload_date"),
                    }
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")

    rows = 0
    with INDEX.open("w", encoding="utf-8") as fh:
        for path in sorted(TEXT.glob("*.json")):
            if path.name.startswith("_"):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            vid = data["video_id"]
            title = data.get("title") or meta.get(vid, {}).get("title") or vid
            for idx, p in enumerate(passages(data["segments"])):
                text = " ".join(s["text"] for s in p["segments"]).strip()
                if len(text.split()) < 8:
                    continue
                terms = tokens(text)
                if not terms:
                    continue
                tf = Counter(terms)
                fh.write(json.dumps({
                    "id": f"{vid}:{idx}",
                    "video_id": vid,
                    "video_title": title,
                    "start_s": round(p["start"], 1),
                    "end_s": round(p["end"], 1),
                    "stamp": stamp(p["start"]),
                    "url": f"https://www.youtube.com/watch?v={vid}&t={int(p['start'])}s",
                    "text": text,
                    "tf": dict(tf),
                }) + "\n")
                rows += 1
    print(f"wrote {rows} passages -> {INDEX}")


if __name__ == "__main__":
    main()
