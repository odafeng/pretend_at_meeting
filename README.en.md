# 🎓 pretend_at_meeting

**English** · [繁體中文](README.md)

Turn a whole folder of conference / lecture recordings into:

1. **A per-talk summary** for every session (`summarize`)
2. **One topic-chaptered master summary** (`synthesize`)
3. **A styled `.pptx` deck**, narrated in the first person as if *you* attended the meeting and came back to share what you learned (`slides`)

Per-video pipeline: `ffmpeg` (chunked audio) → OpenAI Whisper transcript → GPT summary. Every stage is **resumable** (existing outputs are skipped).

---

## Install

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env.local` (next to the script or in your working directory) and fill in your key:

```
OPENAI_API_KEY=sk-...
```

> `.env.local` is git-ignored and never committed.

**ffmpeg (not included in the repo):** the tool needs `ffmpeg` and `ffprobe`. Provide them in any of these ways:

1. Put `ffmpeg`/`ffprobe` on your system PATH (simplest); or
2. Place them in `./ffmpeg/bin/` (this folder is git-ignored); or
3. Set the `FFMPEG_DIR` environment variable, or pass `--ffmpeg-dir <path>` at runtime.

On Windows you can grab an "essentials" build from https://www.gyan.dev/ffmpeg/builds/ .

---

## Usage

End-to-end:

```bash
python pretend_at_meeting.py all "D:\videos" ^
    --meeting "ASCRS 2026 Annual Meeting" --dates "May 9-12, 2026" ^
    --out "D:\ascrs_out"
```

Run individual stages (handy for debugging or re-running one step):

```bash
python pretend_at_meeting.py summarize  "D:\videos" --out "D:\ascrs_out"
python pretend_at_meeting.py synthesize            --out "D:\ascrs_out" --meeting "ASCRS 2026 Annual Meeting"
python pretend_at_meeting.py slides                --out "D:\ascrs_out" --meeting "ASCRS 2026 Annual Meeting" --dates "May 9-12, 2026"
```

Process only some videos (for testing):

```bash
python pretend_at_meeting.py summarize "D:\videos" --out "D:\ascrs_out" --only "Anal Cancer"
```

Rebuild the deck design from an existing outline without calling GPT again:

```bash
python pretend_at_meeting.py slides --out "D:\ascrs_out" --meeting "ASCRS 2026" --reuse-outline
```

---

## Options

| Option | Default | Description |
|---|---|---|
| `--out` | (required) | Output folder |
| `--meeting` | the meeting | Meeting name (used in summaries & slide narration) |
| `--dates` | (empty) | Meeting dates, e.g. `May 9-12, 2026` |
| `--lang` | 繁體中文 | Summary language |
| `--summary-model` | gpt-5.5 | Model for per-talk summaries |
| `--synth-model` | gpt-5.5 | Model for the master summary + slide outline |
| `--transcribe-model` | whisper-1 | Transcription model |
| `--only` | — | Only process videos whose filename contains this string |
| `--keep-audio` | off | Keep intermediate wav files (deleted after transcription by default) |
| `--reuse-outline` | off | (slides) Rebuild the deck from existing `slides_outline.json` without calling GPT |

---

## Output layout

```
<out>/
├── summaries/
│   ├── <talk title>.md        ← per-talk summary
│   ├── MASTER_SUMMARY.md       ← topic-chaptered master summary
│   └── transcripts/<talk>.txt  ← transcripts
├── slides_outline.json         ← slide outline (JSON)
└── presentation.pptx           ← final deck
```

Intermediate audio lives in `<out>/.audio_tmp` and is removed automatically after transcription.

---

## Cost (OpenAI API, usage-based)

- Whisper: about **US$0.006 / minute** of audio.
- GPT summary/synthesis: depends on model and length.

> Example: ~39 hours of video (39 sessions) ≈ US$14 for transcription, plus gpt-5.5 for summaries and synthesis.

---

## Notes

- Audio is chunked into 10-minute segments (16 kHz mono wav ≈ 19 MB) to stay under Whisper's 25 MB limit.
- The deck uses a deep-teal + amber design system with header bands, full-bleed chapter dividers, accent bullets, and footer page numbers (font: Microsoft JhengHei).
- The pipeline is derived from `video_summarizer`, extended into "per-talk summaries → topic-chaptered synthesis → first-person meeting deck".

## License

MIT — see [LICENSE](LICENSE). ffmpeg is licensed separately and is not bundled.
