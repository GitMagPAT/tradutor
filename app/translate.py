from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .cache import TranslationCache


# ----------------------------
# Proteção de entidades
# ----------------------------

# Evita que o tradutor mexa em: números, datas, unidades, URLs, emails, códigos etc.
# (heurístico; você pode ajustar conforme seu domínio)
#
# Nota v0.2.6:
# - Adicionamos proteção para *leader dots* de sumário/índice (ex.: ". . . . . . . 123").
#   Isso ajuda a manter os pontilhados e evita que o tradutor "coma" ou reordene os pontos.
# - Também introduzimos um modo "relaxed" (menos agressivo com números), usado apenas em
#   tentativas de retradução de blocos que ficaram 100% inalterados.

_LEADER_DOTS_PATTERN = r"(?:\.\s*){5,}"  # . . . . . (5+ repetições)

ENTITY_PATTERNS_DEFAULT = [
    r"https?://\S+",
    r"\b\w+@\w+\.[A-Za-z]{2,}\b",
    r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b",  # datas simples
    r"\b\d+(?:[\.,]\d+)?\b",  # números (agressivo)
    r"\b[A-Z]{2,}\d+\b",  # códigos tipo ABC123
    r"\b\d+(?:[\.,]\d+)?\s?(?:kg|g|mg|lb|t|m|cm|mm|km|mi|in|ft|V|kV|mV|A|mA|Hz|kHz|MHz|rpm|N\.?m|N·m|Pa|kPa|MPa|bar|psi|°C|°F|%)\b",
    _LEADER_DOTS_PATTERN,
]

# "Relaxed": NÃO protege números genéricos (evita excesso de placeholders), mas mantém
# proteção para coisas que quebram fácil e *leader dots*.
ENTITY_PATTERNS_RELAXED = [
    r"https?://\S+",
    r"\b\w+@\w+\.[A-Za-z]{2,}\b",
    r"\b\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b",  # datas simples
    r"\b[A-Z]{2,}\d+\b",  # códigos tipo ABC123
    r"\b\d+(?:[\.,]\d+)?\s?(?:kg|g|mg|lb|t|m|cm|mm|km|mi|in|ft|V|kV|mV|A|mA|Hz|kHz|MHz|rpm|N\.?m|N·m|Pa|kPa|MPa|bar|psi|°C|°F|%)\b",
    _LEADER_DOTS_PATTERN,
]

ENTITY_RE_DEFAULT = re.compile("|".join(f"({p})" for p in ENTITY_PATTERNS_DEFAULT))
ENTITY_RE_RELAXED = re.compile("|".join(f"({p})" for p in ENTITY_PATTERNS_RELAXED))


_TOKEN_PREFIX_RE = re.compile(r"[^A-Za-z0-9]+")
# Tokens são desenhados para sobreviver bem a tradutores (somente letras/dígitos).
# Formato: ZXQ{KIND}{PREFIX}X0000ZXQ  (ex.: ZXQENTB001S002X0003ZXQ)
# Placeholders internos do projeto.
#
# IMPORTANTE:
#  - O miolo do token usa quantificador *não-guloso* (*?*) para evitar capturar
#    múltiplos tokens consecutivos como se fosse um único (isso causava
#    vazamento de ZXQ no PDF final quando havia tokens adjacentes).
_TOKEN_RE = re.compile(r"ZXQ(?:ENT|GLOS)[A-Z0-9]*?X\d{4}ZXQ", re.I)

# Versão tolerante a pequenas corrupções do token (ex.: falta de 'Q', zeros
# removidos, 'ZXXQ...' etc.). Usada como segunda passada de restauração.
_TOKEN_FUZZY_RE = re.compile(
    r"Z[XQ]{0,5}(?P<kind>ENT|GLOS)(?P<prefix>[A-Z0-9]{0,32}?)X?(?P<idx>\d{1,4})Z[XQ]{0,5}",
    re.I,
)

# Terceira passada: alguns tradutores podem inserir espaços entre os caracteres do token
# (ex.: "Z X Q ENT B001 X 0003 Z X Q"). Esta regex captura esse caso.
_TOKEN_SPACED_RE = re.compile(
    r"Z\s*X\s*Q\s*(?P<kind>ENT|GLOS)\s*(?P<prefix>[A-Z0-9]{0,32}?)\s*X\s*0*(?P<idx>\d{1,4})\s*Z\s*X\s*Q",
    re.I,
)


# ----------------------------
# Idiomas / PT-BR (alias: pb)
# ----------------------------

_PTBR_ALIASES = {
    "pb",
    "pt-br",
    "pt_br",
    "ptbr",
    "ptbrasil",
}


def normalize_lang_code(lang: str) -> str:
    """Normaliza códigos de idioma vindos do CLI/config.

    - Aceita 'pb'/'pt-br' como PT-BR interno ('pb')
    - Converte variantes como 'en-US' -> 'en'
    """

    l = (lang or "").strip().lower().replace("_", "-")
    if not l:
        return l
    if l in _PTBR_ALIASES:
        return "pb"
    if "-" in l:
        l = l.split("-", 1)[0]
    return l


