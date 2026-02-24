from __future__ import annotations

from enum import Enum

import fitz  # PyMuPDF


class PageType(str, Enum):
    NATIVE = "native"     # texto copiável
    SCANNED = "scanned"   # imagem/scan (sem texto copiável)
    HYBRID = "hybrid"     # mistura


def detect_page_type(page: fitz.Page, min_text_chars_native: int = 40) -> PageType:
    """Heurística simples e robusta para classificar a página."""
    try:
        txt = page.get_text("text") or ""
    except Exception:
        txt = ""

    txt = "".join(ch for ch in txt if ch.strip())
    has_native = len(txt) >= int(min_text_chars_native)

    try:
        imgs = page.get_images(full=True) or []
    except Exception:
        imgs = []

    has_images = len(imgs) > 0

    if has_native and has_images:
        return PageType.HYBRID
    if has_native:
        return PageType.NATIVE
    # sem texto nativo: assume scan/imagem (mesmo se não detectar imagens por algum motivo)
    return PageType.SCANNED


def detect_page_features(page: fitz.Page, min_text_chars_native: int = 40) -> tuple[PageType, bool, bool, int]:
    """Detecta tipo de página + algumas *features* úteis.

    Retorna:
      (page_type, has_native_text, has_images, native_char_count)

    Motivo:
    - A heurística "NATIVE sem imagens" é comum em livros/artigos: nesses casos,
      fazer OCR adicional para "texto dentro de imagens" é desperdício.
    - Para performance, queremos decidir OCR de forma mais inteligente.
    """
    try:
        txt = page.get_text("text") or ""
    except Exception:
        txt = ""

    # conta só caracteres não-espaço
    txt_compact = "".join(ch for ch in txt if ch.strip())
    native_char_count = len(txt_compact)

    has_native = native_char_count >= int(min_text_chars_native)

    try:
        imgs = page.get_images(full=True) or []
    except Exception:
        imgs = []
    has_images = len(imgs) > 0

    if has_native and has_images:
        return PageType.HYBRID, True, True, native_char_count
    if has_native:
        return PageType.NATIVE, True, has_images, native_char_count
    return PageType.SCANNED, False, has_images, native_char_count
