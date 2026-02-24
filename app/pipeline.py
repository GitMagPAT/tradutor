from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
from PIL import Image
from tqdm import tqdm

from .cache import TranslationCache
from . import __version__ as PROJECT_VERSION
from .detect import PageType, detect_page_features
from .extract import extract_native_text_blocks
from .models import TextBlock
from .ocr import configure_tesseract, mask_out_rects_pt, ocr_image_to_blocks, preprocess_image
from .render import (
    apply_translations_raster,
    create_translated_page_pdf_overlay,
    create_translated_page_pdf_overlay_original,
    pil_to_bytes,
    render_page_to_image,
)
from .translate import build_translator, load_glossary, load_do_not_translate, translate_many_with_cache, lang_for_translator
from .utils import rect_iou, stable_hash, file_signature
from .llm_assist import build_llm_assist_client, validate_post_edit_candidate


class TranslatorFatalError(RuntimeError):
    """Erro de tradução considerado *fatal* para o pipeline.

    Ex.: o servidor do tradutor caiu / está travado / não responde repetidamente.
    Nesse caso, é melhor abortar cedo e deixar o usuário corrigir, ao invés
    de gerar um PDF inteiro "não traduzido".
    """





def _resolve_native_cover_mode(has_images: bool, configured_mode: str, auto_mode: bool) -> str:
    """Resolve modo de cobertura de texto nativo (baixo risco).

    - Em páginas sem imagens, usar `block` reduz vazamento visual do texto original.
    - Em páginas com imagens/diagramas, manter modo configurado (`line`/`word`) evita
      apagar traços finos das figuras.
    """
    mode = str(configured_mode or "line").strip().lower()
    if mode not in ("block", "line", "word"):
        mode = "line"
    if auto_mode and (not has_images):
        return "block"
    return mode


def _effective_max_cover_area_ratio_native(has_images: bool, configured_ratio: float, auto_unlimited_no_images: bool) -> float:
    """Define limite de cobertura nativa por página (baixo risco).

    Problema observado: em páginas sem imagens, um bloco nativo grande pode ter
    cobertura desativada pelo limite de área e causar mistura EN+PT no resultado.
    Nesses casos, é mais seguro permitir cobertura ampla.
    """
    try:
        ratio = float(configured_ratio)
    except Exception:
        ratio = 0.50
    if auto_unlimited_no_images and (not has_images):
        return 1.0
    return ratio
def _filter_ocr_duplicates(
    ocr_blocks: List[TextBlock],
    native_blocks: List[TextBlock],
    iou_threshold: float,
) -> List[TextBlock]:
    """Remove blocos OCR que são praticamente o mesmo texto do bloco nativo (evita duplicar)."""
    if not native_blocks:
        return ocr_blocks

    out: List[TextBlock] = []
    for ob in ocr_blocks:
        dup = False
        for nb in native_blocks:
            if rect_iou(ob.rect, nb.rect) >= float(iou_threshold):
                dup = True
                break
        if not dup:
            out.append(ob)
    return out


def _merge_page_pdfs(page_pdfs: List[Path], out_pdf: Path) -> None:
    out_doc = fitz.open()
    tmp_out = out_pdf.with_suffix(out_pdf.suffix + ".tmp")
    try:
        for p in page_pdfs:
            if not Path(p).exists():
                continue
            src = fitz.open(str(p))
            out_doc.insert_pdf(src)
            src.close()

        if tmp_out.exists():
            tmp_out.unlink()

        # Otimiza tamanho do arquivo final (baixo risco)
        out_doc.save(
            str(tmp_out),
            garbage=4,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
        )
    finally:
        out_doc.close()

    try:
        tmp_out.replace(out_pdf)
    except PermissionError as exc:
        raise RuntimeError(
            f"Não foi possível sobrescrever o PDF de saída '{out_pdf}'. "
            "Feche o arquivo no visualizador (Adobe/Edge/etc.) e tente novamente."
        ) from exc


def _preserve_pdf_features(src_pdf: Path, out_pdf: Path) -> None:
    # Best-effort: copia TOC/bookmarks, metadados, page labels e links do PDF original.
    # Importante:
    # - Não altera o conteúdo visual das páginas (apenas estrutura / interatividade).
    # - Falhas aqui NÃO devem quebrar o pipeline.

    try:
        src = fitz.open(str(src_pdf))
        dst = fitz.open(str(out_pdf))

        # Metadados
        try:
            md = src.metadata or {}
            if md:
                dst.set_metadata(md)
        except Exception:
            pass

        # TOC / Bookmarks
        try:
            toc = src.get_toc(simple=False)
            if toc:
                dst.set_toc(toc)
        except Exception:
            pass

        # Page labels (romanos, etc.)
        try:
            if hasattr(src, "get_page_labels") and hasattr(dst, "set_page_labels"):
                labels = src.get_page_labels()  # type: ignore[attr-defined]
                if labels:
                    dst.set_page_labels(labels)  # type: ignore[attr-defined]
        except Exception:
            pass

        # Links (internos/externos)
        try:
            n = min(src.page_count, dst.page_count)
            for i in range(n):
                sp = src.load_page(i)
                dp = dst.load_page(i)
                links = sp.get_links() or []
                for lk in links:
                    try:
                        d = dict(lk)
                        d.pop("xref", None)
                        d.pop("id", None)
                        if "from" in d and not isinstance(d["from"], fitz.Rect):
                            d["from"] = fitz.Rect(d["from"])
                        if "to" in d and d.get("to") is not None and not isinstance(d["to"], fitz.Point):
                            try:
                                d["to"] = fitz.Point(d["to"])
                            except Exception:
                                pass
                        if d.get("kind") == fitz.LINK_GOTO and d.get("page") is not None:
                            if int(d["page"]) < 0 or int(d["page"]) >= n:
                                continue
                        dp.insert_link(d)
                    except Exception:
                        continue
        except Exception:
            pass

        # Salva em arquivo temporário e troca (evita problemas de save incremental)
        tmp = out_pdf.with_suffix(out_pdf.suffix + ".tmp")
        dst.save(str(tmp), deflate=True)
        dst.close()
        src.close()
        tmp.replace(out_pdf)

    except Exception:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass
        return


