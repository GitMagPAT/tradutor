from __future__ import annotations

import io
from pathlib import Path
from typing import Iterable, List, Tuple

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont, ImageStat

from .models import TextBlock
from .utils import clamp


def render_page_to_image(page: fitz.Page, dpi: int = 200) -> Image.Image:
    """Renderiza uma página do PDF para PIL.Image em RGB."""
    zoom = float(dpi) / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    mode = "RGB"
    img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
    return img


def pil_to_bytes(img: Image.Image, image_format: str = "jpg", jpg_quality: int = 85) -> bytes:
    fmt = image_format.lower()
    if fmt in ("jpg", "jpeg"):
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=int(jpg_quality), optimize=True)
        return out.getvalue()
    if fmt == "png":
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    raise ValueError(f"Formato de imagem não suportado: {image_format}")


def _luminance(rgb: Tuple[int, int, int]) -> float:
    # Rec. 709 luma approximation
    r, g, b = rgb
    return 0.2126 * float(r) + 0.7152 * float(g) + 0.0722 * float(b)


def choose_text_color01(bg_rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    """Escolhe cor do texto (preto/branco) baseada no fundo."""
    lum = _luminance(bg_rgb)
    # threshold simples: acima disso, preto; abaixo, branco
    return (0.0, 0.0, 0.0) if lum >= 140.0 else (1.0, 1.0, 1.0)


def sample_background_rgb(
    bg_img: Image.Image,
    rect_pt: fitz.Rect,
    scale_x: float,
    scale_y: float,
    blend_to_white: float = 0.85,
) -> Tuple[int, int, int]:
    """Amostra cor média do fundo para tentar 'camuflar' a caixa de tradução.

    Melhorias (baixo risco):
    - Em fundos claros (página branca), ainda puxamos para branco para cobrir melhor o texto original.
    - Em fundos escuros (banners pretos, figuras), NÃO puxamos agressivamente para branco,
      senão destruímos o visual. Mantemos a cor mais próxima do original e trocamos a cor do texto para branco.
    """
    x0 = clamp(int(rect_pt.x0 * scale_x), 0, bg_img.width)
    y0 = clamp(int(rect_pt.y0 * scale_y), 0, bg_img.height)
    x1 = clamp(int(rect_pt.x1 * scale_x), 0, bg_img.width)
    y1 = clamp(int(rect_pt.y1 * scale_y), 0, bg_img.height)

    if x1 <= x0 or y1 <= y0:
        return (255, 255, 255)

    crop = bg_img.crop((x0, y0, x1, y1))
    stat = ImageStat.Stat(crop)
    if not stat.mean:
        return (255, 255, 255)
    r0, g0, b0 = [int(v) for v in stat.mean[:3]]

    lum0 = _luminance((r0, g0, b0))
    # Se já é praticamente branco, força branco (reduz manchas cinzas no fundo)
    if lum0 >= 245.0:
        return (255, 255, 255)

    target_alpha = max(0.0, min(1.0, float(blend_to_white)))

    # Blend adaptativo: só clareia bem quando o fundo já é claro.
    if lum0 <= 90.0:
        alpha = 0.0
    elif lum0 >= 200.0:
        alpha = target_alpha
    else:
        # interpolação 90..200
        t = (lum0 - 90.0) / (200.0 - 90.0)
        alpha = target_alpha * t

    r = int(r0 * (1 - alpha) + 255 * alpha)
    g = int(g0 * (1 - alpha) + 255 * alpha)
    b = int(b0 * (1 - alpha) + 255 * alpha)
    return (r, g, b)


def _rgb01(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    return (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)


def _fit_textbox(
    page: fitz.Page,
    rect: fitz.Rect,
    text: str,
    fontname: str,
    fontfile: str,
    max_size: float,
    min_size: float,
    color01: Tuple[float, float, float] = (0, 0, 0),
    align: int = 0,
) -> float:
    """Tenta inserir texto reduzindo o fontsize até caber.

    Retorna o fontsize usado.
    """
    # Garante font disponível (PyMuPDF instala automaticamente, mas aqui fazemos explícito)
    try:
        page.insert_font(fontname=fontname, fontfile=fontfile)
    except Exception:
        # Se falhar, ainda tentamos com fontname (pode funcionar com built-in)
        pass

    size = float(max_size)
    while size >= float(min_size):
        rc = page.insert_textbox(
            rect,
            text,
            fontname=fontname,
            fontsize=size,
            color=color01,
            align=align,
            overlay=True,
        )
        # Segundo docs / exemplos, rc < 0 => texto não coube
        if isinstance(rc, (int, float)) and rc >= 0:
            return size
        size -= 0.5

    # Última tentativa: escreve no min_size mesmo que corte
    page.insert_textbox(
        rect,
        text,
        fontname=fontname,
        fontsize=float(min_size),
        color=color01,
        align=align,
        overlay=True,
    )
    return float(min_size)


def create_translated_page_pdf_overlay(
    page_rect: fitz.Rect,
    bg_img: Image.Image,
    translated_blocks: List[TextBlock],
    out_pdf_path: Path,
    dpi: int,
    image_format: str,
    jpg_quality: int,
    font_path: Path,
    font_min_size: float = 6,
    font_max_size: float = 14,
    pad_pt: float = 1.0,
    cover_pad_pt: float = 0.5,
    cover_pad_pt_ocr: float = 0.25,
    cover_blend_to_white: float = 0.90,
    cover_opacity_native: float = 1.0,
    cover_opacity_ocr: float = 0.85,
    max_cover_area_ratio_native: float = 0.50,
    max_cover_area_ratio_ocr: float = 0.15,
) -> None:
    """Cria um PDF de 1 página: fundo rasterizado + texto traduzido vetorial sobreposto.

    Ajustes importantes:
    - O retângulo de *cobertura* (cover) pode ser levemente MAIOR que o bbox do texto
      para esconder antialias e vazamentos do texto original.
    - O retângulo de *texto* (textbox) pode ser levemente MENOR (pad_pt) para não encostar nas bordas.
    """
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open()
    page = doc.new_page(width=page_rect.width, height=page_rect.height)

    # Fundo
    bg_bytes = pil_to_bytes(bg_img, image_format=image_format, jpg_quality=jpg_quality)
    page.insert_image(page_rect, stream=bg_bytes)

    scale_x = bg_img.width / page_rect.width if page_rect.width else 1.0
    scale_y = bg_img.height / page_rect.height if page_rect.height else 1.0

    fontfile = str(font_path.resolve())
    fontname = "F0"

    cpad_native = float(cover_pad_pt or 0.0)
    cpad_ocr = float(cover_pad_pt_ocr or cover_pad_pt or 0.0)
    tpad = float(pad_pt or 0.0)

    page_area_pt2 = float(page_rect.width * page_rect.height) if page_rect else 0.0
    max_ratio_native = float(max_cover_area_ratio_native or 0.0)
    max_ratio_ocr = float(max_cover_area_ratio_ocr or 0.0)

    cov_op_native = float(cover_opacity_native)
    cov_op_ocr = float(cover_opacity_ocr)
    # clampa opacidades para [0, 1]
    cov_op_native = 1.0 if cov_op_native > 1.0 else (0.0 if cov_op_native < 0.0 else cov_op_native)
    cov_op_ocr = 1.0 if cov_op_ocr > 1.0 else (0.0 if cov_op_ocr < 0.0 else cov_op_ocr)

    #
    # Duas passagens (mesma lógica do overlay_original):
    # 1) desenha todas as coberturas
    # 2) escreve todos os textos
    #
    cover_ops: List[Tuple[fitz.Rect, Tuple[int, int, int], float]] = []
    text_ops: List[Tuple[fitz.Rect, str, Tuple[float, float, float]]] = []

    for b in translated_blocks:
        if not (b.text or "").strip():
            continue

        is_ocr = (b.source or "") == "ocr"
        r0 = fitz.Rect(b.rect)
        if r0.x1 <= r0.x0 or r0.y1 <= r0.y0:
            continue

        # Proteção contra blocos gigantes (principalmente OCR em figuras):
        if page_area_pt2 > 0.0 and max_ratio_ocr > 0.0 and is_ocr:
            area_ratio = float(r0.width * r0.height) / page_area_pt2
            if area_ratio > max_ratio_ocr:
                continue

        cpad = cpad_ocr if is_ocr else cpad_native
        cov_op = cov_op_ocr if is_ocr else cov_op_native
        do_cover = True
        if page_area_pt2 > 0.0 and max_ratio_native > 0.0 and (not is_ocr):
            area_ratio = float(r0.width * r0.height) / page_area_pt2
            if area_ratio > max_ratio_native:
                do_cover = False

        rc_big = fitz.Rect(r0)
        if cpad:
            rc_big.x0 -= cpad
            rc_big.y0 -= cpad
            rc_big.x1 += cpad
            rc_big.y1 += cpad
        rc_big = rc_big & page_rect
        if rc_big.x1 <= rc_big.x0 or rc_big.y1 <= rc_big.y0:
            continue

        # retângulo de texto (inset)
        rt = fitz.Rect(r0)
        if tpad:
            rt.x0 += tpad
            rt.y0 += tpad
            rt.x1 -= tpad
            rt.y1 -= tpad
        rt = rt & page_rect
        if rt.x1 <= rt.x0 or rt.y1 <= rt.y0:
            rt = rc_big

        # Cor do texto: amostra o fundo na área do textbox
        fill_for_text = sample_background_rgb(
            bg_img, rt, scale_x, scale_y, blend_to_white=float(cover_blend_to_white)
        )
        text_color01 = choose_text_color01(fill_for_text)

        # Cobertura:
        # - Se existirem retângulos granulares (cover_rects), use-os tanto para OCR
        #   (palavras/linhas) quanto para texto nativo (linhas/palavras), reduzindo
        #   "caixas brancas" que cobrem bordas de tabelas / traços de figuras.
        # - Caso contrário, usa a cobertura grande do bloco.
        if do_cover and cov_op > 0.0:
            if getattr(b, "cover_rects", None):
                sub_pad = float(cpad) if cpad else 0.0
                # Para nativo, use um padding menor para não apagar bordas/linhas finas.
                if (not is_ocr) and sub_pad:
                    sub_pad = min(sub_pad, 0.75)
                for cr0 in (b.cover_rects or []):
                    try:
                        cr = fitz.Rect(cr0) & page_rect
                    except Exception:
                        continue
                    if cr.x1 <= cr.x0 or cr.y1 <= cr.y0:
                        continue
                    if sub_pad:
                        cr.x0 -= sub_pad
                        cr.y0 -= sub_pad
                        cr.x1 += sub_pad
                        cr.y1 += sub_pad
                        cr = cr & page_rect
                    if cr.x1 <= cr.x0 or cr.y1 <= cr.y0:
                        continue
                    fill_rgb = sample_background_rgb(
                        bg_img, cr, scale_x, scale_y, blend_to_white=float(cover_blend_to_white)
                    )
                    cover_ops.append((cr, fill_rgb, cov_op))
            else:
                fill_rgb = sample_background_rgb(
                    bg_img, rc_big, scale_x, scale_y, blend_to_white=float(cover_blend_to_white)
                )
                cover_ops.append((rc_big, fill_rgb, cov_op))

        text_ops.append((rt, b.text, text_color01))

    # 1) Coberturas
    for rc, fill_rgb, cov_op in cover_ops:
        page.draw_rect(rc, color=None, fill=_rgb01(fill_rgb), width=0, fill_opacity=cov_op)

    # 2) Textos
    for rt, text, text_color01 in text_ops:
        _fit_textbox(
            page=page,
            rect=rt,
            text=text,
            fontname=fontname,
            fontfile=fontfile,
            max_size=font_max_size,
            min_size=font_min_size,
            color01=text_color01,
            align=0,
        )

    # Otimiza tamanho do PDF de página (baixo risco)
    doc.save(
        str(out_pdf_path),
        garbage=4,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
    )
    doc.close()


def create_translated_page_pdf_overlay_original(
    src_doc: fitz.Document,
    src_page_number: int,
    page_rect: fitz.Rect,
    bg_img: Image.Image,
    translated_blocks: List[TextBlock],
    out_pdf_path: Path,
    dpi: int,
    image_format: str,
    jpg_quality: int,
    font_path: Path,
    font_min_size: float = 6,
    font_max_size: float = 14,
    pad_pt: float = 1.0,
    cover_pad_pt: float = 0.5,
    cover_pad_pt_ocr: float = 0.25,
    cover_blend_to_white: float = 0.90,
    cover_opacity_native: float = 1.0,
    cover_opacity_ocr: float = 0.85,
    max_cover_area_ratio_native: float = 0.50,
    max_cover_area_ratio_ocr: float = 0.15,
) -> None:
    """Cria um PDF de 1 página: fundo = página original (vetorial) + texto traduzido sobreposto.

    Vantagem principal: mantém imagens/vetores do PDF original e geralmente gera um
    arquivo final MUITO menor (não rasteriza o fundo).

    Observação: ainda renderizamos a página para imagem (bg_img) para:
    - OCR
    - amostrar cores do fundo ao desenhar as caixas de cobertura.
    """
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open()
    page = doc.new_page(width=page_rect.width, height=page_rect.height)

    # Fundo: página original
    try:
        page.show_pdf_page(page_rect, src_doc, int(src_page_number))
    except Exception:
        # Fallback: se por algum motivo não conseguir "copiar" a página, usa raster.
        bg_bytes = pil_to_bytes(bg_img, image_format=image_format, jpg_quality=jpg_quality)
        page.insert_image(page_rect, stream=bg_bytes)

    scale_x = bg_img.width / page_rect.width if page_rect.width else 1.0
    scale_y = bg_img.height / page_rect.height if page_rect.height else 1.0

    fontfile = str(font_path.resolve())
    fontname = "F0"

    cpad_native = float(cover_pad_pt or 0.0)
    cpad_ocr = float(cover_pad_pt_ocr or cover_pad_pt or 0.0)
    tpad = float(pad_pt or 0.0)

    page_area_pt2 = float(page_rect.width * page_rect.height) if page_rect else 0.0
    max_ratio_native = float(max_cover_area_ratio_native or 0.0)
    max_ratio_ocr = float(max_cover_area_ratio_ocr or 0.0)

    cov_op_native = float(cover_opacity_native)
    cov_op_ocr = float(cover_opacity_ocr)
    cov_op_native = 1.0 if cov_op_native > 1.0 else (0.0 if cov_op_native < 0.0 else cov_op_native)
    cov_op_ocr = 1.0 if cov_op_ocr > 1.0 else (0.0 if cov_op_ocr < 0.0 else cov_op_ocr)

    # Render em 2 passagens:
    # 1) desenha *todas* as caixas de cobertura
    # 2) insere *todo* o texto (evita que uma caixa posterior cubra texto já desenhado)
    cover_ops: List[tuple[fitz.Rect, tuple[int, int, int], float]] = []  # (rect, fill_rgb, opacity)
    text_ops: List[tuple[fitz.Rect, str, tuple[float, float, float]]] = []  # (rt, text, color01)

    for b in translated_blocks:
        if not (b.text or "").strip():
            continue

        is_ocr = (b.source or "") == "ocr"
        r0 = fitz.Rect(b.rect)
        if r0.x1 <= r0.x0 or r0.y1 <= r0.y0:
            continue

        # filtro de segurança: ignora OCR com bbox gigante (evita apagar áreas enormes)
        if page_area_pt2 > 0.0 and max_ratio_ocr > 0.0 and is_ocr:
            area_ratio = float(r0.width * r0.height) / page_area_pt2
            if area_ratio > max_ratio_ocr:
                continue

        cpad = cpad_ocr if is_ocr else cpad_native
        cov_op = cov_op_ocr if is_ocr else cov_op_native
        do_cover = True
        if page_area_pt2 > 0.0 and max_ratio_native > 0.0 and (not is_ocr):
            area_ratio = float(r0.width * r0.height) / page_area_pt2
            if area_ratio > max_ratio_native:
                do_cover = False

        # retângulo de texto (inset)
        rt = fitz.Rect(r0)
        if tpad:
            rt.x0 += tpad
            rt.y0 += tpad
            rt.x1 -= tpad
            rt.y1 -= tpad
        rt = rt & page_rect

        # retângulo(s) de cobertura
        cover_rects: List[fitz.Rect] = []
        if getattr(b, "cover_rects", None):
            # Use sub-retângulos quando disponíveis (OCR palavras/linhas OU nativo linhas/palavras).
            # Para nativo, use padding menor para não apagar bordas/linhas finas.
            sub_pad = float(cpad) if cpad else 0.0
            if (not is_ocr) and sub_pad:
                sub_pad = min(sub_pad, 0.75)
            for cr0 in (b.cover_rects or []):
                try:
                    cr = fitz.Rect(cr0)
                except Exception:
                    continue
                if sub_pad:
                    cr.x0 -= sub_pad
                    cr.y0 -= sub_pad
                    cr.x1 += sub_pad
                    cr.y1 += sub_pad
                cr = cr & page_rect
                if cr.x1 > cr.x0 and cr.y1 > cr.y0:
                    cover_rects.append(cr)
        else:
            rc = fitz.Rect(r0)
            if cpad:
                rc.x0 -= cpad
                rc.y0 -= cpad
                rc.x1 += cpad
                rc.y1 += cpad
            rc = rc & page_rect
            if rc.x1 > rc.x0 and rc.y1 > rc.y0:
                cover_rects.append(rc)

        # Agenda covers
        if do_cover and cov_op > 0.0:
            for cr in cover_rects:
                # se for cover do OCR e o retângulo for grande demais, pula
                if is_ocr and page_area_pt2 > 0.0 and max_ratio_ocr > 0.0:
                    ar = float(cr.width * cr.height) / page_area_pt2
                    if ar > max_ratio_ocr:
                        continue
                fill_rgb = sample_background_rgb(
                    bg_img, cr, scale_x, scale_y, blend_to_white=float(cover_blend_to_white)
                )
                cover_ops.append((cr, fill_rgb, cov_op))

        # cor do texto: amostra o fundo do retângulo de texto (melhor do que usar rc)
        ref_rect = rt if (rt.x1 > rt.x0 and rt.y1 > rt.y0) else (cover_rects[0] if cover_rects else r0)
        fill_for_text = sample_background_rgb(
            bg_img, ref_rect, scale_x, scale_y, blend_to_white=float(cover_blend_to_white)
        )
        text_color01 = choose_text_color01(fill_for_text)

        if rt.x1 <= rt.x0 or rt.y1 <= rt.y0:
            rt = cover_rects[0] if cover_rects else r0

        text_ops.append((rt, b.text, text_color01))

    # 1) Covers
    for cr, fill_rgb, op in cover_ops:
        page.draw_rect(cr, color=None, fill=_rgb01(fill_rgb), width=0, fill_opacity=float(op))

    # 2) Text
    for rt, text, color01 in text_ops:
        _fit_textbox(
            page=page,
            rect=rt,
            text=text,
            fontname=fontname,
            fontfile=fontfile,
            max_size=font_max_size,
            min_size=font_min_size,
            color01=color01,
            align=0,
        )

    doc.save(
        str(out_pdf_path),
        garbage=4,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
    )
    doc.close()


def apply_translations_raster(
    bg_img: Image.Image,
    translated_blocks: List[TextBlock],
    scale_x: float,
    scale_y: float,
    font_path: Path,
    font_size_px: int = 18,
    pad_px: int = 2,
) -> Image.Image:
    """(Modo alternativo) Desenha traduções diretamente na imagem (resultado não selecionável)."""
    img = bg_img.copy()
    draw = ImageDraw.Draw(img)

    font = ImageFont.truetype(str(font_path), size=int(font_size_px))

    for b in translated_blocks:
        x0 = clamp(int(b.rect.x0 * scale_x), 0, img.width)
        y0 = clamp(int(b.rect.y0 * scale_y), 0, img.height)
        x1 = clamp(int(b.rect.x1 * scale_x), 0, img.width)
        y1 = clamp(int(b.rect.y1 * scale_y), 0, img.height)
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle([x0, y0, x1, y1], fill=(255, 255, 255))
        draw.text((x0 + pad_px, y0 + pad_px), b.text, fill=(0, 0, 0), font=font)

    return img
