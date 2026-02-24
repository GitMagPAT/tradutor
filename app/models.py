from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List, Dict, Any

import fitz  # PyMuPDF


@dataclass
class TextBlock:
    """Um bloco transladável com bounding box em coordenadas do PDF (pontos)."""

    rect: fitz.Rect
    text: str
    source: str  # native | ocr
    page_number: int
    block_id: str
    confidence: Optional[float] = None
    # Para OCR em imagens/diagramas: lista opcional de sub-retângulos (palavras/linhas)
    # usados APENAS para "apagar" o texto original com precisão, evitando cobrir áreas
    # grandes do desenho/figura com caixas opacas.
    cover_rects: Optional[List[fitz.Rect]] = None
    meta: Optional[Dict[str, Any]] = None

    def short(self, n: int = 60) -> str:
        t = " ".join((self.text or "").split())
        return t if len(t) <= n else (t[: n - 1] + "…")
