"""synthetic stub: channel configuration lookup.

Returns an in-memory sample config. Real projects load ``channels/<name>/channel.yaml``.
Shape used by the pipeline:
  {"characters": {role: {"bubble_color": {"border": "#rrggbb", "highlight": "#rrggbb",
                                          "tail_dir": "left"|"right"}}},
   "bubble": {"max_lines": int},
   "content_types": [{"cuts": [{"id": str}], "bgm": {"volume": float, "path": str}}]}
"""
from __future__ import annotations


def channel_config(channel: str) -> dict:
    return {"characters": {}, "bubble": {}, "content_types": []}