def lang_for_translator(provider_name: str, lang: str) -> str:
    """Mapeia o código de idioma para o que cada provider aceita.

    Observação importante:
      - Internamente usamos "pb" como alias para PT-BR.
      - Alguns providers aceitam variante regionalizada (pt-BR / pt-br),
        outros só aceitam "pt".
    """
    prov = (provider_name or "").lower().strip()
    l = normalize_lang_code(lang)

    if l == "pb":
        # "pb" é nosso alias interno para PT-BR.
        if prov == "mymemory":
            # MyMemory aceita código regionalizado.
            return "pt-br"
        if prov == "translategemma":
            # TranslateGemma aceita código regionalizado (ex.: pt-BR).
            return "pt-BR"
        # LibreTranslate (e outros) normalmente usam "pt".
        return "pt"

    return l


def _match_case(repl: str, original: str) -> str:
    if not original:
        return repl
    if original.isupper():
        return repl.upper()
    if original[0].isupper():
        return repl.capitalize()
    return repl


# Regras simples (alto sinal) para aproximar PT-PT -> PT-BR.
# Obs.: isto NÃO tenta reescrever frases; só corrige ortografia/termos muito comuns.
_PTBR_RULES = [
    # atual
    (re.compile(r"\bactualmente\b", re.I), "atualmente"),
    (re.compile(r"\bactual\b", re.I), "atual"),
    # facto/factos (evitar 'de facto')
    (re.compile(r"(?<!\bde\s)\bfactos\b", re.I), "fatos"),
    (re.compile(r"(?<!\bde\s)\bfacto\b", re.I), "fato"),
    # equipa
    (re.compile(r"\bequipas\b", re.I), "equipes"),
    (re.compile(r"\bequipa\b", re.I), "equipe"),
    # projecto
    (re.compile(r"\bprojectos\b", re.I), "projetos"),
    (re.compile(r"\bprojecto\b", re.I), "projeto"),
    # objectivo
    (re.compile(r"\bobjectivos\b", re.I), "objetivos"),
    (re.compile(r"\bobjectivo\b", re.I), "objetivo"),
    (re.compile(r"\bobjectivas\b", re.I), "objetivas"),
    (re.compile(r"\bobjectiva\b", re.I), "objetiva"),
    # actividade
    (re.compile(r"\bactividades\b", re.I), "atividades"),
    (re.compile(r"\bactividade\b", re.I), "atividade"),
    # optimiza-
    (re.compile(r"\boptimiza", re.I), "otimiza"),
    (re.compile(r"\boptimi", re.I), "otimi"),
    # contacto
    (re.compile(r"\bcontactos\b", re.I), "contatos"),
    (re.compile(r"\bcontacto\b", re.I), "contato"),
]


