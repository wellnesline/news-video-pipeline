"""synthetic stub: minimal .env loader (not the original implementation).

The pipeline modules put ``<project>/src`` on sys.path and import this module from it.
This repo root corresponds to that ``src/`` directory.
"""
from __future__ import annotations
import os
from pathlib import Path


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Load KEY=VALUE lines from ``path`` (default: ./.env) into os.environ without overriding."""
    p = Path(path) if path else Path.cwd() / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
