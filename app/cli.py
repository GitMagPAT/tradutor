from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .config import deep_update, load_config, load_dotenv_if_present
from .doctor import run_doctor
from .pipeline import run_pipeline
from .utils import file_signature


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf_translate_ptbr",
        description="Traduz PDF inteiro para Português (pt-BR/pt) página a página, com OCR quando necessário.",
    )

    p.add_argument("--doctor", action="store_true", help="Checa dependências e sai (não traduz nada).")

    p.add_argument("--pdf", type=Path, default=None, help="Caminho do PDF de entrada (em inglês).")
    p.add_argument("--out", type=Path, default=None, help="PDF final de saída (default: output/<nome>_ptbr.pdf).")

    p.add_argument("--config", type=Path, default=Path("config.yaml"), help="Arquivo YAML de config (default: config.yaml).")
    p.add_argument("--workdir", type=Path, default=Path("work"), help="Diretório de trabalho (cache, logs, páginas).")

    p.add_argument("--start-page", type=int, default=None, help="Página inicial (0-based). Default: 0.")
    p.add_argument("--end-page", type=int, default=None, help="Página final (exclusiva, 0-based). Default: total de páginas.")

    # Overrides comuns
    p.add_argument("--source-lang", default=None, help='Idioma de origem (ex: "en" ou "auto").')
    p.add_argument("--target-lang", default=None, help='Idioma destino (ex: "pt" ou "pt-BR" dependendo do provedor).')
    p.add_argument("--dpi", type=int, default=None, help="DPI de renderização para OCR e fundo (150-300).")

    # OCR
    p.add_argument("--ocr-lang", default=None, help='Idioma(s) do OCR (Tesseract), ex: "eng" ou "eng+por".')
    p.add_argument("--no-translate-images", action="store_true", help="Não faz OCR para texto dentro de figuras/imagens.")
    p.add_argument("--ocr-preprocess", action="store_true", help="Aplica pré-processamento antes do OCR (pode ajudar em scans ruins).")
    p.add_argument(
        "--ocr-timeout-sec",
        type=float,
        default=None,
        help="Timeout do OCR (Tesseract) por página, em segundos. Use para evitar travamentos em páginas difíceis.",
    )

    # Tradutor
    p.add_argument(
        "--translator",
        choices=["opusmt", "translategemma", "libretranslate", "mymemory", "dummy"],
        default=None,
        help="Provedor de tradução.",
    )
    p.add_argument("--libretranslate-url", default=None, help="URL do LibreTranslate (ex: http://127.0.0.1:5000).")
    p.add_argument("--libretranslate-api-key", default=None, help="API key (normalmente vazio em self-host).")
    p.add_argument("--translategemma-url", default=None, help="Base URL OpenAI-compatible para TranslateGemma (ex.: http://127.0.0.1:12434/engines/v1).")
    p.add_argument("--translategemma-model", default=None, help="Nome do modelo no endpoint (ex.: aistaging/translategemma-vllm:27B).")
    p.add_argument("--translategemma-timeout-sec", type=float, default=None, help="Timeout (segundos) para TranslateGemma (endpoint OpenAI-compatible).")
    p.add_argument("--mymemory-email", default=None, help="Email para MyMemory (aumenta limite diário).")

    # Render
    p.add_argument(
        "--render-mode",
        choices=["pdf_overlay", "pdf_overlay_original", "raster"],
        default=None,
        help="Modo de saída.",
    )
    p.add_argument("--image-format", choices=["jpg", "png"], default=None, help="Formato do fundo (jpg/png).")
    p.add_argument("--jpg-quality", type=int, default=None, help="Qualidade do JPG (60-90).")

    # Pipeline
    p.add_argument("--no-resume", action="store_true", help="Não reaproveitar páginas já geradas em work/out_pages.")
    p.add_argument("--no-keep-work", action="store_true", help="Apagar work/ ao final (não recomendado enquanto você testa).")
    p.add_argument("--qa-report", type=Path, default=None, help="Caminho customizado para salvar relatório QA JSON.")
    p.add_argument("--qa-threshold", type=int, default=None, help="Falha quando o score de risco máximo por página atingir este valor (0-100).")
    p.add_argument("--audit-mode", action="store_true", help="Ativa modo auditor (qa_scan=true e log_blocks=true).")

    # Tesseract
    p.add_argument("--tesseract-cmd", default=None, help="Caminho do tesseract.exe se não estiver no PATH.")
    p.add_argument("--tessdata-prefix", default=None, help="Pasta tessdata (TESSDATA_PREFIX).")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parents[1]
    load_dotenv_if_present(project_root)

    cfg = load_config(project_root / args.config)

    overrides: Dict[str, Any] = {}

    if args.source_lang:
        overrides["source_lang"] = args.source_lang
    if args.target_lang:
        overrides["target_lang"] = args.target_lang
    if args.dpi:
        overrides["dpi"] = int(args.dpi)

    if args.ocr_lang or args.no_translate_images or args.ocr_preprocess or args.ocr_timeout_sec is not None:
        ocr = dict((cfg.get("ocr") or {}))
        if args.ocr_lang:
            ocr["lang"] = args.ocr_lang
        if args.no_translate_images:
            ocr["translate_images"] = False
        if args.ocr_preprocess:
            ocr["preprocess"] = True
        if args.ocr_timeout_sec is not None:
            ocr["timeout_sec"] = float(args.ocr_timeout_sec)
        overrides["ocr"] = ocr

    if args.translator or args.libretranslate_url or args.libretranslate_api_key or args.translategemma_url or args.translategemma_model or args.translategemma_timeout_sec is not None or args.mymemory_email:
        tr = dict((cfg.get("translator") or {}))
        if args.translator:
            tr["provider"] = args.translator
        if args.libretranslate_url:
            tr["libretranslate_url"] = args.libretranslate_url
        if args.libretranslate_api_key:
            tr["libretranslate_api_key"] = args.libretranslate_api_key
        if args.translategemma_url:
            tr["translategemma_url"] = args.translategemma_url
        if args.translategemma_model:
            tr["translategemma_model"] = args.translategemma_model
        if args.translategemma_timeout_sec is not None:
            tr["translategemma_timeout_sec"] = float(args.translategemma_timeout_sec)
        if args.mymemory_email:
            tr["mymemory_email"] = args.mymemory_email
        overrides["translator"] = tr

    if args.render_mode or args.image_format or args.jpg_quality:
        rd = dict((cfg.get("render") or {}))
        if args.render_mode:
            rd["mode"] = args.render_mode
        if args.image_format:
            rd["image_format"] = args.image_format
        if args.jpg_quality:
            rd["jpg_quality"] = int(args.jpg_quality)
        overrides["render"] = rd

    if args.no_resume or args.no_keep_work or args.qa_report is not None or args.qa_threshold is not None or args.audit_mode:
        pl = dict((cfg.get("pipeline") or {}))
        if args.no_resume:
            pl["resume"] = False
        if args.no_keep_work:
            pl["keep_work"] = False
        if args.qa_report is not None:
            pl["qa_report_path"] = str(args.qa_report)
        if args.qa_threshold is not None:
            pl["qa_fail_score_threshold"] = int(args.qa_threshold)
        if args.audit_mode:
            pl["qa_scan"] = True
            pl["log_blocks"] = True
        overrides["pipeline"] = pl

    cfg = deep_update(cfg, overrides)

    # Tesseract env overrides
    if args.tesseract_cmd:
        os.environ["TESSERACT_CMD"] = args.tesseract_cmd
    if args.tessdata_prefix:
        os.environ["TESSDATA_PREFIX"] = args.tessdata_prefix

    workdir = project_root / args.workdir

    # Por padrão, isolamos o workdir por PDF (evita misturar páginas quando você roda PDFs diferentes com --resume).
    # Se você passar --workdir explicitamente, respeitamos o valor.
    if (not args.doctor) and args.pdf is not None and args.workdir == Path("work"):
        sig8 = file_signature(args.pdf)[:8]
        workdir = project_root / "work" / f"{args.pdf.stem}_{sig8}"

    if args.doctor:
        return int(run_doctor(cfg, workdir))

    # Validação (para modo tradução)
    if args.pdf is None:
        parser.error("--pdf é obrigatório (a menos que você use --doctor).")

    # Defaults de output
    out_pdf = args.out
    if out_pdf is None:
        out_pdf = project_root / "output" / f"{args.pdf.stem}_ptbr.pdf"

    run_pipeline(
        pdf_path=args.pdf,
        out_pdf=out_pdf,
        cfg=cfg,
        workdir=workdir,
        start_page=args.start_page,
        end_page=args.end_page,
    )

    # Limpeza opcional
    keep_work = bool((cfg.get("pipeline") or {}).get("keep_work", True))
    if not keep_work:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    return 0