def ptbr_postprocess(text: str) -> str:
    """Aplica regras simples de PT-BR sem mexer nos tokens ZXQ (placeholders)."""

    s = text or ""

    def apply_rules(seg: str) -> str:
        out = seg
        for rx, repl in _PTBR_RULES:
            out = rx.sub(lambda m: _match_case(repl, m.group(0)), out)
        return out

    parts = []
    last = 0
    # _TOKEN_RE existe neste módulo e captura os placeholders ZXQ...
    for m in _TOKEN_RE.finditer(s):
        parts.append(apply_rules(s[last : m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(apply_rules(s[last:]))
    return "".join(parts)


def _safe_token_prefix(prefix: str) -> str:
    """Normaliza prefixo para token: mantém apenas A-Z0-9.

    Motivo:
    - Alguns tradutores mexem em '_' e pontuação, quebrando placeholders.
    - Com letras/dígitos, a chance de alteração cai bastante.
    """
    p = _TOKEN_PREFIX_RE.sub("", prefix or "")
    return p.upper()


def _make_token(kind: str, token_prefix: str, idx: int) -> str:
    pref = _safe_token_prefix(token_prefix)
    return f"ZXQ{kind}{pref}X{idx:04d}ZXQ"


def protect_entities(text: str, token_prefix: str = "", mode: str = "default") -> Tuple[str, Dict[str, str]]:
    """Protege entidades para reduzir erros de tradução (números, urls, unidades etc).

    mode:
      - "default": protege números genéricos + unidades + datas + urls + etc.
      - "relaxed": evita proteger números genéricos (menos placeholders), mas mantém
        proteção para itens frágeis (urls/emails/códigos/unidades) e *leader dots*.
    """
    mode_norm = str(mode).lower().strip()
    if mode_norm in ("none", "off", "disable", "disabled", "no"):
        return (text or ""), {}

    mapping: Dict[str, str] = {}
    idx = 0

    ent_re = ENTITY_RE_RELAXED if mode_norm == "relaxed" else ENTITY_RE_DEFAULT

    def repl(m: re.Match) -> str:
        nonlocal idx
        token = _make_token("ENT", token_prefix, idx)
        mapping[token] = m.group(0)
        idx += 1
        return token

    protected = ent_re.sub(repl, text or "")
    return protected, mapping


def restore_placeholders(text: str, mapping: Dict[str, str]) -> str:
    """Restaura placeholders (ENT/GLOS) no texto traduzido.

    Observação:
    - Usamos regex para substituir tokens de forma robusta (case-insensitive).
    """
    out = text or ""
    if not out:
        return out

    # Importante: esta função pode ser chamada em sequência para *diferentes* tipos de placeholder
    # (ex.: ENT e depois GLOS). Portanto, quando não há mapping (ou quando o mapping não contém
    # um token específico), NUNCA devemos remover tokens desconhecidos aqui — isso poderia apagar
    # placeholders que serão restaurados em uma chamada posterior.
    if not mapping:
        return out
    map_lower = {k.lower(): v for k, v in mapping.items()}

    def _repl_exact(m: re.Match) -> str:
        key = m.group(0).lower()
        return map_lower.get(key, m.group(0))

    out = _TOKEN_RE.sub(_repl_exact, out)

    # Passo 2 (resgate): tokens podem aparecer levemente corrompidos pela engine
    # (ex.: "ZXENTX001" / "ZXXQENTX0001ZX" / perda do sufixo "ZXQ").
    # Tentamos reconstruir o token canônico a partir de (kind, prefix, idx).
    def _repl_fuzzy(m: re.Match) -> str:
        kind = (m.group("kind") or "").upper()
        prefix = (m.group("prefix") or "").upper()
        idx_raw = (m.group("idx") or "0")
        try:
            idx_int = int(idx_raw)
        except Exception:
            return m.group(0)
        idx = f"{idx_int:04d}"
        canon = f"ZXQ{kind}{prefix}X{idx}ZXQ".lower()
        return map_lower.get(canon, m.group(0))

    out = _TOKEN_FUZZY_RE.sub(_repl_fuzzy, out)

    # Passo 3 (resgate): tokens com espaços entre caracteres
    def _repl_spaced(m: re.Match) -> str:
        kind = (m.group("kind") or "").upper()
        prefix = (m.group("prefix") or "").upper()
        idx_raw = (m.group("idx") or "0")
        try:
            idx_int = int(idx_raw)
        except Exception:
            return m.group(0)
        idx = f"{idx_int:04d}"
        canon = f"ZXQ{kind}{prefix}X{idx}ZXQ".lower()
        return map_lower.get(canon, m.group(0))

    out = _TOKEN_SPACED_RE.sub(_repl_spaced, out)

    return out


# ----------------------------
# Glossário (opcional)
# ----------------------------

def load_glossary(glossary_path: Path) -> Dict[str, str]:
    """Carrega glossário de YAML/JSON simples: {"term_en": "termo_pt"}.

    Se não existir, retorna {}.
    """
    glossary_path = Path(glossary_path)
    if not glossary_path.exists():
        return {}

    raw = glossary_path.read_text(encoding="utf-8")
    if glossary_path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(raw) or {}
    elif glossary_path.suffix.lower() == ".json":
        data = json.loads(raw) or {}
    else:
        raise ValueError("Glossário deve ser .yaml/.yml ou .json")

    if not isinstance(data, dict):
        raise ValueError("Glossário inválido: esperado um dict (mapeamento termo->termo).")

    out: Dict[str, str] = {}
    for k, v in data.items():
        if k is None or v is None:
            continue
        out[str(k)] = str(v)
    return out


def load_do_not_translate(path: Path) -> List[str]:
    """Carrega lista de termos que não devem ser traduzidos (.yaml/.yml/.json/.txt)."""
    p = Path(path)
    if not p.exists():
        return []

    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(raw) or []
    elif p.suffix.lower() == ".json":
        data = json.loads(raw) or []
    else:
        data = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]

    if isinstance(data, dict):
        data = data.get("terms") or []

    out: List[str] = []
    if isinstance(data, list):
        for it in data:
            t = str(it or "").strip()
            if t:
                out.append(t)
    return out


def protect_do_not_translate_terms(text: str, terms: List[str], token_prefix: str = "") -> Tuple[str, Dict[str, str]]:
    """Protege termos que devem permanecer idênticos no resultado final."""
    if not terms:
        return text or "", {}

    mapping: Dict[str, str] = {}
    protected = text or ""
    idx = 0

    for term in sorted({t for t in terms if str(t).strip()}, key=len, reverse=True):
        token = _make_token("ENT", token_prefix, idx)
        if re.match(r"^[A-Za-z0-9_\-]+$", str(term)):
            pattern = r"\b" + re.escape(str(term)) + r"\b"
        else:
            pattern = re.escape(str(term))
        protected_new, n = re.subn(pattern, token, protected)
        if n > 0:
            protected = protected_new
            mapping[token] = str(term)
            idx += 1

    return protected, mapping


def protect_glossary_terms(text: str, glossary: Dict[str, str], token_prefix: str = "") -> Tuple[str, Dict[str, str]]:
    """Substitui termos do glossário por placeholders e guarda mapping para restauração.

    Estratégia:
    - Antes de traduzir: substitui cada termo 'source' por um token ZXQGLOS... (placeholder).
    - Depois de traduzir: restaura o token para o termo alvo (pt-BR).

    token_prefix existe para permitir *batch translate* sem colisão de placeholders.
    """
    if not glossary:
        return text or "", {}

    mapping: Dict[str, str] = {}
    protected = text or ""
    idx = 0

    # Ordena por tamanho decrescente para evitar conflitos (ex: "API" e "API key")
    items = sorted(glossary.items(), key=lambda kv: len(kv[0]), reverse=True)

    for src, tgt in items:
        if not str(src).strip():
            continue
        token = _make_token("GLOS", token_prefix, idx)

        # tenta casar como palavra inteira quando possível
        if re.match(r"^[A-Za-z0-9_\-]+$", str(src)):
            pattern = r"\b" + re.escape(str(src)) + r"\b"
        else:
            pattern = re.escape(str(src))

        protected_new, n = re.subn(pattern, token, protected)
        if n > 0:
            protected = protected_new
            mapping[token] = str(tgt)
            idx += 1

    return protected, mapping


# ----------------------------
# Chunking simples (por frases)
# ----------------------------

_SENT_SPLIT = re.compile(r"(?<=[\.!\?])\s+")


def chunk_text(text: str, max_chars: int) -> List[str]:
    """Quebra texto grande em pedaços <= max_chars, tentando respeitar frases."""
    text = (text or "").strip()
    if not text:
        return [""]

    if len(text) <= max_chars:
        return [text]

    parts: List[str] = []
    cur = ""
    for sent in _SENT_SPLIT.split(text):
        if not sent:
            continue
        if not cur:
            cur = sent
            continue
        if len(cur) + 1 + len(sent) <= max_chars:
            cur = cur + " " + sent
        else:
            parts.append(cur)
            cur = sent
    if cur:
        parts.append(cur)

    # Se ainda tiver pedaço > max_chars (sentença gigante), quebra na marra
    fixed: List[str] = []
    for p in parts:
        if len(p) <= max_chars:
            fixed.append(p)
            continue
        start = 0
        while start < len(p):
            fixed.append(p[start : start + max_chars])
            start += max_chars
    return fixed


# ----------------------------
# Tradutores
# ----------------------------

class TranslatorBase:
    provider_name: str

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:  # pragma: no cover
        raise NotImplementedError


@dataclass
class LibreTranslateTranslator(TranslatorBase):
    base_url: str
    api_key: str = ""

    # Timeouts configuráveis (segundos). Em self-host normalmente 10-60s é suficiente,
    # mas em 1ª execução (download de modelos) pode demorar.
    timeout_sec: float = 120.0

    # Session reutilizável = menos overhead HTTP (especialmente em 500+ páginas)
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    provider_name: str = "libretranslate"

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type((requests.RequestException,)),
    )
    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        url = self.base_url.rstrip("/") + "/translate"
        payload = {
            "q": text,
            "source": source_lang,
            "target": target_lang,
            "format": "text",
        }
        if self.api_key:
            payload["api_key"] = self.api_key

        r = self.session.post(url, json=payload, timeout=float(self.timeout_sec))
        r.raise_for_status()
        data = r.json()
        out = data.get("translatedText")
        if not isinstance(out, str):
            raise RuntimeError(f"Resposta inesperada do LibreTranslate: {data}")
        return out


@dataclass
class MyMemoryTranslator(TranslatorBase):
    email: str = ""
    timeout_sec: float = 120.0
    session: requests.Session = field(default_factory=requests.Session, repr=False)
    provider_name: str = "mymemory"

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type((requests.RequestException,)),
    )
    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        # MyMemory: GET com q (limite ~500 bytes) e langpair "en|pt"
        url = "https://api.mymemory.translated.net/get"
        params = {
            "q": text,
            "langpair": f"{source_lang}|{target_lang}",
        }
        if self.email:
            params["de"] = self.email

        r = self.session.get(url, params=params, timeout=float(self.timeout_sec))
        r.raise_for_status()
        data = r.json()
        resp = data.get("responseData") or {}
        out = resp.get("translatedText")
        if not isinstance(out, str):
            raise RuntimeError(f"Resposta inesperada do MyMemory: {data}")
        return out


