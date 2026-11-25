# Voice

Push-to-talk, fully local. Click **Hold to talk**, speak, click again; the answer is spoken back
and the same card is rendered on screen.

## Measured on this machine

| Step | Time |
|---|---|
| Speech to text (`large-v3`, CUDA float16, 3.3 s clip) | 3.1 s |
| Card build (market data, both lenses, journal) | ~0.4 s |
| Text to speech (Kokoro, ~8 s reply) | 2.2 s |
| **End to end, cold** | **7.5 s** |
| Spoken reply size | 129 KB MP3 (756 KB as WAV) |

Those numbers came from `POST /api/voice/selftest`, which synthesizes a question, transcribes it,
builds the card and speaks the reply — the same path the browser uses.

## Endpoints

`POST /api/voice` — multipart form:

- `audio` — the recording (webm/opus from the browser, wav/m4a also accepted via ffmpeg)
- `account_equity`, `risk_pct`, `alert`, `green_line`, `red_line` — optional; same meaning as `/api/analyze`

Returns the full card plus `transcript`, `ticker`, `reply`, `audio_b64`, `audio_mime`, `timings`.

`POST /api/voice/selftest` — no microphone needed; runs the whole loop on a fixed question.

## What is local

Everything. faster-whisper `large-v3` runs on the GPU here, Kokoro runs here, and the WAV/MP3 never
leaves the request. `/health` reports `voice.local_only: true`.

## What is deliberately not done

- **No voice clone.** Kokoro ships its own voices (`am_michael` by default). Mark's approval covers
  using his voice, but a clone needs his recordings and an explicit decision about the model's
  ownership; neither exists.
- **No barge-in or full duplex.** This is click-to-talk, not a phone call. Real-time interruption,
  echo cancellation and streaming TTS are a separate project (`docs/ROADMAP.md` P4).
- **No wake word, no always-on listening.**

## Limits

- 45 seconds of audio per request; 15 MB upload.
- Integer-second latency is dominated by STT; a `small.en` model would halve it at some accuracy cost
  (`MMDESK_STT_MODEL=small.en`).
- The browser only allows microphone access on `https://` or `localhost`. A Vercel deployment is
  HTTPS, so the mic works there — but Vercel has no GPU, so voice would need a different host or a
  local tunnel. The text path is the deployable one.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MMDESK_VOICE_DIR` | `C:\Users\GAMEPOWER\tools\tts-local` | Kokoro model + voices |
| `MMDESK_STT_MODEL` | `large-v3` | faster-whisper model |
| `MMDESK_STT_DEVICE` | `cuda` | `cuda` or `cpu` |
| `MMDESK_STT_COMPUTE` | `float16` | `float16` / `int8` |
| `MMDESK_TTS_VOICE` | `am_michael` | Kokoro voice id |
| `MMDESK_TTS_SPEED` | `0.98` | speaking rate |

## Install

```bash
uv pip install --python .venv/Scripts/python.exe kokoro-onnx soundfile
# faster-whisper and ffmpeg are already required for transcription
```

## Grounding rule, spoken

The reply sentence is rendered from typed card fields (`voice.reply_text`), never from the language
model. The model's prose is still barred from containing digits, so nothing it writes can be spoken
as a number.
