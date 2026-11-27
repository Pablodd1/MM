"""Local push-to-talk voice loop: Whisper in, Kokoro out.

Everything runs on the machine that hosts the service. No cloud model is called, and no voice
clone is involved — Kokoro ships with its own licensed voices. When the service is not on
localhost, the recording does travel to the server that transcribes it; the UI says so.

The rule from the rest of the app still holds: the text that gets spoken is rendered from
typed card fields, never from a language model. The model may write prose; it may not write
numbers, and it certainly may not speak them.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("mmdesk.voice")

VOICE_DIR = Path(os.environ.get("MMDESK_VOICE_DIR", str(Path.home() / "tools" / "tts-local")))
KOKORO_MODEL = VOICE_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = VOICE_DIR / "voices-v1.0.bin"

STT_MODEL = os.environ.get("MMDESK_STT_MODEL", "large-v3")
STT_DEVICE = os.environ.get("MMDESK_STT_DEVICE", "cuda")
STT_COMPUTE = os.environ.get("MMDESK_STT_COMPUTE", "float16")
TTS_VOICE = os.environ.get("MMDESK_TTS_VOICE", "am_michael")
TTS_SPEED = float(os.environ.get("MMDESK_TTS_SPEED", "0.98"))

MAX_SECONDS = 45

_lock = threading.Lock()
_whisper = None
_kokoro = None


class VoiceUnavailable(RuntimeError):
    """Raised when a local model or binary is missing. The API turns this into a 501."""


# --------------------------------------------------------------------- status
def status() -> dict:
    return {
        "stt": {
            "model": STT_MODEL,
            "device": STT_DEVICE,
            "loaded": _whisper is not None,
            "audio_supported": bool(shutil.which("ffmpeg")),
        },
        "tts": {
            "engine": "kokoro-onnx",
            "voice": TTS_VOICE,
            "model_present": KOKORO_MODEL.exists(),
            "loaded": _kokoro is not None,
        },
        "local_only": True,
    }


# ------------------------------------------------------------------------ STT
def _get_whisper():
    global _whisper
    with _lock:
        if _whisper is None:
            try:
                from .gpu import enable_cuda_dlls

                enable_cuda_dlls()
                from faster_whisper import WhisperModel

                _whisper = WhisperModel(STT_MODEL, device=STT_DEVICE,
                                        compute_type=STT_COMPUTE)
            except Exception as exc:
                raise VoiceUnavailable(f"speech-to-text unavailable: {exc}") from exc
    return _whisper


def _to_wav16k(src: Path, dst: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VoiceUnavailable("ffmpeg not found on PATH; cannot decode the upload")
    # -t bounds the decode: a long upload used to be decoded in full and only then rejected.
    proc = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-t", str(MAX_SECONDS),
         "-ac", "1", "-ar", "16000", "-f", "wav", str(dst)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise ValueError(f"could not decode audio: {proc.stderr.strip()[:200]}")


def transcribe(path: Path) -> str:
    """Speech to text, entirely locally. Returns the transcript, stripped."""
    model = _get_whisper()
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "in.wav"
        _to_wav16k(path, wav_path)
        with wave.open(str(wav_path), "rb") as fh:
            frames = fh.readframes(fh.getnframes())
            rate = fh.getframerate()
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if rate != 16000:
            raise ValueError(f"unexpected sample rate {rate}")
        if audio.size == 0:
            raise ValueError("empty audio")
        if audio.size > 16000 * MAX_SECONDS:
            raise ValueError(f"audio longer than {MAX_SECONDS}s")
        segments, _info = model.transcribe(
            audio, language="en", vad_filter=True, beam_size=1,
            condition_on_previous_text=False,
        )
    return " ".join(s.text.strip() for s in segments).strip()


# ------------------------------------------------------------------------ TTS
def _get_kokoro():
    global _kokoro
    with _lock:
        if _kokoro is None:
            if not (KOKORO_MODEL.exists() and KOKORO_VOICES.exists()):
                raise VoiceUnavailable(
                    f"Kokoro model files missing under {VOICE_DIR}"
                )
            try:
                from kokoro_onnx import Kokoro

                _kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
            except Exception as exc:
                raise VoiceUnavailable(f"text-to-speech unavailable: {exc}") from exc
    return _kokoro


def speak(text: str) -> bytes:
    """Render speech locally and return WAV bytes."""
    kokoro = _get_kokoro()
    samples, rate = kokoro.create(text.strip(), voice=TTS_VOICE, speed=TTS_SPEED,
                                  lang="en-us")
    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, samples, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def to_mp3(wav: bytes) -> bytes | None:
    """Compress the spoken reply. A WAV answer is ~750 KB; the same MP3 is ~60 KB."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", "pipe:0",
             "-codec:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
            input=wav, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("mp3 conversion failed, returning WAV: %s", exc)
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