class TranslateGemmaTranslator(TranslatorBase):
    """Tradutor via API OpenAI-compatible (ex.: Docker Model Runner).

    Espera um endpoint estilo:
      POST {base_url}/chat/completions

    Por padrão, tenta usar o "template" do TranslateGemma (campos source_lang_code/target_lang_code).
    Se o servidor rejeitar o payload (HTTP 400), faz fallback para um prompt em texto puro.
    """

    provider_name = "translategemma"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_sec: float = 120.0,
        api_key: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout_sec = float(timeout_sec)
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self._session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self.extra_headers)
        return h

    @staticmethod
    def _extract_chat_content(data: Dict[str, Any]) -> str:
        try:
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg = (choices[0].get("message") or {}) if isinstance(choices[0], dict) else {}
            content = msg.get("content")
            if isinstance(content, list):
                # Alguns servers podem devolver lista de partes (content parts)
                parts: List[str] = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        if item.get("type") == "text" and "text" in item:
                            parts.append(str(item.get("text") or ""))
                return "".join(parts).strip()
            if content is not None:
                return str(content).strip()

            # Fallback (algumas implementações antigas)
            if "text" in choices[0]:
                return str(choices[0].get("text") or "").strip()
        except Exception:
            return ""
        return ""

    @retry(
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    def _chat(self, messages: List[Dict[str, Any]], temperature: float = 0.0, max_tokens: Optional[int] = None) -> str:
        url = f"{self.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": float(temperature),
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        r = self._session.post(url, headers=self._headers(), json=payload, timeout=self.timeout_sec)

        # Não vale a pena retry em 4xx (exceto 429). Em 5xx/429, deixamos o raise_for_status acionar o retry.
        if r.status_code == 429 or r.status_code >= 500:
            r.raise_for_status()

        if r.status_code >= 400:
            detail = None
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise ValueError(f"OpenAI-compatible API error {r.status_code}: {detail}")

        data = r.json()
        return self._extract_chat_content(data)

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""

        # O pipeline pode passar códigos já normalizados; ainda assim normalizamos por segurança.
        src = lang_for_translator(self.provider_name, source_lang)
        tgt = lang_for_translator(self.provider_name, target_lang)

        # Estimativa simples para evitar truncamento em servidores que usam um default baixo de max_tokens.
        # (Tokens ~ caracteres/4; tradução costuma ser do mesmo tamanho.)
        approx_in_tokens = max(1, len(t) // 4)
        max_tokens = min(2048, max(256, int(approx_in_tokens * 2)))

        system_prompt = (
            "You are a professional translation engine. "
            f"Translate from {src} to {tgt}. "
            "Return ONLY the translated text (no quotes, no explanations). "
            "Preserve line breaks, punctuation and list markers. "
            "Do NOT translate or modify placeholder tokens that look like 'ZXQ...ZXQ'."
        )

        # 1) Tentativa: formato esperado pelo chat template do TranslateGemma
        messages_tpl: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "source_lang_code": src,
                        "target_lang_code": tgt,
                        "text": t,
                    }
                ],
            }
        ]

        try:
            out = self._chat(messages_tpl, temperature=0.0, max_tokens=max_tokens)
            if out:
                return out.strip()
        except ValueError as e:
            # 400 é relativamente comum quando o servidor valida estritamente o schema do OpenAI.
            # Fazemos fallback para um prompt em texto puro.
            _ = e

        # 2) Fallback: prompt em texto puro (mais compatível)
        messages_plain: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": t},
        ]
        out = self._chat(messages_plain, temperature=0.0, max_tokens=max_tokens)
        return (out or "").strip()

