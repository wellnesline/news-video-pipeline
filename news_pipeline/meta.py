"""ニュースパイプライン — アップロード用メタデータ自動生成。

LLM 不要(テンプレート+トピックデータから組み立て)。
タイトル・タグ・概要欄のテンプレートは設定(JSON)で渡す。設定ファイルのパスは
環境変数 ``NEWS_META_CONFIG`` で指定する。未指定なら下の汎用既定値を使う。

設定 JSON の例 (synthetic sample):
  {
    "title_template": "{title}",
    "title_joiner": " / ",
    "title_more": "",
    "title_pad": "",
    "title_min_chars": 0,
    "title_max_chars": 100,
    "common_tags": ["tag1", "tag2"],
    "cut_tags": {"cut_a": ["tagA"]},
    "description_header": "Summary",
    "description_footer": ["(footer text)"]
  }

タイトルの書式はすべて設定値で、既定は ``{title}`` だけ(汎用):
  - ``title_template``: ``{title}`` に先頭トピックの要約を ``title_joiner`` でつないだ本文が入る。
    ``{month}`` ``{day}`` ``{other}``(本文に載らなかったトピック数)も使える
  - ``title_more``: 3 件以上あるときに本文へ足す文(``{other}`` 使用可)
  - ``title_min_chars`` / ``title_max_chars``: 文字数の範囲。超えたら要約を減らし、足りなければ ``title_pad`` を足す
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from news_pipeline.base import NewsCut, Topic  # noqa: E402

_DEFAULT_CONFIG: dict = {
    # 中立なサンプル値。実運用の文言・書式は NEWS_META_CONFIG の JSON で渡す
    "title_template": "{title}",
    "title_joiner": " / ",
    "title_more": "",
    "title_pad": "",
    "title_min_chars": 0,
    "title_max_chars": 100,
    "common_tags": ["tag1", "tag2"],
    "cut_tags": {},
    "description_header": "Summary",
    "description_footer": ["(footer text)"],
}


def _config() -> dict:
    """設定 JSON(環境変数 NEWS_META_CONFIG)を既定値へマージして返す。"""
    cfg = dict(_DEFAULT_CONFIG)
    path = os.environ.get("NEWS_META_CONFIG", "")
    if path and Path(path).exists():
        try:
            cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


def _render_title(news_cut: NewsCut, summaries: list[str], other: int) -> str:
    cfg = _config()
    body = cfg["title_joiner"].join(summaries) if summaries else "注目トピック"
    if other > 0 and cfg["title_more"]:
        body += cfg["title_more"].format(other=other)
    d = news_cut.date
    return cfg["title_template"].format(title=body, month=d.month, day=d.day, other=other)


def gen_title(news_cut: NewsCut, cut_id: str) -> str:
    """タイトル文字列を生成する(書式と文字数の範囲は設定値)。

    Args:
        news_cut: NewsCut オブジェクト
        cut_id:   カット識別子

    Returns:
        設定した範囲に収めたタイトル文字列
    """
    topics = news_cut.topics
    n = len(topics)
    shown = [_topic_summary(topic) for topic in topics[:2]]
    title = _render_title(news_cut, shown, max(0, n - len(shown)))
    return _adjust_title_length(news_cut, title, topics)

def gen_description(news_cut: NewsCut, cut_id: str) -> str:
    """概要欄テキストを生成する。

    各トピックを箇条書き + ハッシュタグ + 注記。

    Args:
        news_cut: NewsCut オブジェクト
        cut_id:   カット識別子

    Returns:
        概要欄テキスト(改行含む)
    """
    cfg = _config()
    lines = [cfg["description_header"], ""]

    # トピック箇条書き
    nums = "①②③④⑤"
    for i, topic in enumerate(news_cut.topics[:5]):
        num = nums[i] if i < len(nums) else f"{i+1}."
        segs = topic.telop_segs or []
        seg_texts = " ".join(
            s.get("text", "") for s in segs if s.get("text", "")
        )
        summary = _topic_summary(topic)
        if seg_texts:
            lines.append(f"{num} {seg_texts}")
        else:
            lines.append(f"{num} {summary}")

    lines.append("")

    # ハッシュタグ
    tags = list(cfg["common_tags"]) + list(cfg["cut_tags"].get(cut_id, []))
    lines.append(" ".join(f"#{t}" for t in tags))
    lines.append("")

    lines.append("──────────")
    lines.extend(cfg["description_footer"])

    return "\n".join(lines)


def gen_tags(news_cut: NewsCut, cut_id: str) -> list[str]:
    """タグ配列を生成する(最大 20 個)。

    Args:
        news_cut: NewsCut オブジェクト
        cut_id:   カット識別子

    Returns:
        タグ文字列のリスト
    """
    cfg = _config()
    tags = list(cfg["common_tags"])
    tags.extend(cfg["cut_tags"].get(cut_id, []))

    # トピック語から抽出
    seen: set[str] = set(tags)
    for topic in news_cut.topics:
        for seg in topic.telop_segs or []:
            t = seg.get("text", "").strip()
            if t and t not in seen and not _is_symbol_only(t) and len(t) >= 2:
                tags.append(t)
                seen.add(t)

    return tags[:20]


def _topic_summary(topic: Topic) -> str:
    """トピックの短い要約テキストを生成。

    telop_segs の先頭の有効なセグメントを優先して使う。
    """
    segs = topic.telop_segs or []
    # text が 1字以上・記号のみでないセグメントを収集
    candidates = [
        s.get("text", "") for s in segs
        if s.get("text") and not _is_symbol_only(s["text"])
    ]
    if candidates:
        # 先頭から最大 12 字
        text = candidates[0]
        if len(text) > 12:
            text = text[:12]
        return text

    # fallback: telop_cat
    cat = topic.telop_cat or ""
    if len(cat) > 15:
        cat = cat[:15]
    return cat or "注目トピック"


def _is_symbol_only(s: str) -> bool:
    """記号・数字・%のみの文字列なら True。"""
    return all(c in "0123456789.,+-% " for c in s)


def _adjust_title_length(news_cut: NewsCut, title: str, topics: list[Topic]) -> str:
    """設定した文字数の範囲に収まるようタイトルを調整。

    上限超え → 要約を減らして短縮。下限未満 → title_pad を追記。
    """
    cfg = _config()
    low, high = int(cfg["title_min_chars"]), int(cfg["title_max_chars"])
    n = len(topics)
    if len(title) > high and n >= 2:
        first = _topic_summary(topics[0])
        for shown in ([first], []):
            candidate = _render_title(news_cut, shown, n - len(shown))
            if len(candidate) <= high:
                return candidate
    if len(title) < low:
        candidate = title + cfg["title_pad"]
        if low <= len(candidate) <= high:
            return candidate
    return title