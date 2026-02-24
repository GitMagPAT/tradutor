from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import fitz  # PyMuPDF

# Detectores de vazamento de placeholders.
_ZXQ_RE = re.compile(r"ZXQ", re.I)
_ZXQ_FUZZY_RE = re.compile(r"Z[XQ]{0,4}(?:ENT|GLOS)\w*\d", re.I)


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def run_qa_scan(workdir: Path, out_pdf: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Rodar scanner de qualidade no fim do pipeline."""

    pipeline_cfg = (cfg.get("pipeline") or {}) if isinstance(cfg, dict) else {}
    enabled = bool(pipeline_cfg.get("qa_scan", True))
    if not enabled:
        return {"enabled": False}

    fail_on_zxq = bool(pipeline_cfg.get("qa_fail_on_zxq", True))
    unchanged_ratio_warn = _safe_float(pipeline_cfg.get("qa_unchanged_ratio_warn", 0.85), 0.85)
    unchanged_min_chars = _safe_int(pipeline_cfg.get("qa_unchanged_min_chars", 300), 300)
    qa_report_path = str(pipeline_cfg.get("qa_report_path") or "").strip()
    qa_fail_score_threshold = _safe_int(pipeline_cfg.get("qa_fail_score_threshold", 0), 0)

    logs_dir = Path(workdir) / "logs"
    log_files = sorted(logs_dir.glob("page_*.json"))

    pages: List[Dict[str, Any]] = []
    for lf in log_files:
        try:
            pages.append(json.loads(lf.read_text(encoding="utf-8")))
        except Exception:
            continue

    summary: Dict[str, Any] = {
        "pages_total": len(pages),
        "pages_ok": 0,
        "pages_with_errors": 0,
        "warnings_total": 0,
        "errors_total": 0,
        "high_unchanged_pages": 0,
        "zxq_leak_pages": 0,
    }
    issues: List[Dict[str, Any]] = []
    page_scores: Dict[int, Dict[str, Any]] = {}

    # 1) Audita logs por página
    for p in pages:
        page_idx = _safe_int(p.get("page"), -1)
        status = str(p.get("status") or "")
        warnings = p.get("warnings") or []
        errors = p.get("errors") or []
        changed = _safe_int(p.get("changed_blocks"), 0)
        unchanged = _safe_int(p.get("unchanged_blocks"), 0)
        total = changed + unchanged
        native_chars = _safe_int(p.get("native_char_count"), 0)

        sc = page_scores.setdefault(page_idx, {"score": 0, "reasons": []})
        if status not in ("ok", "ok_no_text"):
            sc["score"] += 20
            sc["reasons"].append("status_not_ok")

        if status == "ok":
            summary["pages_ok"] += 1
        if errors:
            summary["pages_with_errors"] += 1
            sc["score"] += min(40, 10 * len(errors))
            sc["reasons"].append("page_errors")
        if warnings:
            sc["score"] += min(20, 5 * len(warnings))
            sc["reasons"].append("page_warnings")

        summary["warnings_total"] += len(warnings)
        summary["errors_total"] += len(errors)

        if total > 0:
            ratio = unchanged / float(total)
            if native_chars >= unchanged_min_chars and ratio >= unchanged_ratio_warn:
                summary["high_unchanged_pages"] += 1
                issues.append(
                    {
                        "type": "high_unchanged_ratio",
                        "severity": "warning",
                        "page": page_idx,
                        "unchanged_ratio": round(ratio, 4),
                        "native_chars": native_chars,
                        "changed_blocks": changed,
                        "unchanged_blocks": unchanged,
                    }
                )
                sc["score"] += 10
                sc["reasons"].append("high_unchanged_ratio")

    # 2) Scanner de vazamento "ZXQ" no PDF final
    zxq_pages: List[Dict[str, Any]] = []
    try:
        doc = fitz.open(str(out_pdf))
        for i in range(len(doc)):
            txt = doc[i].get_text("text") or ""
            if _ZXQ_RE.search(txt) or _ZXQ_FUZZY_RE.search(txt):
                m = re.search(r"(?i)(.{0,30}(?:ZXQ|Z[XQ]{0,4}(?:ENT|GLOS)\w*\d).{0,30})", txt)
                snippet = (m.group(1) if m else "ZXQ").replace("\n", " ").strip()
                zxq_pages.append({"page": i, "snippet": snippet[:120]})
                sc = page_scores.setdefault(i, {"score": 0, "reasons": []})
                sc["score"] = 100
                if "zxq_leak" not in sc["reasons"]:
                    sc["reasons"].append("zxq_leak")
        doc.close()
    except Exception as e:
        issues.append({"type": "qa_pdf_scan_failed", "severity": "warning", "error": str(e)})

    if zxq_pages:
        summary["zxq_leak_pages"] = len(zxq_pages)
        issues.append(
            {
                "type": "zxq_leak",
                "severity": "error" if fail_on_zxq else "warning",
                "count": len(zxq_pages),
                "pages": zxq_pages[:50],
                "note": "Lista truncada para 50 páginas (se houver mais).",
            }
        )

    top_risky_pages = sorted(
        [
            {
                "page": page_idx,
                "score": min(100, int(v.get("score", 0))),
                "reasons": sorted(set(v.get("reasons") or [])),
            }
            for page_idx, v in page_scores.items()
            if page_idx >= 0
        ],
        key=lambda x: x["score"],
        reverse=True,
    )

    report: Dict[str, Any] = {
        "enabled": True,
        "summary": {**summary, "top_risky_pages": top_risky_pages[:10]},
        "issues": issues,
        "out_pdf": str(out_pdf),
        "workdir": str(workdir),
    }

    # 3) Persistir artefatos de QA
    try:
        default_json = Path(workdir) / "qa_report.json"
        default_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if qa_report_path:
            qp = Path(qa_report_path)
            qp.parent.mkdir(parents=True, exist_ok=True)
            qp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        lines: List[str] = []
        lines.append("PDF Translate pt-BR — QA Report")
        lines.append(f"Output: {out_pdf}")
        lines.append(f"Pages: {summary['pages_total']}")
        lines.append(f"OK: {summary['pages_ok']} | Pages with errors: {summary['pages_with_errors']}")
        lines.append(f"Warnings: {summary['warnings_total']} | Errors: {summary['errors_total']}")
        lines.append(f"High-unchanged pages: {summary['high_unchanged_pages']}")
        lines.append(f"ZXQ leak pages: {summary['zxq_leak_pages']}")
        if top_risky_pages:
            lines.append(f"Top risky pages: {top_risky_pages[:5]}")
        lines.append("")

        if issues:
            lines.append("Issues:")
            for it in issues[:80]:
                if it.get("type") == "zxq_leak":
                    lines.append(f"- [ERROR] zxq_leak: {it.get('count')} página(s). Ex.: {it.get('pages', [])[:3]}")
                elif it.get("type") == "high_unchanged_ratio":
                    lines.append(
                        f"- [WARN] high_unchanged_ratio p={it.get('page')} ratio={it.get('unchanged_ratio')} chars={it.get('native_chars')}"
                    )
                else:
                    lines.append(f"- [{it.get('severity', 'warn').upper()}] {it}")
            if len(issues) > 80:
                lines.append(f"... ({len(issues) - 80} itens omitidos)")
        else:
            lines.append("Nenhum problema encontrado (pelas heurísticas atuais).")

        (Path(workdir) / "qa_report.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass

    # 4) Gates de falha
    max_score = max((int(x.get("score", 0)) for x in top_risky_pages), default=0)
    if qa_fail_score_threshold > 0 and max_score >= qa_fail_score_threshold:
        raise RuntimeError(
            f"[QA] Score de risco máximo={max_score} >= threshold={qa_fail_score_threshold}. "
            f"Veja: {Path(workdir) / 'qa_report.json'}"
        )

    if zxq_pages and fail_on_zxq:
        raise RuntimeError(
            f"[QA] Vazamento de 'ZXQ' detectado em {len(zxq_pages)} página(s). "
            f"Veja: {Path(workdir) / 'qa_report.json'}"
        )

    return report