class DummyTranslator(TranslatorBase):
    provider_name: str = "dummy"

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        return text


def build_translator(cfg: dict) -> TranslatorBase:
    tcfg = (cfg or {}).get("translator", {}) or {}
    provider = str(tcfg.get("provider") or "libretranslate").strip().lower()

    # Timeout genérico (usado por LibreTranslate / MyMemory)
    timeout_sec = float(tcfg.get("timeout_sec") or 60.0)

    if provider == "mymemory":
        email = tcfg.get("mymemory_email") or os.environ.get("MYMEMORY_EMAIL") or ""
        return MyMemoryTranslator(email=email, timeout_sec=timeout_sec)

    if provider in ("opusmt", "opus_mt", "opus-mt"):
        # Local Marian/OPUS-MT via HuggingFace (transformers + torch)
        # Import lazy para não forçar dependências quando o usuário usa tradutores HTTP.
        from .hf_opusmt import OpusMTTranslator

        model_name = tcfg.get("opusmt_model") or os.environ.get("OPUSMT_MODEL") or "Helsinki-NLP/opus-mt-tc-big-en-pt"
        target_token_ptbr = tcfg.get("opusmt_target_token_ptbr") or ">>pob<<"
        target_token_ptpt = tcfg.get("opusmt_target_token_ptpt") or ">>por<<"
        device = tcfg.get("opusmt_device") or os.environ.get("OPUSMT_DEVICE") or "auto"
        num_beams = int(tcfg.get("opusmt_num_beams") or os.environ.get("OPUSMT_NUM_BEAMS") or 4)
        batch_size = int(tcfg.get("opusmt_batch_size") or os.environ.get("OPUSMT_BATCH_SIZE") or 4)
        max_input_tokens = int(tcfg.get("opusmt_max_input_tokens") or os.environ.get("OPUSMT_MAX_INPUT_TOKENS") or 384)
        max_new_tokens = int(tcfg.get("opusmt_max_new_tokens") or os.environ.get("OPUSMT_MAX_NEW_TOKENS") or 512)
        fp16 = str(tcfg.get("opusmt_fp16") or os.environ.get("OPUSMT_FP16") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
            "y",
        )
        hf_cache_dir = tcfg.get("opusmt_hf_cache_dir") or os.environ.get("OPUSMT_HF_CACHE_DIR") or ""

        return OpusMTTranslator(
            model_name,
            target_token_ptbr=target_token_ptbr,
            target_token_ptpt=target_token_ptpt,
            device=device,
            num_beams=num_beams,
            batch_size=batch_size,
            max_input_tokens=max_input_tokens,
            max_new_tokens=max_new_tokens,
            fp16=fp16,
            hf_cache_dir=hf_cache_dir,
        )

    if provider == "translategemma":
        # OpenAI-compatible endpoint (ex.: Docker Model Runner)
        base_url = (
            tcfg.get("translategemma_url")
            or os.environ.get("TRANSLATEGEMMA_URL")
            or "http://127.0.0.1:12434/engines/v1"
        )
        model = (
            tcfg.get("translategemma_model")
            or os.environ.get("TRANSLATEGEMMA_MODEL")
            or "aistaging/translategemma-vllm:27B"
        )
        api_key = tcfg.get("translategemma_api_key") or os.environ.get("TRANSLATEGEMMA_API_KEY") or None
        tg_timeout = float(
            tcfg.get("translategemma_timeout_sec") or os.environ.get("TRANSLATEGEMMA_TIMEOUT_SEC") or 120.0
        )

        return TranslateGemmaTranslator(
            base_url=base_url,
            model=model,
            timeout_sec=tg_timeout,
            api_key=api_key,
        )

    if provider == "dummy":
        return DummyTranslator()

    # default: libretranslate
    url = tcfg.get("libretranslate_url") or os.environ.get("LIBRETRANSLATE_URL") or "http://127.0.0.1:5000"
    api_key = tcfg.get("libretranslate_api_key") or os.environ.get("LIBRETRANSLATE_API_KEY") or ""
    return LibreTranslateTranslator(base_url=url, api_key=api_key, timeout_sec=timeout_sec)


