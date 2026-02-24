from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable, List, Tuple

import fitz  # PyMuPDF


_whitespace_re = re.compile(r"[ \t]+")
_newline_re = re.compile(r"\r\n|\r")

# Normalizações úteis para texto extraído de PDFs:
# - alguns PDFs vêm com ligaturas Unicode (ﬁ, ﬂ, etc.), que prejudicam OCR/MT
# - espaços não-quebráveis (NBSP) atrapalham tokenização
_LIGATURE_MAP = {
    ord("\ufb00"): "ff",
    ord("\ufb01"): "fi",
    ord("\ufb02"): "fl",
    ord("\ufb03"): "ffi",
    ord("\ufb04"): "ffl",
    ord("\ufb05"): "ft",
    ord("\ufb06"): "st",
}


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def clean_extracted_text(text: str) -> str:
    """Normaliza texto extraído (nativo/OCR) para melhorar tradução."""
    if not text:
        return ""

    text = _newline_re.sub("\n", text)
    # Espaços não-quebráveis e similares atrapalham tokenização
    text = text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")
    # Substitui ligaduras Unicode por letras comuns (melhora tradução)
    text = text.translate(_LIGATURE_MAP)
    # Algumas variações de hífen/menos aparecem em PDFs escaneados
    text = text.replace("\u2011", "-").replace("\u2212", "-")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    merged: List[str] = []
    for ln in lines:
        if not merged:
            merged.append(ln)
            continue

        prev = merged[-1]
        # Heurística simples: hifenização no final da linha
        if prev.endswith("-") and ln[:1].islower():
            merged[-1] = prev[:-1] + ln
        else:
            merged.append(ln)

    # Heurística v0.2.6:
    # - Trechos de SUMÁRIO/ÍNDICE normalmente têm *leader dots* ("... ... ...")
    #   e/ou várias linhas com número no final.
    # - Nesses casos, preservar as quebras de linha melhora:
    #     (a) a tradução (cada item vira quase uma "frase" independente)
    #     (b) o render no overlay (mantém uma aparência similar ao original)
    # - Para texto corrido (parágrafos), continuamos juntando linhas.
    is_toc_like = False
    if len(lines) >= 2:
        leader_hits = 0
        page_hits = 0
        for ln in lines:
            if re.search(r"(?:\.\s*){5,}", ln) or re.search(r"\.{5,}", ln):
                leader_hits += 1
            if re.search(r"\s+(?:\d+|[ivxlcdm]+)\s*$", ln, flags=re.IGNORECASE):
                page_hits += 1
        # 1+ linhas com leader dots OU 2+ linhas terminando em número/romano
        if leader_hits >= 1 or page_hits >= 2:
            is_toc_like = True

    if is_toc_like:
        # Mantém cada item em sua própria linha
        merged_norm = [_whitespace_re.sub(" ", ln).strip() for ln in merged]
        return "\n".join(merged_norm).strip()

    joined = " ".join(merged)
    joined = _whitespace_re.sub(" ", joined).strip()

    # Ajuste leve: alguns extratores deixam espaço antes de pontuação
    # (ex.: "word !"), o que atrapalha tokenização/tradução e quebra testes.
    joined = re.sub(r"\s+([,.;:!?])", r"\1", joined)
    return joined


def rect_area(r: fitz.Rect) -> float:
    w = max(0.0, float(r.x1 - r.x0))
    h = max(0.0, float(r.y1 - r.y0))
    return w * h


def rect_iou(a: fitz.Rect, b: fitz.Rect) -> float:
    x0 = max(a.x0, b.x0)
    y0 = max(a.y0, b.y0)
    x1 = min(a.x1, b.x1)
    y1 = min(a.y1, b.y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0

    inter = (x1 - x0) * (y1 - y0)
    union = rect_area(a) + rect_area(b) - inter
    if union <= 0:
        return 0.0
    return float(inter / union)


def sort_blocks_reading_order(blocks: List[Tuple[float, float, float, float, str]]) -> List[Tuple[float, float, float, float, str]]:
    """Ordena blocos aproximadamente por leitura (top->down, left->right)."""
    return sorted(blocks, key=lambda b: (round(b[1], 1), round(b[0], 1)))


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def file_signature(path: Path, sample_bytes: int = 65536) -> str:
    """Gera uma assinatura estável do arquivo para evitar *resume* entre PDFs diferentes.

    Estratégia:
    - Hash de: tamanho + mtime + primeiros N bytes + últimos N bytes.
    - Muito mais rápido que hashear o arquivo inteiro e suficientemente robusto para nosso uso.

    Retorna: sha256 hexdigest.
    """
    path = Path(path)
    st = path.stat()
    h = hashlib.sha256()
    h.update(f"{st.st_size}|{int(st.st_mtime)}".encode("utf-8", errors="ignore"))

    with path.open("rb") as f:
        first = f.read(int(sample_bytes))
        h.update(first)
        if st.st_size > int(sample_bytes):
            try:
                f.seek(max(0, st.st_size - int(sample_bytes)))
                last = f.read(int(sample_bytes))
                h.update(last)
            except Exception:
                # em alguns FS/streams, seek pode falhar; sem problema
                pass

    return h.hexdigest()
