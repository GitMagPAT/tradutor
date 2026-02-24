from app.pipeline import _resolve_native_cover_mode, _effective_max_cover_area_ratio_native
from app.pipeline import _resolve_native_cover_mode


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
