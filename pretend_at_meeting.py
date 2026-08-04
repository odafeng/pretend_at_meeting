"""
conference-to-briefing — turn a folder of conference/session recordings into:
  1) a per-video summary for every talk            (summarize)
  2) one topic-chaptered master summary            (synthesize)
  3) a source-grounded .pptx briefing deck                         (slides)

Pipeline per video:  ffmpeg (chunked audio) -> Whisper transcript -> GPT summary.
All stages are resumable (existing outputs are skipped).

Usage examples
--------------
  # everything, end to end
  python pretend_at_meeting.py all "D:\\videos" \
      --meeting "ASCRS 2026 Annual Meeting" --dates "May 9-12, 2026" \
      --out "D:\\ascrs_out" --summary-model gpt-5.5

  # individual stages
  python pretend_at_meeting.py summarize "D:\\videos" --out "D:\\ascrs_out"
  python pretend_at_meeting.py synthesize --out "D:\\ascrs_out" --meeting "ASCRS 2026"
  python pretend_at_meeting.py slides --out "D:\\ascrs_out" --meeting "ASCRS 2026" --dates "May 9-12, 2026"

Setup
-----
  pip install -r requirements.txt
  create .env.local next to this script (or in the cwd) containing:
      OPENAI_API_KEY=sk-...
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from natsort import natsorted
from openai import OpenAI

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".m4v", ".webm", ".mpg", ".mpeg"}
CHUNK_SECONDS = 600  # 16k mono wav ~19MB/chunk, under Whisper's 25MB limit


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
#  Setup helpers
# --------------------------------------------------------------------------- #
def load_client():
    for env in (HERE / ".env.local", Path.cwd() / ".env.local"):
        if env.exists():
            load_dotenv(env)
            break
    else:
        load_dotenv()
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        sys.exit("❌ OPENAI_API_KEY not found. Put it in .env.local next to the script.")
    return OpenAI(api_key=key)


def find_ffmpeg(explicit=None):
    """Return (ffmpeg, ffprobe) paths. Search order: arg/env -> bundled -> PATH."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.getenv("FFMPEG_DIR"):
        candidates.append(Path(os.getenv("FFMPEG_DIR")))
    candidates.append(HERE / "ffmpeg" / "bin")
    exe = ".exe" if os.name == "nt" else ""
    for d in candidates:
        ff, fp = d / f"ffmpeg{exe}", d / f"ffprobe{exe}"
        if ff.exists() and fp.exists():
            return str(ff), str(fp)
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ff and fp:
        return ff, fp
    sys.exit(
        "❌ ffmpeg/ffprobe not found. Put them in ./ffmpeg/bin, set FFMPEG_DIR, or add to PATH."
    )


def out_dirs(out):
    out = Path(out)
    return {
        "out": out,
        "summaries": out / "summaries",
        "transcripts": out / "summaries" / "transcripts",
        "audio": out / ".audio_tmp",
    }


# --------------------------------------------------------------------------- #
#  Stage 1: transcribe + per-video summary
# --------------------------------------------------------------------------- #
SUMMARY_PROMPT = """你是專業的會議記錄整理助手。以下是一場「{meeting}」會議場次的逐字稿（原文語言可能為英文）。

場次標題：{title}

請用「{lang}」撰寫摘要；專有名詞、藥名、技術名稱、試驗/研究名稱、縮寫請保留原文。請輸出 Markdown，嚴格依下列結構：

## {title}

**主題分類**：（用一到多個簡短主題標籤描述本場屬於哪個領域，用、分隔）

**一句話定位**：用一句話說明這場在講什麼、對象是誰。

### 內容總結
以 3 段文字概述本場重點脈絡。

### 重點整理
- 條列 5–8 個重點，盡量包含關鍵數據、結果、技術細節、實務建議。

### Take-home messages
- 條列 3 個可立即帶走的結論。

---
逐字稿：
{transcript}
"""


