"""Transcribe a YouTube video locally with faster-whisper (no cloud, no API key)."""

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "audio"
TEXT = ROOT / "transcripts"


def read_wav(path: Path) -> np.ndarray:
    """Read the 16 kHz mono PCM we asked yt-dlp for, without PyAV.

    faster-whisper 1.2 calls av.open(metadata_errors=...) which current PyAV
    builds no longer accept, so decoding goes through the stdlib and the model
    receives a float32 array instead of a path.
    """
    with wave.open(str(path), "rb") as fh:
        if fh.getnchannels() != 1 or fh.getframerate() != 16000 or fh.getsampwidth() != 2:
            raise SystemExit(
                f"expected 16 kHz mono 16-bit, got {fh.getframerate()} Hz, "
                f"{fh.getnchannels()} ch, {fh.getsampwidth()*8}-bit"
            )
        frames = fh.readframes(fh.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def fetch_audio(video_id: str) -> Path:
    AUDIO.mkdir(exist_ok=True)
    target = AUDIO / f"{video_id}.wav"
    if target.exists() and target.stat().st_size > 1_000_000:
        return target
    subprocess.run(
        [
            "yt-dlp",
            "-f", "bestaudio/best",
            "-x", "--audio-format", "wav",
            "--postprocessor-args", "-ar 16000 -ac 1",
            "-o", str(AUDIO / f"{video_id}.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        check=True,
    )
    return target


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--model", default="small")
    ap.add_argument("--language", default="en")
    args = ap.parse_args()

    TEXT.mkdir(exist_ok=True)
    wav = fetch_audio(args.video_id)
    print(f"audio: {wav} ({wav.stat().st_size/1e6:.1f} MB)", flush=True)

    model = WhisperModel(args.model, device="cpu", compute_type="int8")
    audio = read_wav(wav)
    segments, info = model.transcribe(
        audio, language=args.language, vad_filter=True, beam_size=5,
    )
    print(f"language={info.language} prob={info.language_probability:.2f} duration={info.duration:.0f}s", flush=True)

    rows = []
    lines = []
    for seg in segments:
        rows.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
        stamp = f"{int(seg.start//60):02d}:{int(seg.start%60):02d}"
        lines.append(f"[{stamp}] {seg.text.strip()}")
        if len(rows) % 50 == 0:
            print(f"  ... {rows[-1]['start']:.0f}s", flush=True)

    txt = TEXT / f"{args.video_id}.txt"
    txt.write_text("\n".join(lines), encoding="utf-8")
    (TEXT / f"{args.video_id}.json").write_text(
        json.dumps({"video_id": args.video_id, "model": args.model, "segments": rows}, indent=1),
        encoding="utf-8",
    )
    words = sum(len(r["text"].split()) for r in rows)
    print(f"wrote {txt} — {len(rows)} segments, {words} words", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
