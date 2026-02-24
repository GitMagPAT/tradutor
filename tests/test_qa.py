import json
from pathlib import Path

import fitz

from app.qa import run_qa_scan


def _make_pdf(path: Path, text: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def test_run_qa_scan_generates_top_risky_and_custom_report(tmp_path: Path):
    workdir = tmp_path / "work"
    logs = workdir / "logs"
    logs.mkdir(parents=True)

    (logs / "page_0000.json").write_text(
        json.dumps(
            {
                "page": 0,
                "status": "ok",
                "warnings": ["x"],
                "errors": [],
                "changed_blocks": 1,
                "unchanged_blocks": 9,
                "native_char_count": 400,
                "llm_post_edit_candidates": 5,
                "llm_post_edit_changed": 3,
                "llm_post_edit_rejected_reasons": [{"block_id": "x", "reasons": ["numbers_units_changed"]}],
            }
        ),
        encoding="utf-8",
    )

    out_pdf = tmp_path / "out.pdf"
    _make_pdf(out_pdf, "Clean text")

    custom_report = tmp_path / "reports" / "qa.json"
    cfg = {
        "pipeline": {
            "qa_scan": True,
            "qa_fail_on_zxq": False,
            "qa_unchanged_ratio_warn": 0.85,
            "qa_unchanged_min_chars": 300,
            "qa_report_path": str(custom_report),
            "qa_fail_score_threshold": 0,
        }
    }

    report = run_qa_scan(workdir=workdir, out_pdf=out_pdf, cfg=cfg)
    assert report["enabled"] is True
    assert report["schema_version"] == "qa_report.v2"
    assert report["summary"]["high_unchanged_pages"] == 1
    assert isinstance(report["summary"]["top_risky_pages"], list)
    assert report["summary"]["llm_post_edit_candidates_total"] == 5
    assert report["summary"]["llm_post_edit_changed_total"] == 3
    assert report["summary"]["llm_post_edit_rejected_total"] == 1
    assert report["summary"]["llm_post_edit_rejected_ratio"] == 0.2
    assert custom_report.exists()
