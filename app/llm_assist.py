from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

_ZXQ_TOKEN_RE = re.compile(r"ZXQ(?:ENT|GLOS)[A-Z0-9]*?X\d{4}ZXQ", re.I)
_NUM_UNIT_RE = re.compile(
    r"\b[+-]?\d+(?:[\.,]\d+)?\s?(?:kg|g|mg|lb|t|m|cm|mm|km|mi|in|ft|V|kV|mV|A|mA|Hz|kHz|MHz|rpm|N\.?m|N·m|Pa|kPa|MPa|bar|psi|°C|°F|%)?\b"
)
_REF_RE = re.compile(r"\b(?:fig\.?|figure|tabela|table)\s*\d+(?:[-–]\d+)?\b", re.I)


def _norm_tokens(rx: re.Pattern[str], text: str) -> List[str]:
    return sorted([m.group(0).strip().lower() for m in rx.finditer(text or "") if m.group(0).strip()])


def validate_post_edit_candidate(before: str, after: str) -> Tuple[bool, List[str]]:
    """Valida se pós-edição preservou tokens críticos (baixo risco).

    Retorna `(ok, reasons)`.
    """
    b = before or ""
    a = after or ""
    reasons: List[str] = []

    if _norm_tokens(_ZXQ_TOKEN_RE, b) != _norm_tokens(_ZXQ_TOKEN_RE, a):
        reasons.append("zxq_tokens_changed")

    if _norm_tokens(_NUM_UNIT_RE, b) != _norm_tokens(_NUM_UNIT_RE, a):
        reasons.append("numbers_units_changed")

    if _norm_tokens(_REF_RE, b) != _norm_tokens(_REF_RE, a):
        reasons.append("references_changed")

    return (len(reasons) == 0), reasons


@dataclass
class LlmAssistClient:
    """Cliente OpenAI-compatible para pós-edição/QA opcional.

    Uso pensado para modelos tipo Ministral (3B/8B/14B) servidos via endpoint
    compatível com `/chat/completions`.
    """

    base_url: str
    model: str
    timeout_sec: float = 60.0
    api_key: Optional[str] = None
    temperature: float = 0.0
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _chat(self, messages: List[Dict[str, Any]], max_tokens: int = 800) -> str:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": float(self.temperature),
            "max_tokens": int(max_tokens),
        }
        r = self.session.post(url, headers=self._headers(), json=payload, timeout=float(self.timeout_sec))
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = (choices[0].get("message") or {}) if isinstance(choices[0], dict) else {}
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            out: List[str] = []
            for p in content:
                if isinstance(p, str):
                    out.append(p)
                elif isinstance(p, dict) and p.get("type") == "text":
                    out.append(str(p.get("text") or ""))
            return "".join(out).strip()
        return ""

    def post_edit_block(self, src: str, dst: str) -> str:
        src = (src or "").strip()
        dst = (dst or "").strip()
        if not dst:
            return dst

        system = (
            "Você é um revisor técnico PT-BR. "
            "Revise a tradução mantendo fidelidade ao original e preservando tokens especiais (ZXQ...ZXQ), "
            "números, unidades, sinais (+/-), referências (Fig./Tabela), siglas e comandos. "
            "Não explique nada, retorne apenas JSON: {\"text\":\"...\"}."
        )
        user = json.dumps({"source": src, "translation": dst}, ensure_ascii=False)
        raw = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=700,
        )

        try:
            data = json.loads(raw)
            txt = str((data or {}).get("text") or "").strip()
            return txt or dst
        except Exception:
            return dst

    def summarize_qa_report(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """Gera resumo textual curto de risco e ações, best-effort."""
        system = (
            "Você é QA lead técnico. Analise o JSON de QA e retorne JSON com campos: "
            "risk_summary (string curta), actions (lista curta), confidence (0-1)."
        )
        user = json.dumps(report, ensure_ascii=False)[:12000]
        raw = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=500,
        )
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        return {"risk_summary": "NÃO CONSTA", "actions": [], "confidence": 0.0}


def build_llm_assist_client(cfg: Dict[str, Any]) -> Optional[LlmAssistClient]:
    lcfg = (cfg.get("llm_assist") or {}) if isinstance(cfg, dict) else {}
    if not bool(lcfg.get("enabled", False)):
        return None

    base_url = str(lcfg.get("base_url") or "").strip()
    model = str(lcfg.get("model") or "").strip()
    if not base_url or not model:
        return None

    return LlmAssistClient(
        base_url=base_url,
        model=model,
        timeout_sec=float(lcfg.get("timeout_sec", 60.0)),
        api_key=(str(lcfg.get("api_key") or "").strip() or None),
        temperature=float(lcfg.get("temperature", 0.0)),
    )
