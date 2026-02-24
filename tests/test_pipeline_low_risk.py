from pathlib import Path

import pytest

from app import pipeline
from app.pipeline import (
    _effective_max_cover_area_ratio_native,
    _looks_english_heavy,
    _resolve_native_cover_mode,
    _resolve_render_mode_for_page,
)


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


def test_resolve_render_mode_for_page_auto_falls_back_to_overlay_in_text_pages():
    assert _resolve_render_mode_for_page("pdf_overlay_original", has_images=False, auto_rasterize_text_pages=True) == "pdf_overlay"
    assert _resolve_render_mode_for_page("pdf_overlay_original", has_images=True, auto_rasterize_text_pages=True) == "pdf_overlay_original"


def test_resolve_render_mode_for_page_keeps_configured_when_auto_disabled():
    assert _resolve_render_mode_for_page("pdf_overlay_original", has_images=False, auto_rasterize_text_pages=False) == "pdf_overlay_original"


def test_looks_english_heavy_detects_untranslated_english():
    assert _looks_english_heavy("Mindfulness is about the ability of a system to concentrate on what is going on now") is True


def test_looks_english_heavy_ignores_portuguese_text():
    assert _looks_english_heavy("Mindfulness é sobre a capacidade de um sistema se concentrar no que está acontecendo agora") is False
