"""cut_b カット — scout agent 方式(移行)。

データ取得は scout agentが Web research で行い、
work_dir に news_config.json + script.json を書き出す。
本モジュールはその JSON を読み込んで NewsCut を返すだけ。

使い方:
  1. Claude Code で scout agent を起動
     → channels/channel-xx/videos/news/<date>/cut_b/ に JSON が生成される
  2. python scripts/build_news.py --cut cut_b --date <date>
     → load_news_cut() が JSON を読み込んで build を開始
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from news_pipeline.base import NewsCut, Topic, Line  # noqa: E402

CHANNEL = "channel-xx"
CUT_ID  = "cut_b"
_NEWS_ROOT = ROOT / "channels" / CHANNEL / "videos" / "news"


class SkipBuild(Exception):
    """最低スロット数を満たせない場合 or scout agent 出力が存在しない場合に raise する。
    build_news.py が exit 0 でスキップするために参照する。
    """


def load_news_cut(cut_date: date, work_dir: Path | None = None) -> NewsCut:
    """cut_b カット ローダ。

    work_dir に news_config.json + script.json が存在することを前提とする。
    存在しない場合は RuntimeError で friendly error を返す。

    work_dir が None の場合は規約パス(channels/.../cut_b/)を自動解決する。

    Args:
        cut_date: 配信日
        work_dir: scout agent の出力ディレクトリ。None で自動解決。
    """
    if work_dir is None:
        work_dir = _NEWS_ROOT / str(cut_date) / CUT_ID

    nc_path = work_dir / "news_config.json"
    sc_path = work_dir / "script.json"

    missing = [p for p in (nc_path, sc_path) if not p.exists()]
    if missing:
        raise RuntimeError(
            f"scout agent の出力が見つかりません: {[str(p) for p in missing]}\n"
            f"先に scout agent を起動して、\n"
            f"  channels/channel-xx/videos/news/{cut_date}/cut_b/\n"
            "に news_config.json + script.json を生成してください。"
        )

    nc = json.loads(nc_path.read_text(encoding="utf-8"))
    _validate_news_config(nc, nc_path)

    sc = json.loads(sc_path.read_text(encoding="utf-8"))
    _validate_script(sc, sc_path)

    topics_raw = nc.get("topics", [])
    if not topics_raw:
        raise RuntimeError(
            f"news_config.json に topics が空です: {nc_path}\n"
            "scout agent を再起動してください。"
        )

    topics = [
        Topic(
            id=t["id"],
            telop_cat=t.get("telop_cat", ""),
            telop_segs=t.get("telop_segs", []),
            scenes=t.get("scenes", []),
            source_urls=t.get("source_urls", []),
        )
        for t in topics_raw
    ]

    lines_raw = sc.get("lines", [])
    lines = [
        Line(
            id=ln["id"],
            topic=ln.get("topic", ""),
            speaker=ln["speaker"],
            expression=ln.get("expression", "normal"),
            text=ln["text"],
            highlight=ln.get("highlight", ""),
            start_sec=float(ln.get("start_sec", 0.0)),
            duration_sec=float(ln.get("duration_sec", 0.0)),
        )
        for ln in lines_raw
    ]

    return NewsCut(
        date=cut_date,
        cut_id=CUT_ID,
        channel=CHANNEL,
        topics=topics,
        intro=nc.get("intro", {}),
        outro=nc.get("outro", {}),
        lines=lines,
        duration_sec=float(sc.get("duration_sec", 0.0)),
        news_config=nc,
    )


def _validate_news_config(nc: dict, nc_path: Path) -> None:
    """必須キーが揃っているか確認。欠損なら RuntimeError。"""
    required = ["intro", "topics"]
    missing = [k for k in required if k not in nc]
    if missing:
        raise RuntimeError(
            f"news_config.json に必須キーが不足しています: {missing}\n"
            f"ファイル: {nc_path}\n"
            "scout agent を再起動して正しい形式で生成してください。"
        )
    for i, t in enumerate(nc.get("topics", [])):
        for key in ("id", "scenes"):
            if key not in t:
                raise RuntimeError(
                    f"news_config.json topics[{i}] に必須キー '{key}' がありません。\n"
                    f"ファイル: {nc_path}"
                )


def _validate_script(sc: dict, sc_path: Path) -> None:
    """script.json の必須キー確認。"""
    if "lines" not in sc:
        raise RuntimeError(
            f"script.json に 'lines' キーがありません: {sc_path}\n"
            "scout agent を再起動して正しい形式で生成してください。"
        )
    for i, ln in enumerate(sc.get("lines", [])):
        for key in ("id", "speaker", "text"):
            if key not in ln:
                raise RuntimeError(
                    f"script.json lines[{i}] に必須キー '{key}' がありません: {sc_path}"
                )