def _retry(fn, what, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            wait = 5 * (i + 1)
            log(f"    {what} error (try {i + 1}): {e} -> retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"{what} failed after {tries} tries")


def make_chunks(ffmpeg, video, wdir):
    wdir.mkdir(parents=True, exist_ok=True)
    existing = natsorted(wdir.glob("chunk_*.wav"))
    if existing:
        return existing
    pattern = str(wdir / "chunk_%03d.wav")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-vn",
        "-f",
        "segment",
        "-segment_time",
        str(CHUNK_SECONDS),
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        pattern,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return natsorted(wdir.glob("chunk_*.wav"))


def transcribe_video(client, ffmpeg, video, wdir, model):
    chunks = make_chunks(ffmpeg, video, wdir)
    log(f"    {len(chunks)} audio chunk(s)")
    parts = []
    for ch in chunks:
        tpath = ch.with_suffix(".txt")
        if tpath.exists() and tpath.stat().st_size > 0:
            parts.append(tpath.read_text(encoding="utf-8"))
            continue

        def do(chunk=ch):
            with open(chunk, "rb") as f:
                return client.audio.transcriptions.create(model=model, file=f)

        r = _retry(do, f"whisper {ch.name}")
        tpath.write_text(r.text, encoding="utf-8")
        parts.append(r.text)
        log(f"    transcribed {ch.name}")
    return "\n".join(parts)


def summarize_one(client, model, meeting, lang, title, transcript):
    prompt = SUMMARY_PROMPT.format(meeting=meeting, title=title, lang=lang, transcript=transcript)

    def do():
        return client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}]
        )

    r = _retry(do, "summary")
    return r.choices[0].message.content.strip()


def stage_summarize(args):
    client = load_client()
    ffmpeg, _ = find_ffmpeg(args.ffmpeg_dir)
    d = out_dirs(args.out)
    for p in (d["summaries"], d["transcripts"], d["audio"]):
        p.mkdir(parents=True, exist_ok=True)

    videos = [v for v in natsorted(Path(args.videos).glob("*")) if v.suffix.lower() in VIDEO_EXTS]
    if args.only:
        videos = [v for v in videos if args.only.lower() in v.name.lower()]
    log(f"Model: summary={args.summary_model}, transcribe={args.transcribe_model}")
    log(f"Processing {len(videos)} video(s) -> {d['summaries']}\n")

    done = 0
    for i, v in enumerate(videos, 1):
        stem = v.stem
        sfile = d["summaries"] / f"{stem}.md"
        if sfile.exists() and sfile.stat().st_size > 200:
            log(f"[{i}/{len(videos)}] SKIP (done): {stem}")
            done += 1
            continue
        log(f"[{i}/{len(videos)}] === {stem} ===")
        t0 = time.time()
        tfile = d["transcripts"] / f"{stem}.txt"
        if tfile.exists() and tfile.stat().st_size > 50:
            transcript = tfile.read_text(encoding="utf-8")
            log("    transcript cached")
        else:
            try:
                transcript = transcribe_video(
                    client, ffmpeg, v, d["audio"] / stem, args.transcribe_model
                )
            except Exception as e:
                log(f"    TRANSCRIBE FAILED: {e}")
                continue
            tfile.write_text(transcript, encoding="utf-8")
            log(f"    transcript -> {tfile.name} ({len(transcript)} chars)")
        try:
            summ = summarize_one(
                client, args.summary_model, args.meeting, args.lang, stem, transcript
            )
        except Exception as e:
            log(f"    SUMMARY FAILED: {e}")
            continue
        sfile.write_text(summ, encoding="utf-8")
        log(f"    summary -> {sfile.name}  ({time.time() - t0:.0f}s)")
        if not args.keep_audio:
            for f in (d["audio"] / stem).glob("chunk_*.wav"):
                with contextlib.suppress(Exception):
                    f.unlink()
        done += 1
    log(f"\nDONE summarize. {done}/{len(videos)} videos have summaries.")


# --------------------------------------------------------------------------- #
#  Stage 2: synthesize master chaptered summary
# --------------------------------------------------------------------------- #
def collect_summaries(d):
    files = natsorted(d["summaries"].glob("*.md"))
    files = [f for f in files if f.name != "MASTER_SUMMARY.md"]
    blocks = []
    for f in files:
        blocks.append(f"<<<TALK: {f.stem}>>>\n{f.read_text(encoding='utf-8')}")
    return files, "\n\n".join(blocks)


SYNTH_PROMPT = """你是「{meeting}」的資深與會學者。下面是這個會議 {n} 場個別場次的摘要（每段以 <<<TALK: 標題>>> 開頭）。

請整合成「一份」結構良好的繁體中文「大摘要」，輸出 Markdown：

# {meeting} — 重點整合摘要

開頭用 2–3 段「總覽 Executive Summary」，點出本屆會議的整體主軸與最重要的趨勢。

接著**依臨床/主題分章節**（你自行歸納合適的章節，例如不同疾病別、技術、品質與照護、研究方法、職涯教育等；相近主題的場次要合併在同一章）。每個章節：
- 用 `## 章節標題`，標題後括號列出歸屬於此章的場次編號/簡名。
- 一段該主題的整體脈絡。
- 條列「跨場次的關鍵重點與共識/爭議」，保留關鍵數據、試驗名稱、技術細節（專有名詞保留原文）。

最後一個章節為 `## 本屆十大 Take-home messages`，條列 8–12 點最值得帶回臨床/研究的結論。

注意：要做「跨場次的整合與歸納」，不是把每場摘要原封不動貼上；相同主題要交叉比較。

---
各場次摘要：
{summaries}
"""


