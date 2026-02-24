from pathlib import Path

import pytest

from app import pipeline
from app.pipeline import _resolve_native_cover_mode, _effective_max_cover_area_ratio_native
from app.pipeline import _resolve_native_cover_mode, _effective_max_cover_area_ratio_native
from app.pipeline import _resolve_native_cover_mode
        main


def test_resolve_native_cover_mode_auto_switches_without_images():
    assert _resolve_native_cover_mode(False, "line", True) == "block"
    assert _resolve_native_cover_mode(False, "word", True) == "block"


def test_resolve_native_cover_mode_respects_mode_with_images():
    assert _resolve_native_cover_mode(True, "line", True) == "line"
    assert _resolve_native_cover_mode(True, "word", True) == "word"


def test_resolve_native_cover_mode_falls_back_for_invalid_mode():
    assert _resolve_native_cover_mode(True, "invalid", True) == "line"


def test_effective_max_cover_area_ratio_native_auto_unlimited_on_text_pages():
    assert _effective_max_cover_area_ratio_native(False, 0.5, True) == 1.0
    assert _effective_max_cover_area_ratio_native(True, 0.5, True) == 0.5
    assert _effective_max_cover_area_ratio_native(False, 0.5, False) == 0.5


def test_merge_page_pdfs_shows_clear_message_when_output_is_locked(monkeypatch, tmp_path: Path):
    class DummyDoc:
        def insert_pdf(self, _):
            return None

        def save(self, *_args, **_kwargs):
            (tmp_path / "out.pdf.tmp").write_bytes(b"%PDF-1.7")

        def close(self):
            return None

    monkeypatch.setattr(pipeline.fitz, "open", lambda *_args, **_kwargs: DummyDoc())
    monkeypatch.setattr(Path, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("locked")))

    with pytest.raises(RuntimeError, match="Feche o arquivo no visualizador"):
        pipeline._merge_page_pdfs([], tmp_path / "out.pdf")
        main
