from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import os
import yaml
from dotenv import load_dotenv


def deep_update(d: Dict[str, Any], u: Dict[str, Any]) -> Dict[str, Any]:
    """Atualiza dict recursivamente (sem mutar o original)."""
    out = dict(d or {})
    for k, v in (u or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_update(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config_path: Path) -> Dict[str, Any]:
    """Carrega config.yaml (ou outro) em um dict."""
    if not config_path.exists():
        return {}
    raw = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config inválida (esperado dict YAML): {config_path}")
    return data


def load_dotenv_if_present(project_root: Path) -> None:
    """Carrega .env se existir (não falha se não existir)."""
    env_path = project_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def get_env_or(cfg: Dict[str, Any], key: str, default: Optional[str] = None) -> Optional[str]:
    """Busca em env primeiro, senão no cfg."""
    v = os.getenv(key)
    if v is not None and v != "":
        return v
    return default
