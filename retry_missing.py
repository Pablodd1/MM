"""Retry the videos that failed with YouTube's bot check, at a human pace.

The first batch pulled 17 videos quickly and then hit "Sign in to confirm you're not a
bot". Cookies can't be used here (Chrome's DB is locked, Edge uses app-bound encryption),
so the remaining option is to slow down, vary the player client, and let the block expire.

Each attempt is spaced by --gap seconds; failures are recorded and the next video is tried.
Re-run as often as needed — finished transcripts are never redone.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
AUDIO = ROOT / "audio"
TEXT = ROOT / "transcripts"

CLIENTS = ["web_safari", "tv", "mweb", "android_vr"]


def catalogue() -> list[dict]:
    seen: dict[str, dict] = {}
    for name in ("catalog-streams.json", "catalog-videos.json"):
        p = ROOT / name
        if not p.exists():
            continue
        for e in json.loads(p.read_text(encoding="utf-8")).get("entries", []):
            if e.get("id") and e["id"] not in seen:
                seen[e["id"]] = e
    return sorted(seen.values(), key=lambda e: -(e.get("duration") or 0))


def missing() -> list[dict]:
    out = []
    for e in catalogue():
        txt = TEXT / f"{e['id']}.txt"
        if not (txt.exists() and txt.stat().st_size > 200):
            out.append(e)
    return out


def attempt(video_id: str, client: str) -> tuple[bool, str]:
    AUDIO.mkdir(exist_ok=True)
    cmd = [
        "yt-dlp", "--js-runtimes", "deno",
        "--extractor-args", f"youtube:player_client={client}",
        "--sleep-requests", "2", "--retries", "3", "--fragment-retries", "3",
        "-f", "bestaudio/best", "-x", "--audio-format", "wav",
        "--postprocessor-args", "-ar 16000 -ac 1",
        "-o", str(AUDIO / f"{video_id}.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode == 0:
        return True, "ok"
    err = (p.stderr or "").strip().splitlines()
    return False, (err[-1] if err else f"exit {p.returncode}")[:200]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=45.0, help="seconds between videos")
    ap.add_argument("--rounds", type=int, default=1, help="passes over the missing list")
    args = ap.parse_args()

    ok = fail = 0
    for rnd in range(1, args.rounds + 1):
        todo = missing()
        print(f"round {rnd}: {len(todo)} missing", flush=True)
        if not todo:
            break
        for i, entry in enumerate(todo, 1):
            vid = entry["id"]
            client = CLIENTS[(i - 1) % len(CLIENTS)]
            good, msg = attempt(vid, client)
            stamp = time.strftime("%H:%M:%S")
            if good:
                ok += 1
                print(f"[{stamp}] {i}/{len(todo)} {vid} OK ({client})", flush=True)
            else:
                fail += 1
                print(f"[{stamp}] {i}/{len(todo)} {vid} FAIL ({client}): {msg}", flush=True)
            time.sleep(args.gap + random.uniform(0, 15))
    print(f"attempted ok={ok} fail={fail}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
