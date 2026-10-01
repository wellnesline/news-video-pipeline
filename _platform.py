"""synthetic stub: environment helpers (fonts / H.264 encoder).

Sample implementation only. Fonts are given by environment variables (no OS-specific default
paths are bundled): set NEWS_FONT_REGULAR / NEWS_FONT_BOLD to a CJK-capable font file.
"""
from __future__ import annotations
import os
import platform
from pathlib import Path

_FONT_ENV = {"regular": "NEWS_FONT_REGULAR", "bold": "NEWS_FONT_BOLD"}


def resolve_font(weight: str = "regular") -> str:
    name = _FONT_ENV.get(weight, _FONT_ENV["regular"])
    cand = os.environ.get(name, "")
    if cand and Path(cand).exists():
        return cand
    raise FileNotFoundError(f"font file not found; set {name} to a CJK-capable font file")


def hw_video_encoder() -> str:
    """Return an H.264 encoder name. Software libx264 everywhere (portable default)."""
    return "libx264"