# ----------------------------
# Tradução com cache (unitária)
# ----------------------------

def translate_with_cache(
    cache: TranslationCache,
    translator: TranslatorBase,
    text: str,
    source_lang: str,
    target_lang: str,
    max_chars_per_request: int,
    provider_id: Optional[str] = None,
    glossary: Optional[Dict[str, str]] = None,
    entity_mode: str = "default",
    do_not_translate_terms: Optional[List[str]] = None,
) -> str:
    """Traduz com cache + chunking + proteção de entidades + glossário (opcional)."""
    text = (text or "").strip()
    if not text:
        return ""

    provider_key = provider_id or translator.provider_name

    source_lang_n = normalize_lang_code(source_lang)
    target_lang_n = normalize_lang_code(target_lang)
    source_lang_api = lang_for_translator(translator.provider_name, source_lang_n)
    target_lang_api = lang_for_translator(translator.provider_name, target_lang_n)

    cached = cache.get(provider_key, source_lang_n, target_lang_n, text)
    if cached is not None:
        return cached

    # 1) protege termos que não devem ser traduzidos
    text1, dnt_map = protect_do_not_translate_terms(text, do_not_translate_terms or [])

    # 2) protege termos do glossário (força resultado final)
    text2, gloss_map = protect_glossary_terms(text1, glossary or {})

    # 3) protege entidades (números, urls, etc)
    protected, ent_map = protect_entities(text2, mode=entity_mode)

    # 3) quebra em chunks (por limite de API)
    chunks = chunk_text(protected, max_chars=max_chars_per_request)
    translated_chunks: List[str] = []
    for ch in chunks:
        if not ch.strip():
            continue
        translated_chunks.append(translator.translate(ch, source_lang=source_lang_api, target_lang=target_lang_api))

    translated = " ".join(translated_chunks).strip()

    # 4) restaura entidades, glossário e termos protegidos
    translated = restore_placeholders(translated, ent_map)
    translated = restore_placeholders(translated, gloss_map)
    translated = restore_placeholders(translated, dnt_map)

    # 5) pequenos ajustes determinísticos (pontuação/whitespace)
    translated = postprocess_translation(translated, src=text)
    if target_lang_n == 'pb':
        translated = ptbr_postprocess(translated)

    cache.put(provider_key, source_lang_n, target_lang_n, text, translated)
    return translated


# ----------------------------
# Tradução com cache (em lote)
# ----------------------------

_SEG_MARK = "ZXQSEGBOUNDARYZXQ"

