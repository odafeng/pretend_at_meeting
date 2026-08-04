import subprocess
import sys
from pathlib import Path

import pretend_at_meeting as app


def test_extract_json_accepts_fenced_response():
    assert app._extract_json('```json\n{"title": "Briefing"}\n```') == {"title": "Briefing"}


def test_collect_summaries_excludes_master(tmp_path):
    dirs = app.out_dirs(tmp_path)
    dirs["summaries"].mkdir(parents=True)
    (dirs["summaries"] / "2-talk.md").write_text("second", encoding="utf-8")
    (dirs["summaries"] / "10-talk.md").write_text("tenth", encoding="utf-8")
    (dirs["summaries"] / "MASTER_SUMMARY.md").write_text("master", encoding="utf-8")

    files, joined = app.collect_summaries(dirs)

    assert [file.name for file in files] == ["2-talk.md", "10-talk.md"]
    assert "MASTER_SUMMARY" not in joined
    assert "<<<TALK: 2-talk>>>" in joined


def test_retry_returns_after_transient_failure(monkeypatch):
    attempts = iter([RuntimeError("temporary"), "ok"])
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)

    def flaky_call():
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    assert app._retry(flaky_call, "test") == "ok"


def test_slide_prompt_does_not_claim_in_person_attendance():
    prompt = app.SLIDES_PROMPT.format(meeting="ExampleConf", dates="2026", master="summary")

    assert "不得暗示簡報者親自與會" in prompt
    assert "來源限制" in prompt


def test_build_pptx_smoke(tmp_path):
    output = tmp_path / "briefing.pptx"
    app.build_pptx(
        {
            "title": "Example Briefing",
            "subtitle": "Based on recorded sessions",
            "intro_bullets": ["Source: recordings"],
            "sections": [
                {
                    "chapter": "Methods",
                    "slides": [{"heading": "Finding", "bullets": ["Evidence"]}],
                }
            ],
            "takeaways": ["Verify against the source"],
        },
        output,
        meeting="ExampleConf",
    )

    assert output.exists()
    assert output.stat().st_size > 0


def test_cli_help_smoke():
    result = subprocess.run(
        [sys.executable, str(Path(app.__file__)), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "conference-to-briefing" in result.stdout
