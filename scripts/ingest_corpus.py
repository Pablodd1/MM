"""Turn a tenant's own material into the index the lens reads.

Without a corpus the product still works — the engine, the standard lens and the template
narrative do not need one. This script is how a tenant becomes specific: point it at a folder of
notes, a transcript, or a JSONL export and it writes ``corpus/<tenant>/passages.jsonl``.

Accepted inputs, in order of fidelity:

* ``.json`` transcript with ``segments`` or ``entries`` of timestamped text (best — keeps seconds)
* ``.jsonl`` with one object per passage (``text`` plus optional ``video_id``/``start_s``)
* ``.txt`` / ``.md`` notes (no timestamps; each chunk gets a sequential synthetic start)

Chunking is sentence-aligned and overlapping, because a quote cut mid-sentence is not a quote.

    python scripts/ingest_corpus.py --tenant acme --src ./material --title "Acme method"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collections import Counter  # noqa: E402

from retrieval import tokens  # noqa: E402

CHUNK_CHARS = 900
OVERLAP_SENTENCES = 1
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def stamp(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def chunks(text: str) -> list[str]:
    sentences = [s.strip() for s in SENTENCE_RE.split(text.strip()) if s.strip()]
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for sentence in sentences:
        if size + len(sentence) > CHUNK_CHARS and cur:
            out.append(" ".join(cur))
            cur = cur[-OVERLAP_SENTENCES:] if OVERLAP_SENTENCES else []
            size = sum(len(s) for s in cur)
        cur.append(sentence)
        size += len(sentence)
    if cur:
        out.append(" ".join(cur))
    return [c for c in out if len(c) >= 40]


def from_transcript(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = data.get("segments") or data.get("entries") or []
    title = data.get("title") or path.stem
    vid = str(data.get("video_id") or data.get("id") or hashlib.sha1(path.stem.encode()).hexdigest()[:11])
    rows = []
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = int(float(seg.get("start") or seg.get("start_s") or 0))
        rows.append({"video_id": vid, "video_title": title, "start_s": start,
                     "stamp": stamp(start), "text": text, "url": data.get("url", "")})
    return rows


def from_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        text = str(obj.get("text") or "").strip()
        if not text:
            continue
        start = int(float(obj.get("start_s") or 0))
        rows.append({"video_id": str(obj.get("video_id") or path.stem),
                     "video_title": str(obj.get("video_title") or path.stem),
                     "start_s": start, "stamp": obj.get("stamp") or stamp(start),
                     "text": text, "url": obj.get("url", "")})
    return rows


def from_text(path: Path, seq: int) -> list[dict]:
    rows = []
    for i, chunk in enumerate(chunks(path.read_text(encoding="utf-8", errors="replace"))):
        start = seq * 3600 + i * 120
        rows.append({"video_id": path.stem[:11], "video_title": path.stem,
                     "start_s": start, "stamp": stamp(start), "text": chunk, "url": ""})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--src", required=True, help="file or folder of material")
    ap.add_argument("--title", default="", help="catalogue title used in /health")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        print(f"source not found: {src}")
        return 2

    files = [src] if src.is_file() else sorted(
        p for p in src.rglob("*") if p.suffix.lower() in {".json", ".jsonl", ".txt", ".md"})
    if not files:
        print("no .json/.jsonl/.txt/.md files found")
        return 2

    rows: list[dict] = []
    for i, path in enumerate(files):
        suffix = path.suffix.lower()
        if suffix == ".json":
            rows += from_transcript(path)
        elif suffix == ".jsonl":
            rows += from_jsonl(path)
        else:
            rows += from_text(path, i)

    if not rows:
        print("nothing usable found — every file was empty or had no text field")
        return 2

    out_dir = Path(args.out) if args.out else ROOT / "corpus" / args.tenant
    out_dir.mkdir(parents=True, exist_ok=True)
    # The index format is not raw text: it carries a term-frequency map so search does not have to
    # tokenise every passage on every query. Plain rows produce an index that loads and then fails
    # at the first search (KeyError: 'tf') — write the map here.
    with (out_dir / "passages.jsonl").open("w", encoding="utf-8") as fh:
        for i, row in enumerate(rows):
            row = dict(row)
            row["id"] = f"{row['video_id']}-{int(row['start_s'])}-{i}"
            row["tf"] = dict(Counter(tokens(row["text"])))
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    videos = {r["video_id"] for r in rows}
    meta_path = out_dir / "video-meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    for vid in videos:
        secs = max((r["start_s"] for r in rows if r["video_id"] == vid), default=0)
        meta.setdefault(vid, {"title": args.title or vid, "duration": secs + 120})
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(json.dumps({"tenant": args.tenant, "passages": len(rows), "sources": len(files),
                      "videos": len(videos), "written": str(out_dir / "passages.jsonl")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