_PUNCT_SPACE_BEFORE_RE = re.compile(r"\s+([,.;:!?])")
_PUNCT_SPACE_AFTER_RE = re.compile(r"([,.;:!?])([A-Za-zÀ-ÖØ-öø-ÿ])")
_PERCENT_SPACE_RE = re.compile(r"\s+%")


def _looks_like_leader_dots(line: str) -> bool:
    """Heurística: linha parecida com sumário (leader dots)."""
    s = line or ""
    dots = s.count(".")
    if dots < 10:
        return False
    if re.search(r"(?:\.\s){5,}", s):
        return True
    if re.search(r"\.{10,}", s):
        return True
    return False


def _is_mostly_upper_short_heading(src: str) -> bool:
    s = (src or "").strip()
    # Só aplicar em trechos curtos (títulos) para não gritar o texto.
    if len(re.findall(r"\w+", s)) > 8:
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) >= 0.85


def postprocess_translation(dst: str, src: str = "") -> str:
    """Pequenos ajustes *determinísticos* para melhorar legibilidade.

    Mantém leader dots (TOC) intactos.
    """
    out = (dst or "").replace("\u00a0", " ")
    lines = out.split("\n")
    cleaned: List[str] = []
    for line in lines:
        if _looks_like_leader_dots(line):
            cleaned.append(line.rstrip())
            continue
        s = line
        # IMPORTANTE: em re.sub, "\1"/"\2" referenciam grupos capturados.
        # Se usar "\\1"/"\\2", o resultado vira texto literal "\1"/"\2".
        s = _PUNCT_SPACE_BEFORE_RE.sub(r"\1", s)
        s = _PUNCT_SPACE_AFTER_RE.sub(r"\1 \2", s)
        s = _PERCENT_SPACE_RE.sub("%", s)
        # Espaços múltiplos -> simples (sem afetar linhas de TOC)
        s = re.sub(r"[ \t]{2,}", " ", s)
        cleaned.append(s.strip())
    out = "\n".join(cleaned).strip()
    if _is_mostly_upper_short_heading(src):
        out = out.upper()
    return out
_SEG_SPLIT_RE = re.compile(r"\s*" + re.escape(_SEG_MARK) + r"\s*")


def _batch_enabled(translator: TranslatorBase, batch_mode: Union[str, bool, None]) -> bool:
    if batch_mode is None:
        return translator.provider_name == "libretranslate"

    if isinstance(batch_mode, bool):
        return bool(batch_mode)

    mode = str(batch_mode).strip().lower()
    if mode in ("0", "false", "off", "no"):
        return False
    if mode in ("1", "true", "on", "yes"):
        return True
    # auto
    return translator.provider_name == "libretranslate"


