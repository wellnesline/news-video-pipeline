"""channel-xx ニュースパイプライン (Phase 1)。

モジュール構成:
  base.py   — データクラス(NewsCut / Topic / Line)
  synth.py  — VOICEVOX WAV 合成
  bubble.py — 吹き出し PNG 生成
  compose.py — ffmpeg + Remotion 合成
  build.py  — メインオーケストレータ
  variants/ — cut別ローダ (cut_b / cut_a)
"""
from news_pipeline.base import NewsCut, Topic, Line
from news_pipeline.build import build_news
from news_pipeline.qa import QAReport, run_structural_qa, write_qa_report

__all__ = [
    "NewsCut",
    "Topic",
    "Line",
    "build_news",
    "QAReport",
    "run_structural_qa",
    "write_qa_report",
]
