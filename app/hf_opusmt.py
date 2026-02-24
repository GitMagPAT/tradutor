"""Local translator using HuggingFace Marian/OPUS-MT models.

Default model (recommended): Helsinki-NLP/opus-mt-tc-big-en-pt
- It supports multi-target Portuguese via an explicit target token:
  - >>pob<<  (Português do Brasil / PT-BR)
  - >>por<<  (Português / geralmente PT-PT)

This module is intentionally isolated so the project can still run without
transformers/torch when using HTTP translators.
"""

from __future__ import annotations

import os
import re
import threading
from typing import List, Optional


_ZXQSEP = "\n\n#ZXQSEP#\n\n"


def _norm_lang(lang: str) -> str:
    return (lang or "").strip().lower().replace("_", "-")


class OpusMTTranslator:
    """PT-BR translator using HuggingFace (Marian/OPUS-MT).

    Designed to be drop-in compatible with the project's translator interface:
    - provider_name
    - provider_id
    - translate(text, source_lang, target_lang) -> str

    Notes
    -----
    * Requires: torch + transformers + sentencepiece (+ sacremoses on some setups).
    * Runs on CPU by default; will use CUDA automatically if available and enabled.
    """

    provider_name = "opusmt"

    def __init__(
        self,
        model_name: str,
        *,
        target_token_ptbr: str = ">>pob<<",
        target_token_ptpt: str = ">>por<<",
        device: str = "auto",
        num_beams: int = 4,
        batch_size: int = 4,
        max_input_tokens: int = 384,
        max_new_tokens: int = 512,
        fp16: bool = False,
        hf_cache_dir: str = "",
    ) -> None:
        self.model_name = model_name
        self.target_token_ptbr = target_token_ptbr
        self.target_token_ptpt = target_token_ptpt
        self.device_pref = device
        self.num_beams = int(num_beams)
        self.batch_size = int(batch_size)
        self.max_input_tokens = int(max_input_tokens)
        self.max_new_tokens = int(max_new_tokens)
        self.fp16 = bool(fp16)
        self.hf_cache_dir = hf_cache_dir.strip() if hf_cache_dir else ""

        # Exposed for cache keys/logs
        self.provider_id = (
            f"opusmt:{self.model_name}"
            f":dev={self.device_pref}"
            f":beams={self.num_beams}"
            f":inTok={self.max_input_tokens}"
            f":outTok={self.max_new_tokens}"
        )

        self._lock = threading.Lock()
        self._loaded = False
        self._torch = None
        self._tokenizer = None
        self._model = None
        self._device = "cpu"

    # ---------------------------
    # Lazy load
    # ---------------------------

    def _lazy_load(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return

            try:
                import torch  # type: ignore
                from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # type: ignore
            except Exception as e:  # pragma: no cover
                raise RuntimeError(
                    "OpusMTTranslator requer dependências extras. "
                    "Instale via PowerShell (recomendado) ou pip: "
                    "pip install torch transformers sentencepiece sacremoses"
                ) from e

            # Decide device
            dev_pref = (self.device_pref or "auto").strip().lower()
            if dev_pref == "auto":
                dev = "cuda" if torch.cuda.is_available() else "cpu"
            elif dev_pref in ("cpu", "cuda"):
                if dev_pref == "cuda" and not torch.cuda.is_available():
                    dev = "cpu"
                else:
                    dev = dev_pref
            else:
                dev = "cpu"

            cache_dir: Optional[str] = self.hf_cache_dir or None
            # Some environments require use_fast=False for Marian.
            tokenizer = AutoTokenizer.from_pretrained(self.model_name, cache_dir=cache_dir)
            model = AutoModelForSeq2SeqLM.from_pretrained(self.model_name, cache_dir=cache_dir)

            model.eval()
            model.to(dev)
            if self.fp16 and dev == "cuda":
                # fp16 only makes sense on GPU.
                try:
                    model.half()
                except Exception:
                    pass

            # Basic perf: keep CPU thread usage reasonable
            if dev == "cpu":
                try:
                    n = os.cpu_count() or 4
                    torch.set_num_threads(min(8, max(1, n)))
                except Exception:
                    pass

            self._torch = torch
            self._tokenizer = tokenizer
            self._model = model
            self._device = dev
            self._loaded = True

    # ---------------------------
    # Target selection
    # ---------------------------

    def _target_token(self, target_lang: str) -> str:
        tl = _norm_lang(target_lang)
        # The project uses "pb" internally for PT-BR.
        if tl in ("pb", "pt-br", "ptbr", "pt-brasil", "pt-brz"):
            return self.target_token_ptbr
        # If the user asks "pt" we still default to PT-BR because the project goal is PT-BR.
        if tl in ("pt", "por", "pt-pt", "ptpt"):
            return self.target_token_ptbr
        return self.target_token_ptbr

    # ---------------------------
    # Public API
    # ---------------------------

    def translate(self, text: str, *, source_lang: str, target_lang: str) -> str:
        """Translate text.

        Supports the internal batching marker used by the pipeline (#ZXQSEP#).
        """
        if not text:
            return ""

        # Split by the project's join marker (keeps cache semantics).
        if _ZXQSEP in text:
            parts = text.split(_ZXQSEP)
            out_parts: List[str] = []
            for i in range(0, len(parts), self.batch_size):
                chunk = parts[i : i + self.batch_size]
                out_parts.extend(self._translate_list(chunk, source_lang=source_lang, target_lang=target_lang))
            return _ZXQSEP.join(out_parts)

        return self._translate_list([text], source_lang=source_lang, target_lang=target_lang)[0]

    # ---------------------------
    # Internals
    # ---------------------------

    def _translate_list(self, texts: List[str], *, source_lang: str, target_lang: str) -> List[str]:
        self._lazy_load()
        assert self._tokenizer is not None
        assert self._model is not None
        assert self._torch is not None

        target_token = self._target_token(target_lang)

        # Ensure we never truncate silently.
        # If any text is too long in tokens, split it deterministically.
        out: List[str] = []
        for t in texts:
            out.append(self._translate_one(t, target_token=target_token))
        return out

    def _encode_len(self, text: str) -> int:
        assert self._tokenizer is not None
        # Token count for input side
        ids = self._tokenizer(text, add_special_tokens=True, return_tensors=None)["input_ids"]
        return len(ids)

    def _translate_one(self, text: str, *, target_token: str) -> str:
        # Small fast path
        prefixed = self._with_target_token(text, target_token)
        if self._encode_len(prefixed) <= self.max_input_tokens:
            return self._translate_batch([prefixed])[0]

        # Too long: split by paragraphs/sentences.
        parts = self._split_to_fit(text, target_token=target_token)
        if len(parts) == 1:
            # As a last resort, hard-split to avoid silent truncation.
            parts = self._hard_split(text, max_chars=800)

        translated_parts = []
        for i in range(0, len(parts), self.batch_size):
            batch = [self._with_target_token(p, target_token) for p in parts[i : i + self.batch_size]]
            translated_parts.extend(self._translate_batch(batch))
        return "".join(translated_parts)

    def _with_target_token(self, text: str, target_token: str) -> str:
        t = text.lstrip("\ufeff")
        if t.startswith(target_token):
            return t
        return f"{target_token} {t}"

    def _translate_batch(self, prefixed_texts: List[str]) -> List[str]:
        assert self._tokenizer is not None
        assert self._model is not None
        assert self._torch is not None

        # Tokenize
        enc = self._tokenizer(
            prefixed_texts,
            return_tensors="pt",
            padding=True,
            truncation=False,
        )
        enc = {k: v.to(self._device) for k, v in enc.items()}

        with self._torch.no_grad():
            gen = self._model.generate(
                **enc,
                num_beams=max(1, self.num_beams),
                max_new_tokens=max(16, self.max_new_tokens),
                early_stopping=True,
            )

        out = self._tokenizer.batch_decode(gen, skip_special_tokens=True)
        # Normalize newlines (some models may produce extra spaces)
        return [o.replace("\r\n", "\n") for o in out]

    def _split_to_fit(self, text: str, *, target_token: str) -> List[str]:
        """Split text into chunks that fit into max_input_tokens.

        Heuristic, but deterministic. Prioritizes preserving paragraph breaks.
        """
        # Keep separators so we can stitch back together with minimal formatting loss.
        pieces = re.split(r"(\n{2,})", text)
        out: List[str] = []
        cur = ""

        def flush() -> None:
            nonlocal cur
            if cur:
                out.append(cur)
                cur = ""

        for piece in pieces:
            if piece == "":
                continue
            candidate = cur + piece
            if self._encode_len(self._with_target_token(candidate, target_token)) <= self.max_input_tokens:
                cur = candidate
                continue

            # Would overflow
            if cur:
                flush()

            # If the piece itself is too large, try sentence splitting
            if self._encode_len(self._with_target_token(piece, target_token)) > self.max_input_tokens:
                sentences = re.split(r"(?<=[\.!\?])\s+", piece)
                for s in sentences:
                    if not s:
                        continue
                    cand2 = cur + ("" if not cur else " ") + s
                    if self._encode_len(self._with_target_token(cand2, target_token)) <= self.max_input_tokens:
                        cur = cand2
                    else:
                        if cur:
                            flush()
                        # sentence still too large -> hard split
                        for hs in self._hard_split(s, max_chars=600):
                            out.append(hs)
                        cur = ""
                flush()
            else:
                # Piece fits alone
                out.append(piece)

        if cur:
            out.append(cur)
        return out

    @staticmethod
    def _hard_split(text: str, *, max_chars: int = 800) -> List[str]:
        if len(text) <= max_chars:
            return [text]
        out: List[str] = []
        i = 0
        while i < len(text):
            out.append(text[i : i + max_chars])
            i += max_chars
        return out
