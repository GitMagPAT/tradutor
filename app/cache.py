from __future__ import annotations

import sqlite3
import threading
import re
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .utils import stable_hash

_BAD_CACHE_RE = re.compile(r"(?i)ZXQ|__ENT_|__GLOS_")

def _is_bad_cached_translation(s: str) -> bool:
    if not s:
        return False
    return _BAD_CACHE_RE.search(s) is not None



class TranslationCache:
    """Cache de traduções em SQLite + LRU em memória.

    Por que existe:
    - Para PDFs grandes (ex.: 500 páginas), abrir/conectar no SQLite a cada get/put
      vira gargalo. Aqui mantemos **uma conexão aberta** durante o run.
    - Além disso, mantemos um **LRU em memória** para evitar round-trips ao SQLite
      em traduções repetidas (cabeçalhos/rodapés etc.).

    Observação:
    - Esta implementação é single-process friendly.
    - Para threads, usamos lock e `check_same_thread=False` na conexão.
    """

    def __init__(
        self,
        db_path: Path,
        memory_max_entries: int = 20_000,
        commit_every: int = 50,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL;")
        self._con.execute("PRAGMA synchronous=NORMAL;")
        self._con.execute("PRAGMA temp_store=MEMORY;")
        self._init_db()

        self._mem: "OrderedDict[Tuple[str, str, str, str], str]" = OrderedDict()
        self._mem_max = int(memory_max_entries)

        self._commit_every = max(1, int(commit_every))
        self._writes_since_commit = 0

    def _init_db(self) -> None:
        with self._lock:
            self._con.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    provider TEXT NOT NULL,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    src_text TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    PRIMARY KEY (provider, source_lang, target_lang, text_hash)
                );
                """
            )
            self._con.commit()

    def close(self) -> None:
        """Fecha a conexão (e força commit pendente)."""
        with self._lock:
            try:
                self._con.commit()
            except Exception:
                pass
            try:
                self._con.close()
            except Exception:
                pass

    def __enter__(self) -> "TranslationCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _mem_get(self, key: Tuple[str, str, str, str]) -> Optional[str]:
        v = self._mem.get(key)
        if v is None:
            return None
        # LRU: move para o fim (mais recente)
        self._mem.move_to_end(key)
        return v

    def _mem_put(self, key: Tuple[str, str, str, str], value: str) -> None:
        self._mem[key] = value
        self._mem.move_to_end(key)
        # Evita memória infinita
        while len(self._mem) > self._mem_max:
            self._mem.popitem(last=False)

    def get(self, provider: str, source_lang: str, target_lang: str, text: str) -> Optional[str]:
        """Obtém uma tradução do cache.

        Se detectar vazamento de placeholders (ex.: 'ZXQ', '__ENT_'), invalida a entrada
        e retorna None para forçar re-tradução.
        """
        h = stable_hash(text)
        key = (provider, source_lang, target_lang, h)

        v = self._mem_get(key)
        if v is not None:
            if _is_bad_cached_translation(v):
                # cache contaminado: ignora (força miss)
                try:
                    self._mem.pop(key, None)
                except Exception:
                    pass
            else:
                return v

        with self._lock:
            cur = self._con.cursor()
            cur.execute(
                "SELECT translated_text FROM translations WHERE provider=? AND source_lang=? AND target_lang=? AND text_hash=?",
                key,
            )
            row = cur.fetchone()
            if row is None:
                return None

            out = row[0] or ""
            if _is_bad_cached_translation(out):
                # remove entrada ruim para evitar reaparecer em runs futuras
                try:
                    cur.execute(
                        "DELETE FROM translations WHERE provider=? AND source_lang=? AND target_lang=? AND text_hash=?",
                        key,
                    )
                    self._con.commit()
                except Exception:
                    pass
                return None

        self._mem_put(key, out)
        return out
    def get_many(
        self, provider: str, source_lang: str, target_lang: str, texts: List[str]
    ) -> Dict[str, str]:
        """Obtém traduções em batch do cache.

        Entradas contaminadas (ex.: contendo 'ZXQ' ou '__ENT_') são invalidadas e não
        são retornadas.
        """
        out: Dict[str, str] = {}
        missing: List[Tuple[str, str]] = []  # (text, hash)

        # 1) LRU
        for t in texts:
            h = stable_hash(t)
            key = (provider, source_lang, target_lang, h)
            v = self._mem_get(key)
            if v is not None and not _is_bad_cached_translation(v):
                out[t] = v
            else:
                if v is not None:
                    try:
                        self._mem.pop(key, None)
                    except Exception:
                        pass
                missing.append((t, h))

        if not missing:
            return out

        # 2) SQLite
        hs = [h for (_, h) in missing]
        placeholders = ",".join(["?"] * len(hs))
        params = [provider, source_lang, target_lang, *hs]

        bad_hashes: List[str] = []
        with self._lock:
            cur = self._con.cursor()
            cur.execute(
                f"SELECT text_hash, translated_text FROM translations WHERE provider=? AND source_lang=? AND target_lang=? AND text_hash IN ({placeholders})",
                params,
            )
            rows = cur.fetchall()
            found_map = {h: tr for (h, tr) in rows}

            for t, h in missing:
                tr = found_map.get(h)
                if tr is None:
                    continue
                tr = tr or ""
                if _is_bad_cached_translation(tr):
                    bad_hashes.append(h)
                    continue
                out[t] = tr
                self._mem_put((provider, source_lang, target_lang, h), tr)

            if bad_hashes:
                try:
                    ph2 = ",".join(["?"] * len(bad_hashes))
                    cur.execute(
                        f"DELETE FROM translations WHERE provider=? AND source_lang=? AND target_lang=? AND text_hash IN ({ph2})",
                        [provider, source_lang, target_lang, *bad_hashes],
                    )
                    self._con.commit()
                except Exception:
                    pass

        return out
    def put(self, provider: str, source_lang: str, target_lang: str, src_text: str, translated_text: str) -> None:
        src_text = src_text or ""
        translated_text = translated_text or ""
        h = stable_hash(src_text)
        key = (provider, source_lang, target_lang, h)

        self._mem_put(key, translated_text)

        with self._lock:
            self._con.execute(
                """
                INSERT OR REPLACE INTO translations
                (provider, source_lang, target_lang, text_hash, src_text, translated_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (provider, source_lang, target_lang, h, src_text, translated_text),
            )
            self._writes_since_commit += 1
            if self._writes_since_commit >= self._commit_every:
                self._con.commit()
                self._writes_since_commit = 0

    def put_many(
        self,
        provider: str,
        source_lang: str,
        target_lang: str,
        pairs: Iterable[Tuple[str, str]],
    ) -> None:
        """Insere várias traduções de uma vez (mais eficiente)."""
        rows = []
        for src_text, translated_text in pairs:
            src_text = (src_text or "").strip()
            translated_text = (translated_text or "").strip()
            if not src_text:
                continue
            h = stable_hash(src_text)
            rows.append((provider, source_lang, target_lang, h, src_text, translated_text))
            self._mem_put((provider, source_lang, target_lang, h), translated_text)

        if not rows:
            return

        with self._lock:
            self._con.executemany(
                """
                INSERT OR REPLACE INTO translations
                (provider, source_lang, target_lang, text_hash, src_text, translated_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._writes_since_commit += len(rows)
            if self._writes_since_commit >= self._commit_every:
                self._con.commit()
                self._writes_since_commit = 0
