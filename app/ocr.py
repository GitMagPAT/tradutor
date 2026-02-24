from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import os
import re

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageOps
import pytesseract
from pytesseract import Output

from .models import TextBlock
from .utils import clean_extracted_text, clamp


def configure_tesseract(tesseract_cmd: Optional[str] = None, tessdata_prefix: Optional[str] = None) -> None:
    """Configura o caminho do executável do Tesseract (Windows geralmente precisa)."""
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    if tessdata_prefix:
        os.environ["TESSDATA_PREFIX"] = tessdata_prefix


def preprocess_image(img: Image.Image) -> Image.Image:
    """Pré-processamento opcional para OCR (pode ajudar em scans ruins).

    ⚠️ Atenção:
    - Pode melhorar scans de baixa qualidade.
    - Pode piorar diagramas/figuras técnicas.
    """
    g = img.convert("L")
    g = ImageOps.autocontrast(g)
    return g.convert("RGB")


def mask_out_rects_pt(
    img: Image.Image,
    rects_pt: List[fitz.Rect],
    scale_x: float,
    scale_y: float,
    fill_rgb: Tuple[int, int, int] = (255, 255, 255),
    pad_pt: float = 1.0,
) -> Image.Image:
    """Apaga (pinta) regiões em coordenadas do PDF (pontos) na imagem renderizada.

    pad_pt:
      Expande cada bbox (em pontos) antes de pintar.
      Isso ajuda a cobrir *antialias* e pequenas imprecisões de bbox.
    """
    out = img.copy()
    draw = ImageDraw.Draw(out)
    pad = float(pad_pt or 0.0)

    for r0 in rects_pt:
        r = fitz.Rect(r0)
        if pad:
            r.x0 -= pad
            r.y0 -= pad
            r.x1 += pad
            r.y1 += pad

        x0 = clamp(int(r.x0 * scale_x), 0, out.width)
        y0 = clamp(int(r.y0 * scale_y), 0, out.height)
        x1 = clamp(int(r.x1 * scale_x), 0, out.width)
        y1 = clamp(int(r.y1 * scale_y), 0, out.height)
        if (x1 - x0) < 2 or (y1 - y0) < 2:
            continue
        draw.rectangle([x0, y0, x1, y1], fill=fill_rgb)
    return out


def _is_probably_noise_ocr_text(text: str) -> bool:
    """Heurística para filtrar *lixo* típico de OCR em diagramas.

    Objetivo:
    - Evitar traduzir / renderizar strings muito curtas (ex.: "A", "B"),
      ou compostas majoritariamente por símbolos (© ® □ etc.).
    - Isso reduz falsos positivos e evita *overlays* que "apagam" o diagrama.

    Observação:
    - Em documentos técnicos, letras isoladas geralmente são *callouts* (A, B, C),
      que não precisam de tradução.
    """
    t = (text or "").strip()
    if not t:
        return True

    # Remoções leves
    t_nospace = re.sub(r"\s+", "", t)
    if len(t_nospace) < 2:
        return True

    # Contagens básicas
    letters = len(re.findall(r"[A-Za-z]", t))
    digits = len(re.findall(r"\d", t))
    alnum = letters + digits
    nonspace = len(t_nospace)

    if alnum == 0:
        return True

    # Callouts muito curtos (A, B, C, FG, RC, etc.) ou itens de legenda
    if len(t_nospace) <= 2 and re.fullmatch(r"[A-Za-z]{1,2}", t_nospace):
        return True
    if re.fullmatch(r"[A-Za-z]\)", t_nospace):
        return True

    # Pouco "conteúdo" vs muitos símbolos: tende a ser ruído
    ratio = alnum / float(nonspace or 1)
    if ratio < 0.30 and letters < 4 and digits < 2:
        return True

    # Sequências com muitos tokens curtíssimos (ex.: "© ® 5° □ © 2 ® © i) ©")
    # costumam ser ruído ou marcações que não agregam na tradução.
    tokens = [tok for tok in re.split(r"\s+", t.strip()) if tok]
    if len(tokens) >= 5:
        short_tokens = sum(1 for tok in tokens if len(tok) <= 2)
        if (short_tokens / len(tokens)) >= 0.80 and letters <= 2 and digits <= 2:
            return True

    # Presença de símbolos de copyright/registro com pouco texto útil
    if any(ch in t_nospace for ch in "©®™") and letters <= 2 and digits <= 2:
        return True

    # Relaxa o critério para pegar linhas dominadas por símbolos
    if ratio < 0.35 and letters <= 2 and digits <= 2:
        return True

    # Sequências de símbolos comuns
    if re.fullmatch(r"[©®°□■▪▫◆◇]+", t_nospace):
        return True

    return False