def stage_synthesize(args):
    client = load_client()
    d = out_dirs(args.out)
    files, joined = collect_summaries(d)
    if not files:
        sys.exit(f"❌ No per-video summaries found in {d['summaries']}. Run `summarize` first.")
    log(f"Synthesizing master summary from {len(files)} talk summaries with {args.synth_model}...")
    prompt = SYNTH_PROMPT.format(meeting=args.meeting, n=len(files), summaries=joined)

    def do():
        return client.chat.completions.create(
            model=args.synth_model, messages=[{"role": "user", "content": prompt}]
        )

    r = _retry(do, "synthesize")
    master = r.choices[0].message.content.strip()
    mfile = d["summaries"] / "MASTER_SUMMARY.md"
    mfile.write_text(master, encoding="utf-8")
    log(f"DONE. master summary -> {mfile}")


# --------------------------------------------------------------------------- #
#  Stage 3: source-grounded briefing slides (.pptx)
# --------------------------------------------------------------------------- #
SLIDES_PROMPT = """你要根據「{meeting}」（會期：{dates}）的場次錄影摘要，製作一份可向同事分享的簡報大綱。
語氣專業、直接，清楚區分錄影中明確陳述的內容與整理者的歸納；不得暗示簡報者親自與會或目擊現場互動。
語言：繁體中文，專有名詞保留原文。

請**只輸出 JSON**（不要任何其他文字、不要 code fence），schema 如下：
{{
  "title": "簡報主標題",
  "subtitle": "副標（含會議名與日期）",
  "intro_bullets": ["開場 3-5 點：資料來源、整體趨勢、這份分享的架構"],
  "sections": [
    {{
      "chapter": "章節標題（一個主題）",
      "slides": [
        {{"heading": "投影片標題", "bullets": ["重點 1","重點 2","重點 3-6"], "notes": "講稿備註與來源限制(1-3句)"}}
      ]
    }}
  ],
  "takeaways": ["結尾 take-home 6-10 點"]
}}

規則：
- 依大摘要的主題分 4–8 個 sections，每個 section 1–3 張 slides。
- 每張 slide 的 bullets 控制在 3–6 點、每點精煉（不超過約 30 字），適合投影片呈現。
- notes 補充 slide 上沒寫的細節、臨床觀點或來源限制。
- 內容必須忠實反映大摘要；若摘要沒有提供證據，不得補寫現場氣氛、討論熱度或個人經歷。

---
大摘要內容：
{master}
"""


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s : e + 1]
    return json.loads(text)


# ---- design system ------------------------------------------------------- #
# A calm clinical palette: deep teal primary, warm amber accent, soft neutrals.
THEME = {
    "primary": (0x10, 0x46, 0x52),  # deep teal  — headers, titles
    "primary_dark": (0x0A, 0x30, 0x39),  # darker teal — section dividers bg
    "accent": (0xE0, 0x7A, 0x3C),  # warm amber — rules, bullet marks, numbers
    "text": (0x22, 0x2B, 0x30),  # near-black body text
    "muted": (0x6B, 0x7A, 0x80),  # captions / footer
    "light": (0xF5, 0xF7, 0xF8),  # page background
    "panel": (0xEA, 0xF0, 0xF1),  # subtle panel tint
    "white": (0xFF, 0xFF, 0xFF),
}
FONT = "Microsoft JhengHei"  # clean CJK + Latin face that ships with Windows
FONT_LIGHT = "Microsoft JhengHei Light"


