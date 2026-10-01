"""ニュースパイプライン 共通データクラス。

NewsCut  — 1配信回(date + cut_id + topics リスト)
Topic    — 1トピック(Remotion scenes + テロップ情報)
Line     — 1セリフ行(script.json の lines 要素)

制約: max_topics=5 を超えると ValueError。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

MAX_TOPICS = 5


@dataclass
class Line:
    id: str
    topic: str
    speaker: str
    expression: str
    text: str
    highlight: str = ""
    start_sec: float = 0.0
    duration_sec: float = 0.0


@dataclass
class Topic:
    id: str
    telop_cat: str
    telop_segs: list[dict]  # [{"text": str, "color": str}]
    scenes: list[dict]      # template-based scene specs
    source_urls: list[str] = field(default_factory=list)


@dataclass
class NewsCut:
    date: date
    cut_id: str
    channel: str
    topics: list[Topic]
    intro: dict
    outro: dict
    lines: list[Line]
    duration_sec: float = 0.0
    news_config: dict = field(default_factory=dict)

    def __post_init__(self):
        if len(self.topics) > MAX_TOPICS:
            raise ValueError(f"max_topics={MAX_TOPICS} を超えています: {len(self.topics)}")
