# Legacy: the single-corpus research pipeline

These scripts predate tenants. They built **one** index — `corpus/passages.jsonl` — from one
folder of transcripts, for the research corpus this product started from. They are kept because
that corpus is private and may need rebuilding; they are not part of the app, not exercised by
the test suite, and they need tools the app does not (`yt-dlp`, `ffmpeg`, a GPU for
transcription).

New material goes through the tenant pipeline instead:

```bash
python scripts/ingest_corpus.py --tenant <id> --src ./material --title "Title"
```

| File | What it does |
|---|---|
| `transcribe.py` | transcribe one audio file with faster-whisper |
| `batch_transcribe.py` | transcribe a queue of downloaded audio, resumable |
| `build_index.py` | window the transcripts into passages and write the single index |
| `serve_adapter.py` | the first, single-tenant adapter server; `scripts/serve_lens.py` replaces it |
| `video-ids.txt` | the list of source video ids the research corpus was drawn from |

`build_index.py` uses the application's tokenizer (`retrieval.tokens`); it used to carry its own
copy with a different stop list from the one queries used.