def build_pptx(deck, path, meeting=""):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Inches, Pt

    def C(name):
        return RGBColor(*THEME[name])

    SW, SH = 13.333, 7.5

    prs = Presentation()
    prs.slide_width = Inches(SW)
    prs.slide_height = Inches(SH)
    blank = prs.slide_layouts[6]

    def set_run(run, size, color, bold=False, font=FONT):
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = C(color) if isinstance(color, str) else color
        run.font.name = font
        # make CJK glyphs use the same face (python-pptx only sets the latin run)
        rPr = run._r.get_or_add_rPr()
        for tag in ("a:ea", "a:cs"):
            el = rPr.find(qn(tag))
            if el is None:
                el = rPr.makeelement(qn(tag), {})
                rPr.append(el)
            el.set("typeface", font)

    def bg(slide, color):
        f = slide.background.fill
        f.solid()
        f.fore_color.rgb = C(color)

    def rect(slide, x, y, w, h, color, line=None):
        shp = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h)
        )
        shp.fill.solid()
        shp.fill.fore_color.rgb = C(color) if isinstance(color, str) else color
        if line is None:
            shp.line.fill.background()
        else:
            shp.line.color.rgb = C(line)
            shp.line.width = Pt(1)
        shp.shadow.inherit = False
        return shp

    def textbox(slide, x, y, w, h, anchor=MSO_ANCHOR.TOP):
        tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        tf.margin_left = 0
        tf.margin_right = 0
        tf.margin_top = 0
        tf.margin_bottom = 0
        return tf

    page = {"n": 0}

    def footer(slide):
        page["n"] += 1
        rect(slide, 0.7, 7.02, 11.93, 0.012, "muted")
        tf = textbox(slide, 0.7, 7.08, 10.0, 0.3)
        r = tf.paragraphs[0].add_run()
        r.text = meeting
        set_run(r, 9, "muted")
        tf2 = textbox(slide, 11.6, 7.08, 1.03, 0.3)
        tf2.paragraphs[0].alignment = PP_ALIGN.RIGHT
        r2 = tf2.paragraphs[0].add_run()
        r2.text = str(page["n"])
        set_run(r2, 9, "muted")

    # ---- title slide ---- #
    def add_title(title, subtitle):
        s = prs.slides.add_slide(blank)
        bg(s, "light")
        rect(s, 0, 0, 0.45, SH, "primary")  # left spine
        rect(s, 0, 0, 0.45, SH, "primary")
        rect(s, 1.1, 2.05, 2.0, 0.10, "accent")  # accent rule above title
        tf = textbox(s, 1.05, 2.35, 11.0, 2.4)
        p = tf.paragraphs[0]
        r = p.add_run()
        r.text = title
        set_run(r, 44, "primary", bold=True)
        if subtitle:
            p2 = tf.add_paragraph()
            p2.space_before = Pt(16)
            r2 = p2.add_run()
            r2.text = subtitle
            set_run(r2, 20, "muted", font=FONT_LIGHT)
        cap = textbox(s, 1.05, 6.5, 11.0, 0.4)
        rc = cap.paragraphs[0].add_run()
        rc.text = "整理自會議場次錄影 · Source-grounded briefing"
        set_run(rc, 12, "muted", font=FONT_LIGHT)
        return s

    # ---- section divider ---- #
    def add_divider(num, title):
        s = prs.slides.add_slide(blank)
        bg(s, "primary_dark")
        tf0 = textbox(s, 1.1, 2.55, 11.0, 1.2)
        r0 = tf0.paragraphs[0].add_run()
        r0.text = f"CHAPTER {num:02d}"
        set_run(r0, 18, "accent", bold=True, font=FONT_LIGHT)
        rect(s, 1.13, 3.35, 1.7, 0.09, "accent")
        tf = textbox(s, 1.1, 3.6, 11.1, 2.2)
        r = tf.paragraphs[0].add_run()
        r.text = title
        set_run(r, 34, "white", bold=True)
        footer(s)
        return s

    # ---- content slide ---- #
    def add_content(heading, bullets, notes=None, dark=False):
        s = prs.slides.add_slide(blank)
        bg(s, "primary" if dark else "light")
        if dark:
            rect(s, 0.7, 0.7, 1.6, 0.10, "accent")
            htf = textbox(s, 0.7, 0.95, 11.9, 1.2)
            hr = htf.paragraphs[0].add_run()
            hr.text = heading
            set_run(hr, 28, "white", bold=True)
            mark_color, body_color = C("accent"), C("white")
        else:
            rect(s, 0, 0, SW, 1.5, "primary")  # header band
            rect(s, 0, 1.5, SW, 0.07, "accent")  # accent underline
            htf = textbox(s, 0.7, 0.32, 11.9, 1.0, anchor=MSO_ANCHOR.MIDDLE)
            hr = htf.paragraphs[0].add_run()
            hr.text = heading
            set_run(hr, 26, "white", bold=True)
            mark_color, body_color = C("accent"), C("text")
        btf = textbox(s, 0.85, 1.95, 11.7, 4.9, anchor=MSO_ANCHOR.TOP)
        bl = bullets or []
        size = 20 if len(bl) <= 4 else (18 if len(bl) <= 6 else 16)
        for i, b in enumerate(bl):
            p = btf.paragraphs[0] if i == 0 else btf.add_paragraph()
            p.space_after = Pt(12 if len(bl) <= 5 else 8)
            p.line_spacing = 1.05
            rm = p.add_run()
            rm.text = "▍ "
            set_run(rm, size, mark_color, bold=True)
            rt = p.add_run()
            rt.text = str(b)
            set_run(rt, size, body_color)
        if notes:
            s.notes_slide.notes_text_frame.text = str(notes)
        footer(s)
        return s

    # ---- assemble ---- #
    add_title(deck.get("title", "Meeting Highlights"), deck.get("subtitle", ""))
    if deck.get("intro_bullets"):
        add_content("開場 ｜ Sources, scope & what's ahead", deck["intro_bullets"])
    for idx, sec in enumerate(deck.get("sections", []), 1):
        add_divider(idx, sec.get("chapter", ""))
        for sl in sec.get("slides", []):
            add_content(sl.get("heading", ""), sl.get("bullets", []), sl.get("notes"))
    if deck.get("takeaways"):
        add_content("Take-home messages", deck["takeaways"], dark=True)
    prs.save(str(path))


