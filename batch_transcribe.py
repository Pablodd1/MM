"""Transcribe every catalogue video: GPU when it works, CPU otherwise.

Resumable — a transcript that already exists is skipped, so the job can be
interrupted and restarted without losing work. Each output carries the video
id, title, model used, and timestamped segments so every later citation can
point back to a specific moment in a specific video.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
AUDIO = ROOT / "audio"
TEXT = ROOT / "transcripts"
BATCH_SIZE = 8


def catalogue() -> list[dict]:
    seen: dict[str, dict] = {}
    for name in ("catalog-streams.json", "catalog-videos.json"):
        path = ROOT / name
        if not path.exists():
            continue
        for entry in json.loads(path.read_text(encoding="utf-8")).get("entries", []):
            vid = entry.get("id")
            if vid and vid not in seen:
                seen[vid] = {
                    "video_id": vid,
                    "title": entry.get("title") or "",
                    "duration": entry.get("duration") or 0,
                }
    return sorted(seen.values(), key=lambda r: -r["duration"])


def fetch_audio(video_id: str) -> Path:
    AUDIO.mkdir(exist_ok=True)
    target = AUDIO / f"{video_id}.wav"
    if target.exists() and target.stat().st_size > 500_000:
        return target
    subprocess.run(
        [
            "yt-dlp", "-f", "bestaudio/best", "-x", "--audio-format", "wav",
            "--postprocessor-args", "-ar 16000 -ac 1",
            "-o", str(AUDIO / f"{video_id}.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return target


def load_audio(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as fh:
        if fh.getnchannels() != 1 or fh.getframerate() != 16000 or fh.getsampwidth() != 2:
            raise RuntimeError(f"{path.name}: not 16 kHz mono 16-bit")
        raw = fh.readframes(fh.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def make_model(prefer_gpu: bool):
    from faster_whisper import WhisperModel

    if prefer_gpu:
        try:
            from mmdesk.gpu import cuda_available, enable_cuda_dlls

            dirs = enable_cuda_dlls()
            if not cuda_available():
                raise RuntimeError("no CUDA device visible to ctranslate2")
            model = WhisperModel("large-v3", device="cuda", compute_type="float16")
            # Probe one short encode: cublas/cudnn load lazily, so a model that
            # constructs fine can still fail on the first real transcription.
            import numpy as np
            model.transcribe(np.zeros(16000 * 3, dtype=np.float32), language="en", vad_filter=False)
            print(f"model: large-v3 on cuda/float16 ({len(dirs)} dll dirs)", flush=True)
            return model, "large-v3/cuda"
        except Exception as exc:  # noqa: BLE001 - fall back, never crash the batch
            print(f"gpu unavailable ({type(exc).__name__}: {exc}); falling back to cpu", flush=True)
    model = WhisperModel("small", device="cpu", compute_type="int8")
    print("model: small on cpu/int8", flush=True)
    return model, "small/cpu"


def main() -> int:
    prefer_gpu = "--cpu" not in sys.argv
    limit = None
    for arg in sys.argv[1:]:
        if arg.isdigit():
            limit = int(arg)

    model, model_tag = make_model(prefer_gpu)
    from faster_whisper import BatchedInferencePipeline

    # Batched VAD-chunk inference: roughly an order of magnitude faster than
    # sequential decoding on GPU, with the same weights and quality.
    pipeline = BatchedInferencePipeline(model=model)
    TEXT.mkdir(exist_ok=True)
    queue = catalogue()
    if limit:
        queue = queue[:limit]

    index_path = ROOT / "transcripts" / "_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}

    done = skipped = failed = 0
    for i, item in enumerate(queue, 1):
        vid = item["video_id"]
        txt = TEXT / f"{vid}.txt"
        if txt.exists() and txt.stat().st_size > 200:
            skipped += 1
            continue
        started = time.time()
        try:
            wav = fetch_audio(vid)
            audio = load_audio(wav)
            segments, info = pipeline.transcribe(
                audio, language="en", batch_size=BATCH_SIZE, vad_filter=True,
                initial_prompt="Stock Talk Live, tickers, risk reward, position sizing, NYSE, NASDAQ.",
            )
            rows, lines = [], []
            for seg in segments:
                rows.append({"start": round(seg.start, 2), "end": round(seg.end, 2),
                             "text": seg.text.strip()})
                lines.append(f"[{int(seg.start//60):02d}:{int(seg.start%60):02d}] {seg.text.strip()}")
            txt.write_text("\n".join(lines), encoding="utf-8")
            (TEXT / f"{vid}.json").write_text(json.dumps({
                "video_id": vid, "title": item["title"], "model": model_tag,
                "duration": round(info.duration, 1), "segments": rows,
            }, indent=1), encoding="utf-8")
            index[vid] = {"title": item["title"], "seconds": round(info.duration, 1),
                          "words": sum(len(r["text"].split()) for r in rows)}
            index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
            done += 1
            print(f"[{i}/{len(queue)}] {vid} {info.duration/60:.0f}min -> "
                  f"{len(rows)} segs in {time.time()-started:.0f}s", flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad video must not stop the batch
            failed += 1
            print(f"[{i}/{len(queue)}] {vid} FAILED: {type(exc).__name__}: {exc}", flush=True)

    print(f"done={done} skipped={skipped} failed={failed} model={model_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