def ocr_image_to_blocks(
    image: Image.Image,
    page_number: int,
    scale_x: float,
    scale_y: float,
    lang: str = "eng",
    oem: int = 3,
    psm: int = 6,
    min_confidence: int = 40,
    timeout_sec: float = 0.0,
    group_mode: str = "paragraph",
    filter_noise: bool = True,
    return_word_boxes: bool = False,
    cluster_sparse_lines: bool = True,
    cluster_gap_factor: float = 2.5,
) -> List[TextBlock]:
    """Faz OCR com Tesseract e retorna blocos com bounding boxes em *pontos*.

    group_mode:
      - "paragraph" (padrão): agrupa por (block_num, par_num). Melhor para páginas escaneadas
        com texto corrido (menos chamadas ao tradutor).
      - "line": agrupa por (block_num, par_num, line_num). Melhor para texto em figuras/diagramas
        (evita caixas gigantes que "apagam" o desenho).

    return_word_boxes:
      - quando True, também retorna sub-retângulos (por palavra) em TextBlock.cover_rects.
        Isso é usado no render para "apagar" o texto original com precisão (baixo risco
        e reduz muito os "retângulos brancos" em diagramas).

    Observação:
    - O Tesseract retorna palavras com (block_num, par_num, line_num).
    """
    group_mode = (group_mode or "paragraph").strip().lower()
    if group_mode not in ("paragraph", "line"):
        group_mode = "paragraph"

    config = f"--oem {int(oem)} --psm {int(psm)}"
    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config=config,
        output_type=Output.DICT,
        timeout=float(timeout_sec or 0.0),
    )

    n = len(data.get("text", []))
    groups: Dict[Tuple[int, int, int], List[int]] = {}

    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0
        if conf < float(min_confidence):
            continue

        bnum = int(data["block_num"][i])
        pnum = int(data["par_num"][i])
        lnum = int(data["line_num"][i])

        if group_mode == "line":
            key = (bnum, pnum, lnum)
        else:
            # paragraph
            key = (bnum, pnum, -1)

        groups.setdefault(key, []).append(i)

    blocks: List[TextBlock] = []
    for (block_num, par_num, line_num), idxs in groups.items():
        words = []
        x0s: List[int] = []
        y0s: List[int] = []
        x1s: List[int] = []
        y1s: List[int] = []
        confs: List[float] = []
        cover_rects: List[fitz.Rect] = []

        # Ordena por linha + x (para "paragraph") ou só por x (para "line")
        if group_mode == "line":
            idxs_sorted = sorted(idxs, key=lambda i: int(data["left"][i]))
        else:
            idxs_sorted = sorted(idxs, key=lambda i: (int(data["line_num"][i]), int(data["left"][i])))

        for i in idxs_sorted:
            word = (data["text"][i] or "").strip()
            if not word:
                continue
            left, top = int(data["left"][i]), int(data["top"][i])
            w, h = int(data["width"][i]), int(data["height"][i])
            x0s.append(left)
            y0s.append(top)
            x1s.append(left + w)
            y1s.append(top + h)
            try:
                confs.append(float(data["conf"][i]))
            except Exception:
                pass
            words.append((int(data["line_num"][i]), left, word))

            # Para páginas com imagens/diagramas: guardamos caixas por palavra
            # para "apagar" o texto original com precisão, evitando cobrir
            # áreas grandes do desenho.
            if return_word_boxes:
                x0_pt = left / scale_x if scale_x else left
                y0_pt = top / scale_y if scale_y else top
                x1_pt = (left + w) / scale_x if scale_x else (left + w)
                y1_pt = (top + h) / scale_y if scale_y else (top + h)
                rr = fitz.Rect(float(x0_pt), float(y0_pt), float(x1_pt), float(y1_pt))
                # ignora caixas degeneradas
                if rr.width > 0.5 and rr.height > 0.5:
                    cover_rects.append(rr)

        if not words:
            continue
        # (v0.3.x) Em 'line' OCR, clusteriza palavras muito afastadas em subgrupos (útil p/ diagramas).
        if (
            cluster_sparse_lines
            and group_mode == "line"
            and return_word_boxes
            and len(words) >= 3
            and len(cover_rects) == len(words)
        ):
            items = []  # (x0, x1, y0, y1, word, conf, rect)
            for ((_, __left, wtxt), rc, c) in zip(words, cover_rects, confs):
                items.append((float(rc.x0), float(rc.x1), float(rc.y0), float(rc.y1), str(wtxt), float(c), rc))
            items.sort(key=lambda t: (t[2], t[0]))
            heights = [abs(t[3] - t[2]) for t in items if abs(t[3] - t[2]) > 0]
            heights_sorted = sorted(heights)
            h_med = heights_sorted[len(heights_sorted) // 2] if heights_sorted else 10.0
            gap_thr = float(cluster_gap_factor) * float(h_med)
            clusters = []
            cur = [items[0]]
            prev_end = float(items[0][1])
            for it in items[1:]:
                gap = float(it[0]) - float(prev_end)
                if gap > gap_thr:
                    clusters.append(cur)
                    cur = [it]
                    prev_end = float(it[1])
                else:
                    cur.append(it)
                    prev_end = max(float(prev_end), float(it[1]))
            clusters.append(cur)
        
            for ci, cl in enumerate(clusters):
                cl_words = [it[4].strip() for it in cl if it[4].strip()]
                if not cl_words:
                    continue
                paragraph_text = clean_extracted_text(" ".join(cl_words))
                if len(paragraph_text.strip()) < min_chars_block:
                    continue
                if filter_noise and _is_probably_noise_ocr_text(paragraph_text):
                    continue
                x0 = min(it[0] for it in cl)
                y0 = min(it[2] for it in cl)
                x1 = max(it[1] for it in cl)
                y1 = max(it[3] for it in cl)
                rect = fitz.Rect(x0, y0, x1, y1)
                conf = float(sum(it[5] for it in cl) / max(1, len(cl)))
                cl_cover = [it[6] for it in cl] if return_word_boxes else None
                out.append(
                    TextBlock(
                        block_id=f"ocr_{page_number:04d}_{block_num:03d}_{par_num:02d}_{line_num:03d}_c{ci:02d}",
                        page_number=page_number,
                        rect=rect,
                        text=paragraph_text,
                        source="ocr",
                        cover_rects=cl_cover,
                        confidence=conf,
                        meta={"block": block_num, "par": par_num, "line": line_num, "clustered": True},
                    )
                )
            continue

        if group_mode == "line":
            # Uma linha (line_num fixo)
            text_lines = [" ".join([w[2] for w in words])]
        else:
            # Reconstrói texto com quebras de linha aproximadas
            text_lines: List[str] = []
            current_line = words[0][0]
            line_words: List[str] = []
            for lnum, _left, word in words:
                if lnum != current_line:
                    text_lines.append(" ".join(line_words))
                    line_words = [word]
                    current_line = lnum
                else:
                    line_words.append(word)
            if line_words:
                text_lines.append(" ".join(line_words))

        paragraph_text = clean_extracted_text("\n".join(text_lines))
        if len(paragraph_text) < 2:
            continue

        if filter_noise and _is_probably_noise_ocr_text(paragraph_text):
            continue

        # bbox em pixels
        x0_px, y0_px, x1_px, y1_px = min(x0s), min(y0s), max(x1s), max(y1s)

        # converte p/ pontos
        x0_pt = x0_px / scale_x if scale_x else x0_px
        y0_pt = y0_px / scale_y if scale_y else y0_px
        x1_pt = x1_px / scale_x if scale_x else x1_px
        y1_pt = y1_px / scale_y if scale_y else y1_px

        rect = fitz.Rect(float(x0_pt), float(y0_pt), float(x1_pt), float(y1_pt))
        avg_conf = sum(confs) / len(confs) if confs else None

        # id: inclui line_num quando group_mode="line"
        if group_mode == "line":
            bid = f"ocr_{page_number:04d}_{block_num}_{par_num}_{line_num}"
        else:
            bid = f"ocr_{page_number:04d}_{block_num}_{par_num}"

        blocks.append(
            TextBlock(
                rect=rect,
                text=paragraph_text,
                source="ocr",
                page_number=page_number,
                block_id=bid,
                confidence=avg_conf,
                cover_rects=cover_rects if (return_word_boxes and cover_rects) else None,
            )
        )

    return blocks