def stage_slides(args):
    d = out_dirs(args.out)
    outline_path = d["out"] / "slides_outline.json"
    if args.reuse_outline:
        if not outline_path.exists():
            sys.exit(f"❌ {outline_path} not found; cannot --reuse-outline.")
        deck = json.loads(outline_path.read_text(encoding="utf-8"))
        log(f"Reusing existing outline ({outline_path.name}); rebuilding deck design only.")
    else:
        client = load_client()
        mfile = d["summaries"] / "MASTER_SUMMARY.md"
        if not mfile.exists():
            sys.exit(f"❌ {mfile} not found. Run `synthesize` first.")
        master = mfile.read_text(encoding="utf-8")
        log(f"Generating slide outline with {args.synth_model}...")
        prompt = SLIDES_PROMPT.format(meeting=args.meeting, dates=args.dates or "", master=master)

        def do():
            return client.chat.completions.create(
                model=args.synth_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )

        r = _retry(do, "slides outline")
        deck = _extract_json(r.choices[0].message.content)
        outline_path.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    pptx_path = d["out"] / "presentation.pptx"
    footer_text = args.meeting + (f" · {args.dates}" if args.dates else "")
    build_pptx(deck, pptx_path, meeting=footer_text)
    log(f"DONE. deck -> {pptx_path}")


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def add_common(p, need_videos=False):
    if need_videos:
        p.add_argument("videos", help="folder containing the session videos")
    p.add_argument("--out", required=True, help="output folder")
    p.add_argument("--meeting", default="the meeting", help="meeting name")
    p.add_argument("--dates", default="", help="meeting dates, e.g. 'May 9-12, 2026'")
    p.add_argument("--lang", default="繁體中文", help="summary language")
    p.add_argument("--summary-model", default="gpt-5.5")
    p.add_argument("--synth-model", default="gpt-5.5")
    p.add_argument("--transcribe-model", default="whisper-1")
    p.add_argument("--ffmpeg-dir", default=None)
    p.add_argument("--only", default=None, help="(summarize) only videos whose name contains this")
    p.add_argument("--keep-audio", action="store_true")
    p.add_argument(
        "--reuse-outline",
        action="store_true",
        help="(slides) rebuild the deck from existing slides_outline.json without calling GPT",
    )


def main():
    ap = argparse.ArgumentParser(
        description="conference-to-briefing — turn recorded talks into a source-grounded briefing"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, need_v in [
        ("summarize", True),
        ("synthesize", False),
        ("slides", False),
        ("all", True),
    ]:
        sp = sub.add_parser(name)
        add_common(sp, need_videos=need_v)
    args = ap.parse_args()

    if args.cmd == "summarize":
        stage_summarize(args)
    elif args.cmd == "synthesize":
        stage_synthesize(args)
    elif args.cmd == "slides":
        stage_slides(args)
    elif args.cmd == "all":
        stage_summarize(args)
        stage_synthesize(args)
        stage_slides(args)


if __name__ == "__main__":
    main()