def translate_many_with_cache(
    cache: TranslationCache,
    translator: TranslatorBase,
    texts: List[str],
    source_lang: str,
    target_lang: str,
    max_chars_per_request: int,
    provider_id: Optional[str] = None,
    glossary: Optional[Dict[str, str]] = None,
    batch_mode: Union[str, bool, None] = "auto",
    entity_mode: str = "default",
    do_not_translate_terms: Optional[List[str]] = None,
) -> List[str]:
    """Traduz uma lista de strings de forma eficiente.

    Melhorias vs traduzir 1-por-1:
    - Deduplica textos repetidos na página.
    - Usa cache SQLite + LRU em memória.
    - (Opcional) Faz *batch translate* (várias strings por request), reduzindo MUITO o overhead
      em PDFs grandes (ex.: 500 páginas).

    Observação:
    - Batch é habilitado por padrão apenas para LibreTranslate (local). Para MyMemory, por segurança,
      fica desligado por padrão (muitos limites/peculiaridades).
    """
    provider_key = provider_id or translator.provider_name

    source_lang_n = normalize_lang_code(source_lang)
    target_lang_n = normalize_lang_code(target_lang)
    source_lang_api = lang_for_translator(translator.provider_name, source_lang_n)
    target_lang_api = lang_for_translator(translator.provider_name, target_lang_n)
    glossary = glossary or {}

    out: List[str] = [""] * len(texts)

    # normaliza + agrupa por texto (dedupe)
    by_text: Dict[str, List[int]] = {}
    for i, t in enumerate(texts):
        t2 = (t or "").strip()
        if not t2:
            out[i] = ""
            continue
        by_text.setdefault(t2, []).append(i)

    unique_texts = list(by_text.keys())
    if not unique_texts:
        return out

    # 1) cache em lote
    cached_map = cache.get_many(provider_key, source_lang, target_lang, unique_texts)

    remaining: List[str] = []
    for t in unique_texts:
        if t in cached_map:
            for idx in by_text[t]:
                out[idx] = cached_map[t]
        else:
            remaining.append(t)

    if not remaining:
        return out

    # 2) decide se usa batch translate
    enable_batch = _batch_enabled(translator, batch_mode)

    # Estratégia segura: textos muito grandes vão individual (chunking)
    max_single = max(50, int(max_chars_per_request * 0.80))

    oversized: List[str] = []
    batchable: List[str] = []
    for t in remaining:
        if len(t) > max_single:
            oversized.append(t)
        else:
            batchable.append(t)

    # 2a) oversized: traduz individual
    for t in oversized:
        tr = translate_with_cache(
            cache=cache,
            translator=translator,
            text=t,
            source_lang=source_lang,
            target_lang=target_lang,
            max_chars_per_request=max_chars_per_request,
            provider_id=provider_key,
            glossary=glossary,
            entity_mode=entity_mode,
            do_not_translate_terms=do_not_translate_terms,
        )
        for idx in by_text[t]:
            out[idx] = tr

    if not batchable:
        return out

    if not enable_batch:
        # fallback: traduz 1-por-1, mas ainda deduplicado
        for t in batchable:
            tr = translate_with_cache(
                cache=cache,
                translator=translator,
                text=t,
                source_lang=source_lang,
                target_lang=target_lang,
                max_chars_per_request=max_chars_per_request,
                provider_id=provider_key,
                glossary=glossary,
                entity_mode=entity_mode,
                do_not_translate_terms=do_not_translate_terms,
            )
            for idx in by_text[t]:
                out[idx] = tr
        return out

    # 3) batch translate: empacota várias strings por request, respeitando max_chars_per_request
    sep = f"\n\n{_SEG_MARK}\n\n"
    sep_len = len(sep)

    batches: List[List[str]] = []
    cur: List[str] = []
    cur_len = 0

    for t in batchable:
        est = len(t) + sep_len
        if cur and (cur_len + est) > max_chars_per_request:
            batches.append(cur)
            cur = [t]
            cur_len = len(t)
        else:
            cur.append(t)
            cur_len = cur_len + est

    if cur:
        batches.append(cur)

    for b_idx, batch in enumerate(batches):
        # Protege glossário/entidades por segmento com prefixo único
        protected_texts: List[str] = []
        maps: List[Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]] = []

        for s_idx, t in enumerate(batch):
            prefix = f"b{b_idx:03d}_s{s_idx:03d}_"
            t0, dnt_map = protect_do_not_translate_terms(t, do_not_translate_terms or [], token_prefix=prefix)
            t1, gloss_map = protect_glossary_terms(t0, glossary, token_prefix=prefix)
            t2, ent_map = protect_entities(t1, token_prefix=prefix, mode=entity_mode)
            protected_texts.append(t2)
            maps.append((dnt_map, gloss_map, ent_map))

        joined = sep.join(protected_texts)

        # Se por algum motivo estourar, cai para individual (seguro)
        if len(joined) > max_chars_per_request:
            for t in batch:
                tr = translate_with_cache(
                    cache=cache,
                    translator=translator,
                    text=t,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    max_chars_per_request=max_chars_per_request,
                    provider_id=provider_key,
                    glossary=glossary,
                    entity_mode=entity_mode,
                    do_not_translate_terms=do_not_translate_terms,
                )
                for idx in by_text[t]:
                    out[idx] = tr
            continue

        try:
            translated_joined = translator.translate(joined, source_lang=source_lang_api, target_lang=target_lang_api)
        except Exception:
            # fallback seguro: individual
            for t in batch:
                tr = translate_with_cache(
                    cache=cache,
                    translator=translator,
                    text=t,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    max_chars_per_request=max_chars_per_request,
                    provider_id=provider_key,
                    glossary=glossary,
                    entity_mode=entity_mode,
                    do_not_translate_terms=do_not_translate_terms,
                )
                for idx in by_text[t]:
                    out[idx] = tr
            continue

        parts = [p.strip() for p in _SEG_SPLIT_RE.split(translated_joined or "")]

        if len(parts) != len(batch):
            # Se o tradutor mexer no marcador, reverte com segurança
            for t in batch:
                tr = translate_with_cache(
                    cache=cache,
                    translator=translator,
                    text=t,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    max_chars_per_request=max_chars_per_request,
                    provider_id=provider_key,
                    glossary=glossary,
                    entity_mode=entity_mode,
                    do_not_translate_terms=do_not_translate_terms,
                )
                for idx in by_text[t]:
                    out[idx] = tr
            continue

        pairs: List[Tuple[str, str]] = []
        for i_seg, translated_seg in enumerate(parts):
            dnt_map, gloss_map, ent_map = maps[i_seg]
            restored = restore_placeholders(translated_seg, ent_map)
            restored = restore_placeholders(restored, gloss_map)
            restored = restore_placeholders(restored, dnt_map)
            src = batch[i_seg]
            restored = postprocess_translation(restored, src=src).strip()
            if target_lang_n == 'pb':
                restored = ptbr_postprocess(restored)
            pairs.append((src, restored))
            for idx in by_text[src]:
                out[idx] = restored

        # grava no cache em lote
        cache.put_many(provider_key, source_lang_n, target_lang_n, pairs)

    return out
