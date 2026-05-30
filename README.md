# 🎓 pretend_at_meeting

把「一整個資料夾的會議/演講錄影」變成：

1. **每場個別摘要** （`summarize`）
2. **依主題分章節的總大摘要** （`synthesize`）
3. **一份 `.pptx` 簡報**，以「我親自去開了這場會、回來分享所學」的第一人稱口吻呈現 （`slides`）

逐部管線：`ffmpeg` 切段音訊 → OpenAI Whisper 逐字稿 → GPT 摘要。所有階段**可中斷續跑**（已存在的產物會自動略過）。

---

## 安裝

```bash
pip install -r requirements.txt
```

複製 `.env.example` 為 `.env.local`（放在本資料夾或執行目錄），填入你的金鑰：

```
OPENAI_API_KEY=sk-...
```

> `.env.local` 已被 `.gitignore` 排除，不會被提交。

**ffmpeg（未包含在 repo 中）**：本工具需要 `ffmpeg` 與 `ffprobe`。任選一種方式提供：

1. 確保 `ffmpeg`/`ffprobe` 在系統 PATH（最簡單）；或
2. 把它們放到 `./ffmpeg/bin/`（此資料夾已被 `.gitignore` 排除）；或
3. 設定環境變數 `FFMPEG_DIR`，或執行時加 `--ffmpeg-dir <路徑>`。

Windows 可至 https://www.gyan.dev/ffmpeg/builds/ 下載 essentials build。

---

## 使用

一鍵跑完整條：

```bash
python pretend_at_meeting.py all "D:\videos" ^
    --meeting "ASCRS 2026 Annual Meeting" --dates "May 9-12, 2026" ^
    --out "D:\ascrs_out"
```

分階段執行（方便除錯或重跑單一步驟）：

```bash
python pretend_at_meeting.py summarize  "D:\videos" --out "D:\ascrs_out"
python pretend_at_meeting.py synthesize            --out "D:\ascrs_out" --meeting "ASCRS 2026 Annual Meeting"
python pretend_at_meeting.py slides                --out "D:\ascrs_out" --meeting "ASCRS 2026 Annual Meeting" --dates "May 9-12, 2026"
```

只處理部分影片（測試用）：

```bash
python pretend_at_meeting.py summarize "D:\videos" --out "D:\ascrs_out" --only "Anal Cancer"
```

---

## 參數

| 參數 | 預設 | 說明 |
|---|---|---|
| `--out` | （必填）| 輸出資料夾 |
| `--meeting` | the meeting | 會議名稱（用於摘要與簡報語氣）|
| `--dates` | （空）| 會期，例如 `May 9-12, 2026` |
| `--lang` | 繁體中文 | 摘要語言 |
| `--summary-model` | gpt-5.5 | 個別場次摘要模型 |
| `--synth-model` | gpt-5.5 | 大摘要 + 簡報大綱模型 |
| `--transcribe-model` | whisper-1 | 轉錄模型 |
| `--only` | — | 只處理檔名含此字串的影片 |
| `--keep-audio` | off | 保留中間 wav（預設轉錄後刪除省空間）|

---

## 輸出結構

```
<out>/
├── summaries/
│   ├── <每場標題>.md          ← 個別摘要
│   ├── MASTER_SUMMARY.md      ← 分章節大摘要
│   └── transcripts/<每場>.txt ← 逐字稿
├── slides_outline.json        ← 簡報大綱（JSON）
└── presentation.pptx          ← 最終簡報
```

中間音檔放在 `<out>/.audio_tmp`，轉錄完成後自動清除。

---

## 成本（OpenAI API，依量計費）

- Whisper：約 **US$0.006 / 分鐘**音訊。
- GPT 摘要/合成：依模型與長度而定。

> 範例：約 39 小時影片（39 場）的轉錄約 US$14，加上 gpt-5.5 摘要與合成。

---

## 注意

- 每段音檔切 10 分鐘（16kHz mono wav ≈ 19MB），避開 Whisper 25MB 上限。
- 工具來源 pipeline 衍生自 `video_summarizer`，擴充為「個別摘要 → 分章節合成 → 模擬與會簡報」。
