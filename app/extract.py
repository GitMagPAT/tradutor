from __future__ import annotations

from typing import Dict, List, Tuple

import fitz  # PyMuPDF

from .models import TextBlock
from .utils import clean_extracted_text


def extract_native_text_blocks(
    page: fitz.Page,
    page_number: int,
    min_chars_block: int = 5,
    *,
    has_images: bool = False,
    include_cover_rects: bool = True,
    cover_mode: str = "line",
    split_sparse_blocks: bool = True,
    sparse_min_area_ratio: float = 0.04,
    sparse_max_chars: int = 120,
    sparse_max_words: int = 30,
    cluster_gap_factor: float = 2.5,
) -> List[TextBlock]:
    """
    Extrai blocos de texto nativo (vetorial) do PDF.

    Melhorias (v0.3.x):
    - Gera `cover_rects` em nível de linha/palavra para reduzir “lacunas” (caixas brancas enormes).
    - Em páginas com imagens (diagramas), divide blocos “esparsos” (bbox grande, pouco texto)
      em sub-blocos menores (por linha/cluster) para posicionamento mais fiel.
    """
    blocks = page.get_text("blocks") or []

    # words: [x0,y0,x1,y1,word,block_no,line_no,word_no]
    words = []
    if include_cover_rects or split_sparse_blocks:
        try:
            words = page.get_text("words") or []
        except Exception:
            words = []

    words_by_block: Dict[int, List[tuple]] = {}
    for w in words:
        if len(w) < 8:
            continue
        try:
            bno = int(w[5])
        except Exception:
            continue
        words_by_block.setdefault(bno, []).append(w)

    page_area = max(1.0, float(page.rect.width * page.rect.height))

    out: List[TextBlock] = []

    for b in blocks:
        if len(b) < 5:
            continue

        x0, y0, x1, y1, raw_text = b[:5]
        rect = fitz.Rect(x0, y0, x1, y1)

        block_no = None
        if len(b) > 5:
            try:
                block_no = int(b[5])
            except Exception:
                block_no = None

        if not raw_text or not str(raw_text).strip():
            continue

        cleaned = clean_extracted_text(str(raw_text))
        if len(cleaned.strip()) < min_chars_block:
            continue

        wlist = words_by_block.get(block_no, []) if block_no is not None else []
        # fallback: se não temos block_no ou não achou, tenta coletar palavras que intersectam o bbox
        if not wlist and words:
            for w in words:
                if len(w) < 5:
                    continue
                wx0, wy0, wx1, wy1 = w[0], w[1], w[2], w[3]
                if rect.intersects(fitz.Rect(wx0, wy0, wx1, wy1)):
                    wlist.append(w)

        # Métricas para decidir split em diagramas
        area_ratio = float(rect.width * rect.height) / page_area
        char_count = len(cleaned)
        word_count = len([w for w in wlist if str(w[4]).strip()]) if wlist else 0

        is_sparse = (
            bool(split_sparse_blocks)
            and bool(has_images)
            and bool(wlist)
            and area_ratio >= float(sparse_min_area_ratio)
            and char_count <= int(sparse_max_chars)
            and word_count >= 2
            and word_count <= int(sparse_max_words)
        )

        if is_sparse:
            # Divide por linha (line_no) e, dentro da linha, cluster por espaçamento em X (para labels distantes)
            by_line: Dict[int, List[tuple]] = {}
            for w in wlist:
                try:
                    lno = int(w[6])
                except Exception:
                    lno = 0
                by_line.setdefault(lno, []).append(w)

            for lno, line_words in sorted(by_line.items(), key=lambda kv: kv[0]):
                # ordena por X
                line_words = sorted(line_words, key=lambda w: (w[0], w[1]))

                # threshold baseado na altura típica da palavra
                heights = [(w[3] - w[1]) for w in line_words if (w[3] - w[1]) > 0]
                heights_sorted = sorted(heights)
                h_med = heights_sorted[len(heights_sorted) // 2] if heights_sorted else 10.0
                gap_thr = float(cluster_gap_factor) * float(h_med)

                clusters: List[List[tuple]] = []
                cur: List[tuple] = []
                prev_end = None
                for w in line_words:
                    if not str(w[4]).strip():
                        continue
                    if not cur:
                        cur = [w]
                        prev_end = float(w[2])
                        continue
                    gap = float(w[0]) - float(prev_end)
                    if gap > gap_thr:
                        clusters.append(cur)
                        cur = [w]
                        prev_end = float(w[2])
                    else:
                        cur.append(w)
                        prev_end = max(float(prev_end), float(w[2]))
                if cur:
                    clusters.append(cur)

                for ci, cl in enumerate(clusters):
                    words_txt = [str(w[4]).strip() for w in cl if str(w[4]).strip()]
                    if not words_txt:
                        continue

                    txt = " ".join(words_txt)
                    rx0 = min(float(w[0]) for w in cl)
                    ry0 = min(float(w[1]) for w in cl)
                    rx1 = max(float(w[2]) for w in cl)
                    ry1 = max(float(w[3]) for w in cl)
                    cl_rect = fitz.Rect(rx0, ry0, rx1, ry1)

                    cover_rects = None
                    if include_cover_rects:
                        if str(cover_mode).lower() == "word":
                            cover_rects = [fitz.Rect(float(w[0]), float(w[1]), float(w[2]), float(w[3])) for w in cl]
                        else:
                            cover_rects = [cl_rect]

                    out.append(
                        TextBlock(
                            block_id=f"nat_{page_number:04d}_{(block_no or 0):03d}_{lno:03d}_{ci:02d}",
                            page_number=page_number,
                            rect=cl_rect,
                            text=txt,
                            source="native",
                            cover_rects=cover_rects,
                            meta={
                                "split_sparse": True,
                                "area_ratio": area_ratio,
                                "char_count": char_count,
                                "word_count": word_count,
                            },
                        )
                    )
            continue  # já gerou sub-blocos, não adiciona bloco “grande”

        # Bloco “normal”
        cover_rects = None
        if include_cover_rects and wlist:
            mode = str(cover_mode).lower()
            if mode == "word":
                cover_rects = [fitz.Rect(float(w[0]), float(w[1]), float(w[2]), float(w[3])) for w in wlist]
            elif mode == "line":
                by_line: Dict[int, List[tuple]] = {}
                for w in wlist:
                    try:
                        lno = int(w[6])
                    except Exception:
                        lno = 0
                    if not str(w[4]).strip():
                        continue
                    by_line.setdefault(lno, []).append(w)
                cover_rects = []
                for _, lw in sorted(by_line.items(), key=lambda kv: kv[0]):
                    rx0 = min(float(w[0]) for w in lw)
                    ry0 = min(float(w[1]) for w in lw)
                    rx1 = max(float(w[2]) for w in lw)
                    ry1 = max(float(w[3]) for w in lw)
                    cover_rects.append(fitz.Rect(rx0, ry0, rx1, ry1))

        out.append(
            TextBlock(
                block_id=f"nat_{page_number:04d}_{(block_no or len(out)):03d}",
                page_number=page_number,
                rect=rect,
                text=cleaned,
                source="native",
                cover_rects=cover_rects,
                meta={
                    "area_ratio": area_ratio,
                    "char_count": char_count,
                    "word_count": word_count,
                },
            )
        )

    # ordena para leitura
    out.sort(key=lambda b: (round(b.rect.y0 / 10) * 10, b.rect.x0))
    return out
