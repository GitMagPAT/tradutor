from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests


def _ok(msg: str) -> None:
    print(f"[OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _which(cmd: str) -> str:
    p = shutil.which(cmd)
    return p or ""


def _check_tesseract() -> Tuple[bool, str]:
    # 1) env override
    tcmd = os.getenv("TESSERACT_CMD") or ""
    if tcmd and Path(tcmd).exists():
        return True, tcmd

    # 2) PATH
    p = _which("tesseract")
    if p:
        return True, p

    # 3) default install path (Windows)
    default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if Path(default).exists():
        return True, default

    return False, ""


def _run(cmd: List[str]) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return int(p.returncode), out.strip()
    except Exception as e:
        return 1, repr(e)


def run_doctor(cfg: Dict[str, Any], workdir: Path) -> int:
    print("\n========================")
    print("Doctor (verificação do ambiente)")
    print("========================\n")

    problems = 0

    # Python
    _ok(f"Python: {sys.version.split()[0]} ({platform.python_implementation()})")
    _ok(f"OS: {platform.system()} {platform.release()} ({platform.version()})")

    # Imports essenciais
    try:
        import fitz  # noqa
        _ok("PyMuPDF (fitz) importou OK")
    except Exception as e:
        _fail(f"PyMuPDF (fitz) falhou ao importar: {e}")
        problems += 1

    try:
        from PIL import Image  # noqa
        _ok("Pillow (PIL) importou OK")
    except Exception as e:
        _fail(f"Pillow falhou ao importar: {e}")
        problems += 1

    try:
        import pytesseract  # noqa
        _ok("pytesseract importou OK")
    except Exception as e:
        _fail(f"pytesseract falhou ao importar: {e}")
        problems += 1

    # Tesseract
    ok, tpath = _check_tesseract()
    if not ok:
        _fail("Tesseract NÃO encontrado. Instale e/ou configure TESSERACT_CMD.")
        problems += 1
    else:
        _ok(f"Tesseract encontrado: {tpath}")
        code, out = _run([tpath, "--version"]) if tpath.lower().endswith(".exe") else _run(["tesseract", "--version"])
        if code == 0 and out:
            _ok("tesseract --version OK")
        else:
            _warn("Não consegui executar 'tesseract --version' (mas o executável existe).")

    # Tessdata
    tessdata_prefix = os.getenv("TESSDATA_PREFIX") or ""
    if tessdata_prefix:
        p = Path(tessdata_prefix)
        if p.exists():
            _ok(f"TESSDATA_PREFIX: {p}")
        else:
            _fail(f"TESSDATA_PREFIX aponta para pasta inexistente: {p}")
            problems += 1
    else:
        _warn("TESSDATA_PREFIX não setado. (OK se o Tesseract achar o tessdata sozinho.)")

    # Verifica idiomas esperados
    ocr_lang = str((cfg.get("ocr") or {}).get("lang", "eng"))
    langs = [s.strip() for s in ocr_lang.split("+") if s.strip()]
    # OSD é útil; se estiver no tessdata do projeto, melhor
    langs = ["osd", *langs]
    if tessdata_prefix and Path(tessdata_prefix).exists():
        missing = []
        for lg in langs:
            if not (Path(tessdata_prefix) / f"{lg}.traineddata").exists():
                missing.append(lg)
        if missing:
            _warn(f"Faltam traineddata em {tessdata_prefix}: {', '.join(missing)}")
        else:
            _ok("traineddata esperados estão presentes")

    # Tradutor
    tr_cfg = cfg.get("translator", {}) or {}
    provider = str(tr_cfg.get("provider", "libretranslate")).strip().lower()

    if provider == "libretranslate":
        url = str(tr_cfg.get("libretranslate_url", "http://127.0.0.1:5000")).rstrip("/")
        api_key = str(tr_cfg.get("libretranslate_api_key") or os.getenv("LIBRETRANSLATE_API_KEY") or "").strip()
        source_lang = str(cfg.get("source_lang", "en")).strip() or "en"
        target_lang = str(cfg.get("target_lang", "pt")).strip() or "pt"
        health = url + "/health"
        try:
            r = requests.get(health, timeout=5)
            if r.status_code == 200:
                _ok(f"LibreTranslate respondeu em {health}")
            else:
                _warn(f"LibreTranslate respondeu, mas status={r.status_code} em {health}")
        except Exception as e:
            _warn(f"LibreTranslate não respondeu em {health}: {e}")
            _warn("Se você for usar LibreTranslate local, suba via Docker ou script em scripts/.")

        # Languages + teste de tradução (evita rodar o pipeline inteiro e descobrir no final)
        try:
            r = requests.get(url + "/languages", timeout=5)
            if r.status_code == 200:
                langs = r.json() or []
                codes = [x.get("code") for x in langs if isinstance(x, dict) and x.get("code")]
                if target_lang in codes:
                    _ok(f"LibreTranslate: idioma alvo '{target_lang}' aparece em /languages")
                else:
                    _warn(
                        f"LibreTranslate: idioma alvo '{target_lang}' NÃO aparece em /languages. "
                        "Pode faltar modelo/idioma no container."
                    )
            else:
                _warn(f"LibreTranslate /languages respondeu status={r.status_code}")
        except Exception as e:
            _warn(f"Não consegui consultar LibreTranslate /languages: {e}")

        try:
            payload = {
                "q": "This is a translation test.",
                "source": source_lang,
                "target": target_lang,
                "format": "text",
            }
            if api_key:
                payload["api_key"] = api_key
            r = requests.post(url + "/translate", json=payload, timeout=30)
            if r.status_code == 200:
                data = r.json() or {}
                out = str(data.get("translatedText") or "").strip()
                if out and out.lower() != payload["q"].lower():
                    _ok("LibreTranslate: teste de tradução OK")
                else:
                    _warn(
                        "LibreTranslate respondeu ao teste, mas a tradução parece vazia/igual ao original. "
                        "Isso costuma indicar modelos ausentes ou problema no servidor."
                    )
            else:
                _warn(f"LibreTranslate /translate respondeu status={r.status_code}: {r.text[:200]}")
        except Exception as e:
            _warn(f"Falha ao testar /translate no LibreTranslate: {e}")
    elif provider == "mymemory":
        _ok("Translator: MyMemory (atenção: limites de uso)")
    else:
        _ok(f"Translator: {provider}")

    # Pastas
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "_doctor_write_test.tmp").write_text("ok", encoding="utf-8")
        (workdir / "_doctor_write_test.tmp").unlink(missing_ok=True)
        _ok(f"Workdir OK: {workdir}")
    except Exception as e:
        _fail(f"Não consigo escrever no workdir ({workdir}): {e}")
        problems += 1

    print("\n========================")
    if problems == 0:
        _ok("Doctor finalizado: ambiente parece OK ✅")
        print("========================\n")
        return 0

    _fail(f"Doctor finalizado: encontrei {problems} problema(s) ❌")
    print("========================\n")
    return 1