# --------------------------------------------------------------------- routing
TICKER_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
COMPANIES = {
    "nvidia": "NVDA", "nvda": "NVDA", "microsoft": "MSFT", "apple": "AAPL",
    "tesla": "TSLA", "amazon": "AMZN", "google": "GOOGL", "alphabet": "GOOGL",
    "meta": "META", "facebook": "META", "amd": "AMD", "intel": "INTC",
    "palantir": "PLTR", "marvell": "MRVL", "micron": "MU", "broadcom": "AVGO",
    "netflix": "NFLX", "salesforce": "CRM", "oracle": "ORCL", "coinbase": "COIN",
}
# All-caps words that are ordinary speech, not symbols. "DD" (due diligence) used to be heard
# as a ticker.
STOPWORDS = {
    "BUY", "SELL", "HOLD", "ADD", "TRIM", "STOP", "WHAT", "THIS", "THAT", "HERE",
    "NOW", "GOOD", "BAD", "THE", "AND", "FOR", "ARE", "YOU", "CAN", "HOW", "WHY",
    "AI", "CEO", "IPO", "ETF", "GDP", "FED", "USD", "OK", "YES", "NO", "IS", "IT",
    "DD", "PM", "AM", "ET", "USA", "UK", "EU", "CFO", "CTO", "COO", "FOMC", "CPI",
    "YOY", "EPS", "ROI", "PC", "TV", "EV", "PE", "PB", "IR", "IRAs",
}


def route(transcript: str) -> dict:
    """Pull a ticker and the question out of what was actually said.

    When several names are heard, the first one wins: "I bought Apple and sold Microsoft" is
    about Apple. The names used to be checked in dictionary order.
    """
    text = transcript.strip()
    ticker = None
    m = TICKER_RE.search(text)
    if m:
        ticker = m.group(1).upper()
    if ticker is None:
        found = None
        for word, sym in COMPANIES.items():
            hit = re.search(rf"\b{word}\b", text, re.IGNORECASE)
            if hit and (found is None or hit.start() < found[0]):
                found = (hit.start(), sym)
        if found:
            ticker = found[1]
    if ticker is None:
        # Only 2-5 letter all-caps tokens: a lone "I" or "A" is a word, not a symbol.
        for token in re.findall(r"\b[A-Z]{2,5}\b", text):
            if token not in STOPWORDS:
                ticker = token
                break
    return {"ticker": ticker, "question": text, "heard": text}


# ---------------------------------------------------------------------- reply
def reply_text(card: dict) -> str:
    """One or two spoken sentences, rendered from typed fields only.

    Every figure here is read off the card the deterministic layer produced — nothing is
    invented, and nothing is paraphrised by a model. The card's lens block is `lens`; this read
    `mm_lens`, a key the card stopped carrying, so every reply said "score unknown".
    """
    instrument = card.get("instrument", "the name")
    price = (card.get("standard_lens") or {}).get("price")
    risk = card.get("risk") or {}
    lens = card.get("lens") or {}
    shares = risk.get("shares", 0)
    stop = risk.get("invalidation")
    verdict = card.get("verdict", "")

    def num(x, places=2):
        return f"{x:,.{places}f}" if isinstance(x, (int, float)) else "an unknown level"

    if not shares:
        # The sizer's own reason: zero shares follows from the alert, the budget, the cash cap
        # or the score's cap, and only one of those is true of this card.
        reason = (lens.get("reason") or "no position is permitted").rstrip(".")
        return (
            f"{instrument} is at {num(price)}. The read is {verdict}, and the size comes back "
            f"zero: {reason}. Nothing to do here."
        )
    return (
        f"{instrument} is at {num(price)}. The read is {verdict}, proxy score "
        f"{lens.get('score_10', 'unknown')} out of ten. Size is {shares} shares with invalidation "
        f"at {num(stop)}. The stop decides, not the opinion."
    )