def _write_fallback_page(
    out_page_pdf: Path,
    page_rect: fitz.Rect,
    bg_img: Image.Image,
    image_format: str,
    jpg_quality: int,
) -> None:
    """Cria uma página de fallback só com a imagem de fundo (sem tradução)."""
    out_page_pdf.parent.mkdir(parents=True, exist_ok=True)
    img_bytes = pil_to_bytes(bg_img, image_format=image_format, jpg_quality=jpg_quality)
    d = fitz.open()
    p = d.new_page(width=page_rect.width, height=page_rect.height)
    p.insert_image(page_rect, stream=img_bytes)
    d.save(
        str(out_page_pdf),
        garbage=4,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
    )
    d.close()


def run_pipeline(
    pdf_path: Path,
    out_pdf: Path,
    cfg: Dict[str, Any],
    workdir: Path,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
) -> None:
    """Pipeline principal (página a página)."""
    pdf_path = Path(pdf_path)
    out_pdf = Path(out_pdf)
    workdir = Path(workdir)

    # Segurança do *resume*:
    # Evita misturar páginas geradas de PDFs diferentes (ou configs diferentes) dentro do mesmo workdir.
    pdf_sig = file_signature(pdf_path)
    cfg_sig = stable_hash(json.dumps(cfg, sort_keys=True, ensure_ascii=False))

    manifest_path = workdir / "manifest.json"
    if manifest_path.exists():
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
        prev_pdf_sig = prev.get("pdf_signature")
        prev_cfg_sig = prev.get("cfg_signature")

        if (prev_pdf_sig and prev_pdf_sig != pdf_sig) or (prev_cfg_sig and prev_cfg_sig != cfg_sig):
            print("[!] Workdir contém resultados de outro PDF/config. Limpando out_pages/ e logs/ para evitar mistura...")
            # Mantém cache.sqlite (pode reaproveitar traduções idênticas), mas limpa artefatos por página.
            import shutil

            shutil.rmtree(workdir / "out_pages", ignore_errors=True)
            shutil.rmtree(workdir / "logs", ignore_errors=True)

    # garante estrutura
    work_out_pages = workdir / "out_pages"
    work_logs = workdir / "logs"
    work_out_pages.mkdir(parents=True, exist_ok=True)
    work_logs.mkdir(parents=True, exist_ok=True)

    # Atualiza manifest (best effort)
    try:
        manifest = {
            "pdf_path": str(pdf_path.resolve()),
            "pdf_signature": pdf_sig,
            "cfg_signature": cfg_sig,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    pipeline_cfg = cfg.get("pipeline", {}) or {}
    min_text_chars_native = int(pipeline_cfg.get("min_text_chars_native", 40))
    iou_threshold = float(pipeline_cfg.get("iou_threshold", 0.20))
    resume = bool(pipeline_cfg.get("resume", True))
    log_blocks = bool(pipeline_cfg.get("log_blocks", False))

    # Robustez:
    # - Resume só pula páginas que deram "OK" (evita ficar preso com páginas de fallback).
    # - Thresholds para falhas de tradução (melhor abortar do que gerar PDF inteiro sem tradução).
    resume_success_only = bool(pipeline_cfg.get("resume_success_only", True))
    abort_on_translate_errors = bool(pipeline_cfg.get("abort_on_translate_errors", True))
    max_translate_failures_total = int(pipeline_cfg.get("max_translate_failures_total", 10))
    max_translate_failures_consecutive = int(pipeline_cfg.get("max_translate_failures_consecutive", 3))
    fail_if_zero_translated_pages = bool(pipeline_cfg.get("fail_if_zero_translated_pages", True))

    # Cache tuning (baixo risco, melhora performance em PDFs grandes)
    cache_mem = int(pipeline_cfg.get("cache_memory_max_entries", 20_000))
    cache_commit = int(pipeline_cfg.get("cache_commit_every", 50))
    cache = TranslationCache(workdir / "cache.sqlite", memory_max_entries=cache_mem, commit_every=cache_commit)

    translator = build_translator(cfg)
    llm_assist = build_llm_assist_client(cfg)

    llm_cfg = cfg.get("llm_assist", {}) or {}
    llm_post_edit_enabled = bool(llm_assist) and bool(llm_cfg.get("post_edit_enabled", False))
    llm_post_edit_min_chars = int(llm_cfg.get("post_edit_min_chars", 30))
    llm_post_edit_max_chars = int(llm_cfg.get("post_edit_max_chars", 700))
    llm_post_edit_max_blocks_per_page = int(llm_cfg.get("post_edit_max_blocks_per_page", 30))

    source_lang = str(cfg.get("source_lang", "en"))
    target_lang = str(cfg.get("target_lang", "pt"))

    dpi = int(cfg.get("dpi", 200))

    ocr_cfg = cfg.get("ocr", {}) or {}
    ocr_lang = str(ocr_cfg.get("lang", "eng"))
    ocr_oem = int(ocr_cfg.get("oem", 3))
    ocr_psm = int(ocr_cfg.get("psm", 6))
    # Permite otimizar OCR separadamente para páginas escaneadas vs texto em figuras:
    # - scanned: texto corrido -> psm 6 costuma funcionar bem
    # - images:  texto esparso em diagramas -> psm 11 costuma funcionar melhor
    ocr_psm_scanned = int(ocr_cfg.get("psm_scanned", ocr_psm))
    ocr_psm_images = int(ocr_cfg.get("psm_images", ocr_psm))
    ocr_min_conf = int(ocr_cfg.get("min_confidence", 40))
    translate_images = bool(ocr_cfg.get("translate_images", True))
    ocr_preprocess = bool(ocr_cfg.get("preprocess", False))
    mask_pad_pt = float(ocr_cfg.get("mask_pad_pt", 1.0))
    # Timeout do Tesseract (segundos). 0 = sem timeout.
    ocr_timeout_sec = float(ocr_cfg.get("timeout_sec", 180))
    # Agrupamento do OCR:
    # - scanned: "paragraph" (melhor para texto corrido)
    # - images:  "line" (melhor para texto em figuras/diagramas; evita caixas gigantes)
    ocr_group_mode_scanned = str(ocr_cfg.get("group_mode_scanned", "paragraph") or "paragraph")
    ocr_group_mode_images = str(ocr_cfg.get("group_mode_images", "line") or "line")
    ocr_filter_noise = bool(ocr_cfg.get("filter_noise", True))
    # Evita bboxes de OCR absurdamente grandes quando fazemos OCR só para traduzir texto em imagens.
    ocr_images_max_block_area_ratio = float(ocr_cfg.get("translate_images_max_block_area_ratio", 0.15))

    translator_cfg = cfg.get("translator", {}) or {}
    # MyMemory tem limites chatos; LibreTranslate self-host aguenta mais.
    max_chars_per_request = int(
        translator_cfg.get(
            "max_chars_per_request",
            450 if translator.provider_name == "mymemory" else 1200,
        )
    )
    batch_mode = translator_cfg.get("batch_mode", "auto")

    # Como proteger entidades antes da tradução.
    # - default: protege números de forma ampla (mais "seguro", porém insere mais placeholders)
    # - relaxed: protege apenas padrões técnicos (unidades, códigos etc.), tende a ficar mais natural
    # - none/off: desliga proteção (use somente se necessário)
    entity_mode = str(translator_cfg.get("entity_mode", "default")).strip().lower()
    if entity_mode not in ("default", "relaxed", "none", "off"):
        entity_mode = "default"

    # Glossário opcional
    glossary_path = translator_cfg.get("glossary_path", "glossary.yaml")
    glossary = {}
    try:
        if glossary_path:
            glossary = load_glossary(Path.cwd() / str(glossary_path))
    except Exception:
        glossary = {}

    do_not_translate_path = translator_cfg.get("do_not_translate_path", "do_not_translate.yaml")
    do_not_translate_terms: List[str] = []
    try:
        if do_not_translate_path:
            do_not_translate_terms = load_do_not_translate(Path.cwd() / str(do_not_translate_path))
    except Exception:
        do_not_translate_terms = []

    # Para o cache: se houver glossário/listas, inclui hash curto para não misturar resultados
    glossary_hash = ""
    if glossary:
        glossary_hash = stable_hash(json.dumps(glossary, sort_keys=True, ensure_ascii=False))[:8]
    dnt_hash = ""
    if do_not_translate_terms:
        dnt_hash = stable_hash(json.dumps(sorted(do_not_translate_terms), ensure_ascii=False))[:8]
    cache_version = str(cfg.get("pipeline", {}).get("cache_version", PROJECT_VERSION)).strip()
    provider_id = "{}|g:{}|dnt:{}|em:{}|cv:{}|pv:{}".format(translator.provider_name, (glossary_hash or ""), (dnt_hash or ""), entity_mode, cache_version, PROJECT_VERSION)

    render_cfg = cfg.get("render", {}) or {}
    render_mode = str(render_cfg.get("mode", "pdf_overlay")).strip().lower()
    image_format = str(render_cfg.get("image_format", "jpg"))
    jpg_quality = int(render_cfg.get("jpg_quality", 85))
    font_path = Path(render_cfg.get("font_path", "assets/fonts/DejaVuSans.ttf"))
    font_min_size = float(render_cfg.get("font_min_size", 6))
    font_max_size = float(render_cfg.get("font_max_size", 14))
    pad_pt = float(render_cfg.get("pad_pt", 1.0))
    cover_pad_pt = float(render_cfg.get("cover_pad_pt", 0.5))
    cover_blend_to_white = float(render_cfg.get("cover_blend_to_white", 0.90))
    # Ajustes específicos para OCR em figuras/diagramas (evita 'caixas brancas' enormes):
    cover_pad_pt_ocr = float(render_cfg.get("cover_pad_pt_ocr", 0.25))
    cover_opacity_native = float(render_cfg.get("cover_opacity_native", 1.0))
    cover_opacity_ocr = float(render_cfg.get("cover_opacity_ocr", 0.85))
    max_cover_area_ratio_native = float(render_cfg.get("max_cover_area_ratio_native", 0.50))
    max_cover_area_ratio_ocr = float(render_cfg.get("max_cover_area_ratio_ocr", 0.15))
    auto_unlimited_native_cover_on_text_pages = bool(render_cfg.get("auto_unlimited_native_cover_on_text_pages", True))

    # Tesseract via env (o PowerShell normalmente seta)
    import os

    configure_tesseract(
        tesseract_cmd=os.getenv("TESSERACT_CMD") or None,
        tessdata_prefix=os.getenv("TESSDATA_PREFIX") or None,
    )

    # ------------------------------------------------------------
    # Preflight do tradutor (baixo risco, evita rodar horas e sair sem tradução)
    # ------------------------------------------------------------
    preflight_enabled = bool(translator_cfg.get("preflight_test", True))
    if preflight_enabled and translator.provider_name != "dummy":
        test_text = str(translator_cfg.get("preflight_text", "This is a translation test."))
        test_text = (test_text or "").strip() or "This is a translation test."
        try:
            src_api = lang_for_translator(translator.provider_name, source_lang)
            tgt_api = lang_for_translator(translator.provider_name, target_lang)
            test_out = translator.translate(test_text, source_lang=src_api, target_lang=tgt_api)
            if not isinstance(test_out, str) or not test_out.strip():
                raise RuntimeError("Resultado vazio no teste de tradução")
            if test_out.strip().lower() == test_text.strip().lower():
                raise RuntimeError("O tradutor devolveu o mesmo texto (sem traduzir)")
        except Exception as e:
            raise RuntimeError(
                "Falha no teste de tradução. O tradutor não parece pronto/funcional "
                f"para {source_lang}->{target_lang} (provider={translator.provider_name}). "
                "Verifique se o LibreTranslate está rodando e com modelos instalados. "
                f"Detalhe: {e}"
            ) from e

    doc = fitz.open(str(pdf_path))
    try:
        total_pages = doc.page_count

        sp = int(start_page) if start_page is not None else 0
        ep = int(end_page) if end_page is not None else total_pages
        sp = max(0, min(sp, total_pages))
        ep = max(0, min(ep, total_pages))
        if ep <= sp:
            raise ValueError(f"Intervalo de páginas inválido: start={sp}, end={ep}, total={total_pages}")

        page_indices = list(range(sp, ep))

        produced_pages: List[Path] = []

        # Estatísticas (para validação final e mensagens mais úteis)
        translated_pages = 0  # páginas que tiveram pelo menos 1 bloco traduzido
        translate_fail_total = 0
        translate_fail_consecutive = 0
        ok_pages = 0
        fallback_pages = 0

        for page_number in tqdm(page_indices, desc="Traduzindo páginas", unit="pág"):
            out_page_pdf = work_out_pages / f"page_{page_number:04d}.pdf"
            out_page_log = work_logs / f"page_{page_number:04d}.json"

            if resume and out_page_pdf.exists():
                if resume_success_only and out_page_log.exists():
                    try:
                        prev = json.loads(out_page_log.read_text(encoding="utf-8"))
                        st = str(prev.get("status") or "")
                        if st.startswith("ok"):
                            ok_pages += 1
                            if int(prev.get("changed_blocks") or 0) > 0:
                                translated_pages += 1
                            produced_pages.append(out_page_pdf)
                            continue
                    except Exception:
                        # Se o log estiver corrompido, reprocessa a página.
                        pass
                elif not resume_success_only:
                    produced_pages.append(out_page_pdf)
                    continue

            t_page0 = time.time()
            timings: Dict[str, float] = {}

            page = doc.load_page(page_number)
            page_rect = page.rect

            # Renderiza página para OCR + fundo
            t_render0 = time.time()
            try:
                bg_img = render_page_to_image(page, dpi=dpi)
            except Exception:
                # fallback: página branca com tamanho aproximado
                bg_img = Image.new("RGB", (int(page_rect.width), int(page_rect.height)), (255, 255, 255))
            timings["render_bg_sec"] = round(time.time() - t_render0, 3)

            scale_x = bg_img.width / page_rect.width if page_rect.width else 1.0
            scale_y = bg_img.height / page_rect.height if page_rect.height else 1.0

            info: Dict[str, Any] = {
                "page": page_number,
                "status": "pending",
                "page_type": None,
                "has_images": None,
                "native_char_count": None,
                "native_blocks": 0,
                "ocr_blocks": 0,
                "translated_blocks": 0,
                "provider": provider_id,
                "warnings": [],
                "errors": [],
                "timings": timings,
            }

            native_blocks: List[TextBlock] = []
            ocr_blocks: List[TextBlock] = []
            translated_blocks: List[TextBlock] = []

            try:
                t_detect0 = time.time()
                page_type, has_native_text, has_images, native_char_count = detect_page_features(
                    page, min_text_chars_native=min_text_chars_native
                )
                info["page_type"] = str(page_type.value)
                info["has_images"] = bool(has_images)
                info["native_char_count"] = int(native_char_count)
                timings["detect_sec"] = round(time.time() - t_detect0, 3)

                # 1) Extração nativa
                if page_type in (PageType.NATIVE, PageType.HYBRID):
                    t_ext0 = time.time()
                    native_cover_mode_cfg = cfg.get("render", {}).get("native_cover_mode", "line")
                    auto_native_cover_mode = bool(cfg.get("render", {}).get("auto_native_cover_mode", True))
                    native_cover_mode = _resolve_native_cover_mode(
                        has_images=bool(has_images),
                        configured_mode=str(native_cover_mode_cfg),
                        auto_mode=auto_native_cover_mode,
                    )
                    native_blocks = extract_native_text_blocks(
                        page,
                        page_number=page_number,
                        has_images=has_images,
                        include_cover_rects=(str(native_cover_mode).lower() != "block"),
                        cover_mode=str(native_cover_mode),
                        split_sparse_blocks=bool(cfg.get("render", {}).get("split_sparse_native_blocks", True)),
                        sparse_min_area_ratio=float(cfg.get("render", {}).get("sparse_native_min_area_ratio", 0.04)),
                        sparse_max_chars=int(cfg.get("render", {}).get("sparse_native_max_chars", 120)),
                        sparse_max_words=int(cfg.get("render", {}).get("sparse_native_max_words", 30)),
                        cluster_gap_factor=float(cfg.get("render", {}).get("sparse_cluster_gap_factor", 2.5)),
                    )
                    timings["extract_native_sec"] = round(time.time() - t_ext0, 3)

                # 2) OCR
                # IMPORTANTE: o pytesseract pode travar por muito tempo em algumas páginas.
                # Por isso, usamos um timeout (configurável) para evitar "travamentos" infinitos.
                t_ocr0 = time.time()
                if page_type == PageType.SCANNED:
                    img_for_ocr = preprocess_image(bg_img) if ocr_preprocess else bg_img
                    try:
                        ocr_blocks = ocr_image_to_blocks(
                            img_for_ocr,
                            page_number=page_number,
                            scale_x=scale_x,
                            scale_y=scale_y,
                            lang=ocr_lang,
                            oem=ocr_oem,
                            psm=ocr_psm_scanned,
                            min_confidence=ocr_min_conf,
                            timeout_sec=ocr_timeout_sec,
                            group_mode=ocr_group_mode_scanned,
                            filter_noise=ocr_filter_noise,
                        )
                    except Exception as e_ocr:
                        # Página scan sem OCR => provavelmente não conseguiremos traduzir.
                        info["warnings"].append("ocr_failed")
                        info["errors"].append(f"ocr_failed: {repr(e_ocr)}")
                        ocr_blocks = []
                else:
                    # Página tem texto nativo; ainda assim queremos pegar texto dentro de figuras/imagens.
                    # Otimização importante: só vale a pena se a página tiver imagens.
                    if translate_images and has_images:
                        masked = mask_out_rects_pt(
                            bg_img,
                            [b.rect for b in native_blocks],
                            scale_x,
                            scale_y,
                            pad_pt=mask_pad_pt,
                        )
                        img_for_ocr = preprocess_image(masked) if ocr_preprocess else masked
                        try:
                            ocr_blocks = ocr_image_to_blocks(
                                img_for_ocr,
                                page_number=page_number,
                                scale_x=scale_x,
                                scale_y=scale_y,
                                lang=ocr_lang,
                                oem=ocr_oem,
                                psm=ocr_psm_images,
                                min_confidence=ocr_min_conf,
                                timeout_sec=ocr_timeout_sec,
                                group_mode=ocr_group_mode_images,
                                filter_noise=ocr_filter_noise,
                                # Importante p/ diagramas: usar sub-caixas (palavras)
                                # para "apagar" o texto original sem criar caixas brancas enormes.
                                return_word_boxes=bool(cfg.get("ocr", {}).get("word_boxes_for_images", True)),
                                cluster_sparse_lines=bool(cfg.get("ocr", {}).get("cluster_sparse_lines", True)),
                                cluster_gap_factor=float(cfg.get("ocr", {}).get("cluster_gap_factor", 2.5)),
                            )
                            # Proteção: em figuras/diagramas o OCR pode retornar um bloco com bbox gigante,
                            # o que causa "caixas brancas" enormes no overlay. Filtramos por área relativa.
                            if ocr_blocks:
                                page_area_pt2 = float(page_rect.width * page_rect.height) if page_rect else 0.0
                                if page_area_pt2 > 0.0:
                                    kept = []
                                    dropped = 0
                                    for ob in ocr_blocks:
                                        ob_area = float(ob.rect.width * ob.rect.height)
                                        if (ocr_images_max_block_area_ratio > 0.0) and (ob_area / page_area_pt2 > ocr_images_max_block_area_ratio):
                                            dropped += 1
                                            continue
                                        kept.append(ob)
                                    if dropped:
                                        info["warnings"].append("ocr_big_blocks_skipped")
                                        info["ocr_blocks_dropped_big_bbox"] = int(dropped)
                                    ocr_blocks = kept
                        except Exception as e_ocr:
                            # Não é fatal para páginas com texto nativo — seguimos só com nativo.
                            info["warnings"].append("ocr_on_images_failed")
                            info["errors"].append(f"ocr_on_images_failed: {repr(e_ocr)}")
                            ocr_blocks = []
                    elif translate_images and not has_images:
                        # evita OCR desnecessário em páginas só-texto
                        info["warnings"].append("ocr_skipped_no_images")

                # Fallback seguro: se detectamos texto nativo mas não conseguimos extrair blocos,
                # tentamos OCR da página inteira (pode salvar PDFs estranhos/híbridos).
                if page_type in (PageType.NATIVE, PageType.HYBRID) and not native_blocks and not ocr_blocks:
                    try:
                        img_for_ocr = preprocess_image(bg_img) if ocr_preprocess else bg_img
                        ocr_blocks = ocr_image_to_blocks(
                            img_for_ocr,
                            page_number=page_number,
                            scale_x=scale_x,
                            scale_y=scale_y,
                            lang=ocr_lang,
                            oem=ocr_oem,
                            psm=ocr_psm_scanned,
                            min_confidence=ocr_min_conf,
                            timeout_sec=ocr_timeout_sec,
                            group_mode=ocr_group_mode_scanned,
                            filter_noise=ocr_filter_noise,
                        )
                        info["warnings"].append("ocr_fallback_full_page")
                    except Exception as e_ocr2:
                        info["warnings"].append("ocr_fallback_failed")
                        info["errors"].append(f"ocr_fallback_failed: {repr(e_ocr2)}")
                        # não quebra a página; só segue

                timings["ocr_sec"] = round(time.time() - t_ocr0, 3)

                # 3) Filtra duplicados OCR vs nativo
                ocr_blocks = _filter_ocr_duplicates(ocr_blocks, native_blocks, iou_threshold=iou_threshold)

                info["native_blocks"] = len(native_blocks)
                info["ocr_blocks"] = len(ocr_blocks)

                # 4) Une blocos
                blocks_all = native_blocks + ocr_blocks

                # 5) Tradução (em lote, quando possível)
                if blocks_all:
                    t_tr0 = time.time()
                    try:
                        translated_texts = translate_many_with_cache(
                            cache=cache,
                            translator=translator,
                            texts=[b.text for b in blocks_all],
                            source_lang=source_lang,
                            target_lang=target_lang,
                            max_chars_per_request=max_chars_per_request,
                            provider_id=provider_id,
                            glossary=glossary,
                            entity_mode=entity_mode,
                            do_not_translate_terms=do_not_translate_terms,
                            batch_mode=batch_mode,
                        )
                        # Se chegou aqui, a tradução respondeu.
                        translate_fail_consecutive = 0
                    except Exception as e_tr:
                        translate_fail_total += 1
                        translate_fail_consecutive += 1

                        info["warnings"].append("translate_failed")
                        info["errors"].append(f"translate_failed: {repr(e_tr)}")

                        # Se o tradutor caiu, é melhor abortar cedo.
                        if abort_on_translate_errors and (
                            translate_fail_consecutive >= max_translate_failures_consecutive
                            or translate_fail_total >= max_translate_failures_total
                        ):
                            raise TranslatorFatalError(
                                "Falhas repetidas no tradutor (provável queda/travamento do LibreTranslate). "
                                f"Falhas consecutivas={translate_fail_consecutive}, total={translate_fail_total}. "
                                "Verifique o LibreTranslate (docker logs) e rode novamente com resume."
                            ) from e_tr

                        # Erro de tradução nesta página => cai para fallback e segue (páginas seguintes).
                        raise
                    timings["translate_sec"] = round(time.time() - t_tr0, 3)

                    # Métrica simples: quantos blocos realmente mudaram?
                    # Ajuda a detectar casos em que o tradutor está respondendo mas não está traduzindo.
                    def _norm(s: str) -> str:
                        return " ".join((s or "").split()).strip().lower()

                    def _count_changed_unchanged(src_blocks: List[TextBlock], tr_texts: List[str]) -> tuple[int, int]:
                        ch = 0
                        un = 0
                        for b0, tr_txt in zip(src_blocks, tr_texts):
                            if _norm(tr_txt) == _norm(b0.text):
                                un += 1
                            else:
                                ch += 1
                        return ch, un

                    changed0, unchanged0 = _count_changed_unchanged(blocks_all, translated_texts)

                    # v0.2.6 — Retradução controlada de blocos "unchanged" (baixo risco)
                    # Motivo: alguns blocos (especialmente sumários/índices com muitos pontos e números)
                    # podem voltar idênticos no 1º passe. Fazemos 1 retry em um subconjunto elegível,
                    # com um modo de proteção de entidades mais "relaxado".
                    pipe_cfg = cfg.get("pipeline") or {}
                    do_retry_unchanged = bool(pipe_cfg.get("retranslate_unchanged", True))
                    retry_min_chars = int(pipe_cfg.get("retranslate_unchanged_min_chars", 12))
                    retry_max_chars = int(pipe_cfg.get("retranslate_unchanged_max_chars", 800))
                    retry_max_blocks = int(pipe_cfg.get("retranslate_unchanged_max_blocks_per_page", 250))
                    retry_entity_mode = str(pipe_cfg.get("retranslate_unchanged_entity_mode", "relaxed") or "relaxed")

                    retry_candidates: List[int] = []

                    if do_retry_unchanged and unchanged0 > 0:
                        import re

                        # Gatilhos simples (heurística): reduz custo em PDFs grandes.
                        en_triggers = {
                            " the ", " and ", " of ", " to ", " for ", " with ", " in ", " on ", " from ", " by ",
                        }

                        def _should_retry_block(src_text: str) -> bool:
                            t = (src_text or "").strip()
                            if len(t) < retry_min_chars or len(t) > retry_max_chars:
                                return False
                            if not re.search(r"[A-Za-z]", t):
                                return False
                            # Evita retraduzir tokens "de código" muito curtos
                            if len(t.split()) == 1 and re.fullmatch(r"[A-Za-z0-9_\-\.]+", t) and len(t) <= 10:
                                return False
                            # Se tem gatilho de inglês, vale o retry
                            tl = f" {t.lower()} "
                            if any(trg in tl for trg in en_triggers):
                                return True
                            # Caso alternativo: texto com 3+ palavras alfabéticas
                            words = re.findall(r"[A-Za-z]{2,}", t)
                            return len(words) >= 3

                        for i0, (b0, tr_txt) in enumerate(zip(blocks_all, translated_texts)):
                            if len(retry_candidates) >= retry_max_blocks:
                                break
                            if _norm(tr_txt) != _norm(b0.text):
                                continue
                            if _should_retry_block(b0.text):
                                retry_candidates.append(i0)

                    retry_changed = 0
                    if retry_candidates:
                        t_retry0 = time.time()
                        try:
                            retry_texts = [blocks_all[i].text for i in retry_candidates]
                            retry_provider_id = f"{provider_id}|retry|{retry_entity_mode}"
                            retry_translations = translate_many_with_cache(
                                cache=cache,
                                translator=translator,
                                texts=retry_texts,
                                source_lang=source_lang,
                                target_lang=target_lang,
                                max_chars_per_request=max_chars_per_request,
                                provider_id=retry_provider_id,
                                glossary=glossary,
                                batch_mode=batch_mode,
                                entity_mode=retry_entity_mode,
                                do_not_translate_terms=do_not_translate_terms,
                            )

                            for idx0, new_tr in zip(retry_candidates, retry_translations):
                                if _norm(new_tr) != _norm(blocks_all[idx0].text):
                                    if _norm(translated_texts[idx0]) == _norm(blocks_all[idx0].text):
                                        retry_changed += 1
                                    translated_texts[idx0] = new_tr
                        except Exception as e_retry:
                            # Não é fatal: mantemos as traduções do 1º passe.
                            info["warnings"].append("retry_unchanged_failed")
                            info["errors"].append(f"retry_unchanged_failed: {repr(e_retry)}")
                        timings["translate_retry_sec"] = round(time.time() - t_retry0, 3)
                    else:
                        timings["translate_retry_sec"] = 0.0

                    # Pós-edição opcional com LLM (ex.: Ministral) para fluência/consistência.
                    if llm_post_edit_enabled and translated_texts:
                        pe_candidates = []
                        for i0, tr_txt in enumerate(translated_texts):
                            txt_len = len((tr_txt or "").strip())
                            if txt_len < llm_post_edit_min_chars or txt_len > llm_post_edit_max_chars:
                                continue
                            if len(pe_candidates) >= llm_post_edit_max_blocks_per_page:
                                break
                            pe_candidates.append(i0)

                        pe_changed = 0
                        pe_errors = 0
                        for idx0 in pe_candidates:
                            try:
                                new_txt = llm_assist.post_edit_block(blocks_all[idx0].text, translated_texts[idx0]) if llm_assist else translated_texts[idx0]
                            except Exception:
                                pe_errors += 1
                                continue
                            if (new_txt or "").strip() and new_txt.strip() != (translated_texts[idx0] or "").strip():
                                ok_edit, reasons = validate_post_edit_candidate(translated_texts[idx0], new_txt)
                                if ok_edit:
                                    translated_texts[idx0] = new_txt.strip()
                                    pe_changed += 1
                                else:
                                    info["warnings"].append("llm_post_edit_rejected_guard")
                                    info.setdefault("llm_post_edit_rejected_reasons", []).append({
                                        "block_id": blocks_all[idx0].block_id,
                                        "reasons": reasons,
                                    })

                        info["llm_post_edit_candidates"] = len(pe_candidates)
                        info["llm_post_edit_changed"] = int(pe_changed)
                        if pe_errors:
                            info["warnings"].append("llm_post_edit_partial_fail")
                            info["llm_post_edit_errors"] = int(pe_errors)

                    changed, unchanged = _count_changed_unchanged(blocks_all, translated_texts)

                    info["changed_blocks"] = changed
                    info["unchanged_blocks"] = unchanged
                    info["retry_unchanged_candidates"] = len(retry_candidates)
                    info["retry_unchanged_changed"] = retry_changed
                    info["retry_unchanged_entity_mode"] = retry_entity_mode

                    translated_blocks = []
                    for b, ttxt in zip(blocks_all, translated_texts):
                        translated_blocks.append(
                            TextBlock(
                                rect=b.rect,
                                text=ttxt,
                                source=b.source,
                                page_number=b.page_number,
                                block_id=b.block_id,
                                confidence=b.confidence,
                                cover_rects=b.cover_rects,
                            )
                        )
                else:
                    timings["translate_sec"] = 0.0
                    translated_blocks = []
                    info["warnings"].append("no_text_blocks_detected")

                    info["changed_blocks"] = 0
                    info["unchanged_blocks"] = 0

                info["translated_blocks"] = len(translated_blocks)

                if log_blocks:
                    info["blocks"] = [
                        {
                            "id": b.block_id,
                            "src": b.source,
                            "conf": b.confidence,
                            "rect": [float(b.rect.x0), float(b.rect.y0), float(b.rect.x1), float(b.rect.y1)],
                            "text": b.short(200),
                        }
                        for b in blocks_all
                    ]

                # 6) Render output page
                t_rend0 = time.time()
                eff_max_cover_area_ratio_native = _effective_max_cover_area_ratio_native(
                    has_images=bool(has_images),
                    configured_ratio=float(max_cover_area_ratio_native),
                    auto_unlimited_no_images=auto_unlimited_native_cover_on_text_pages,
                )
                if render_mode == "pdf_overlay":
                    create_translated_page_pdf_overlay(
                        page_rect=page_rect,
                        bg_img=bg_img,
                        translated_blocks=translated_blocks,
                        out_pdf_path=out_page_pdf,
                        dpi=dpi,
                        image_format=image_format,
                        jpg_quality=jpg_quality,
                        font_path=(Path.cwd() / font_path) if not font_path.is_absolute() else font_path,
                        font_min_size=font_min_size,
                        font_max_size=font_max_size,
                        pad_pt=pad_pt,
                        cover_pad_pt=cover_pad_pt,
                        cover_pad_pt_ocr=cover_pad_pt_ocr,
                        cover_blend_to_white=cover_blend_to_white,
                        cover_opacity_native=cover_opacity_native,
                        cover_opacity_ocr=cover_opacity_ocr,
                        max_cover_area_ratio_native=eff_max_cover_area_ratio_native,
                        max_cover_area_ratio_ocr=max_cover_area_ratio_ocr,
                    )
                elif render_mode == "pdf_overlay_original":
                    create_translated_page_pdf_overlay_original(
                        src_doc=doc,
                        src_page_number=page_number,
                        page_rect=page_rect,
                        bg_img=bg_img,
                        translated_blocks=translated_blocks,
                        out_pdf_path=out_page_pdf,
                        dpi=dpi,
                        image_format=image_format,
                        jpg_quality=jpg_quality,
                        font_path=(Path.cwd() / font_path) if not font_path.is_absolute() else font_path,
                        font_min_size=font_min_size,
                        font_max_size=font_max_size,
                        pad_pt=pad_pt,
                        cover_pad_pt=cover_pad_pt,
                        cover_pad_pt_ocr=cover_pad_pt_ocr,
                        cover_blend_to_white=cover_blend_to_white,
                        cover_opacity_native=cover_opacity_native,
                        cover_opacity_ocr=cover_opacity_ocr,
                        max_cover_area_ratio_native=eff_max_cover_area_ratio_native,
                        max_cover_area_ratio_ocr=max_cover_area_ratio_ocr,
                    )
                elif render_mode == "raster":
                    img_out = apply_translations_raster(
                        bg_img=bg_img,
                        translated_blocks=translated_blocks,
                        scale_x=scale_x,
                        scale_y=scale_y,
                        font_path=(Path.cwd() / font_path) if not font_path.is_absolute() else font_path,
                        font_size_px=18,
                    )
                    img_bytes = pil_to_bytes(img_out, image_format=image_format, jpg_quality=jpg_quality)
                    d = fitz.open()
                    p = d.new_page(width=page_rect.width, height=page_rect.height)
                    p.insert_image(page_rect, stream=img_bytes)
                    d.save(
                        str(out_page_pdf),
                        garbage=4,
                        deflate=True,
                        deflate_images=True,
                        deflate_fonts=True,
                    )
                    d.close()
                else:
                    raise ValueError(f"Modo de render inválido: {render_mode}")

                timings["render_out_sec"] = round(time.time() - t_rend0, 3)

                # Se chegou aqui, a página foi gerada com sucesso.
                # Conta "página traduzida" apenas se algum bloco realmente mudou.
                if int(info.get("changed_blocks") or 0) > 0:
                    translated_pages += 1

                info["status"] = "ok" if blocks_all else "ok_no_text"
                ok_pages += 1

            except Exception as e:
                # Erros de tradução fatais: interrompe o pipeline.
                if isinstance(e, TranslatorFatalError):
                    info["status"] = "fatal_translate_error"
                    info["errors"].append(f"fatal_translate_error: {repr(e)}")
                    info["elapsed_sec"] = round(time.time() - t_page0, 2)
                    out_page_log.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
                    raise

                info["status"] = "fallback"
                fallback_pages += 1
                info["errors"].append(repr(e))

                # Mostra no console sem quebrar a barra de progresso
                try:
                    tqdm.write(f"[WARN] Página {page_number}: erro no processamento ({e}). Gerando fallback.")
                except Exception:
                    pass

                # fallback: tenta gerar uma página visualmente idêntica à original.
                try:
                    if render_mode == "pdf_overlay_original":
                        # Mantém o PDF original como fundo (bem menor que rasterizar tudo)
                        create_translated_page_pdf_overlay_original(
                            src_doc=doc,
                            src_page_number=page_number,
                            page_rect=page_rect,
                            bg_img=bg_img,
                            translated_blocks=[],
                            out_pdf_path=out_page_pdf,
                            dpi=dpi,
                            image_format=image_format,
                            jpg_quality=jpg_quality,
                            font_path=(Path.cwd() / font_path) if not font_path.is_absolute() else font_path,
                            font_min_size=font_min_size,
                            font_max_size=font_max_size,
                            pad_pt=pad_pt,
                            cover_pad_pt=cover_pad_pt,
                            cover_blend_to_white=cover_blend_to_white,
                        )
                    else:
                        # Fallback simples: rasteriza a página
                        _write_fallback_page(
                            out_page_pdf=out_page_pdf,
                            page_rect=page_rect,
                            bg_img=bg_img,
                            image_format=image_format,
                            jpg_quality=jpg_quality,
                        )
                except Exception as e2:
                    info["errors"].append(f"fallback_failed: {repr(e2)}")

            # Garantia: nunca deixa o merge quebrar por falta de arquivo
            if not out_page_pdf.exists():
                try:
                    _write_fallback_page(
                        out_page_pdf=out_page_pdf,
                        page_rect=page_rect,
                        bg_img=bg_img,
                        image_format=image_format,
                        jpg_quality=jpg_quality,
                    )
                except Exception as e3:
                    info["errors"].append(f"final_fallback_failed: {repr(e3)}")

            info["elapsed_sec"] = round(time.time() - t_page0, 2)
            out_page_log.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

            produced_pages.append(out_page_pdf)

        # ------------------------------------------------------------
        # Resumo + validação final
        # ------------------------------------------------------------
        total_proc = len(page_indices)
        try:
            tqdm.write(
                f"\n[INFO] Resumo: páginas OK={ok_pages}, fallback={fallback_pages}, "
                f"com tradução={translated_pages}/{total_proc}."
            )
        except Exception:
            pass

        # Se NENHUMA página teve algum bloco realmente modificado, é muito provável
        # que o tradutor não esteja funcionando, o idioma está errado, ou o PDF não
        # contém texto detectável. Nesse caso, é melhor falhar do que gerar um PDF
        # 'aparentemente ok' mas sem tradução.
        if fail_if_zero_translated_pages and total_proc > 0 and translated_pages == 0:
            raise RuntimeError(
                "Nenhuma página teve blocos traduzidos (0 páginas com texto modificado). "
                "O PDF de saída provavelmente ficaria igual ao original. "
                "Verifique: (1) LibreTranslate rodando e com idiomas carregados; "
                "(2) se o PDF tem texto copiável ou se o OCR está funcionando; "
                "(3) veja work/logs/page_XXXX.json para detalhes."
            )

        # Merge final
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        _merge_page_pdfs(produced_pages, out_pdf)


        # Preserva estrutura do documento (TOC/bookmarks, links, metadados, page labels)
        try:
            _preserve_pdf_features(src_pdf=pdf_path, out_pdf=out_pdf)
        except Exception:
            pass

        # v0.2.6: QA scanner (placeholders ZXQ + páginas suspeitas)
        # - Não altera o PDF; apenas gera work/qa_report.json e imprime um resumo.
        try:
            from .qa import run_qa_scan

            run_qa_scan(workdir=workdir, out_pdf=out_pdf, cfg=cfg)
        except RuntimeError:
            # Falha "controlada" de QA (ex.: vazamento de placeholders) deve interromper o pipeline.
            raise
        except Exception as e:
            # QA é best-effort: não quebra o fluxo por falha interna do scanner.
            try:
                tqdm.write(f"[QA] Falha ao rodar scanner: {repr(e)}")
            except Exception:
                pass

    finally:
        try:
            doc.close()
        except Exception:
            pass
        try:
            cache.close()
        except Exception:
            pass
