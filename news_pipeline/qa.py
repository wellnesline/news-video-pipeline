"""channel-xx ニュースパイプライン 構造 QA モジュール。

build_news() step 9 として自動実行される。
work_dir 内の news_config.json / script.json を読み込み、
14項目の構造検査(A1-A14)を実施して qa_report.md を書き出す。

CLI:
  python src/news_pipeline/qa.py --work-dir channels/channel-xx/videos/news/<date>/cut_a

exit code:
  0 — PASS または WARN(build 継続)
  1 — FAIL_BLOCKING(build 停止)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Literal

# ── TYPE ALIAS ───────────────────────────────────────────────────────────────
QASeverity = Literal["PASS", "WARN", "FAIL"]

# ── 正規表現定数 ──────────────────────────────────────────────────────────────
_NUMERIC_RE        = re.compile(r"^[+\-]?[\d,\.]+$")
_DUAL_NOTATION_RE  = re.compile(r"[A-Za-z][A-Za-z0-9&/]+[、,]\s*[ァ-ヶー]{3,}")
_EXCL_QUEST_RE     = re.compile(r"[！!][？?]|[？?][！!]")

_DISCLAIMER_WORDS  = ["自己責任", "投資推奨", "元本", "免責", "リスク管理"]


# ── データクラス ──────────────────────────────────────────────────────────────

@dataclass
class QAItem:
    code: str
    severity: QASeverity
    message: str
    details: list[str] = field(default_factory=list)

    def is_blocking(self) -> bool:
        return self.severity == "FAIL"


@dataclass
class QAReport:
    cut_id: str
    cut_date: _date
    work_dir: Path
    items: list[QAItem] = field(default_factory=list)

    def has_blocking_failures(self) -> bool:
        return any(item.is_blocking() for item in self.items)

    @property
    def failures(self) -> list[QAItem]:
        return [item for item in self.items if item.severity == "FAIL"]

    @property
    def warnings(self) -> list[QAItem]:
        return [item for item in self.items if item.severity == "WARN"]

    @property
    def passes(self) -> list[QAItem]:
        return [item for item in self.items if item.severity == "PASS"]

    def overall(self) -> str:
        if self.has_blocking_failures():
            return "FAIL_BLOCKING"
        if self.warnings:
            return "WARN(human review)"
        return "PASS"


# ── 検査実装 ──────────────────────────────────────────────────────────────────

import re as _re

_RATE_LABEL_RE = _re.compile(r"%\s*$")
_UNIT_TAIL_RE = _re.compile(r"([^\d\.\,\+\-\s]+)\s*$")


def _label_unit_tail(label: str) -> str:
    """value_label の末尾単位文字列を抽出(数字以外の最後の連続)。"""
    if not label:
        return ""
    m = _UNIT_TAIL_RE.search(label)
    return m.group(1).strip() if m else ""


def _check_comparebars_mode(topics: list[dict], items: list[QAItem]) -> None:
    """A5a-e: comparebars テンプレの compare_mode 整合性チェック。

    A5a: compare_mode 必須(absolute|rate)
    A5b: absolute モードで負値混入 → FAIL
    A5c: absolute モードで単位不一致(value_label 末尾) → WARN
    A5d: rate モードで value_label が % 終端でない → FAIL
    A5e: rate モードで value 値が誤って指数値などの巨大数 → WARN(>200想定)
    """
    a5a_fails: list[str] = []
    a5b_fails: list[str] = []
    a5c_warns: list[str] = []
    a5d_fails: list[str] = []
    a5e_warns: list[str] = []

    for t in topics:
        for scene in t.get("scenes", []):
            if scene.get("template") != "comparebars":
                continue
            mode = scene.get("compare_mode")
            tag = f"topic={t.get('id', '?')} scene={scene.get('title', '?')}"
            rows = scene.get("rows", [])
            if mode not in ("absolute", "rate"):
                a5a_fails.append(f"{tag}: compare_mode 未設定 or 不正({mode!r})。'absolute' または 'rate' 必須")
                continue
            values: list[float] = []
            for r in rows:
                try:
                    values.append(float(str(r.get("value", "")).replace(",", "")))
                except (ValueError, TypeError):
                    pass

            if mode == "absolute":
                if any(v < 0 for v in values):
                    a5b_fails.append(f"{tag}: absolute モードに負値が含まれます({values})。rate モードを検討してください")
                # 単位末尾の一致を確認
                tails = {_label_unit_tail(r.get("value_label", "")) for r in rows if r.get("value_label")}
                tails.discard("")
                if len(tails) > 1:
                    a5c_warns.append(f"{tag}: value_label の単位が不一致 {sorted(tails)}。同単位に揃えるか scene 分割を検討")
            elif mode == "rate":
                bad_labels = [r.get("value_label", "") for r in rows
                              if not _RATE_LABEL_RE.search(str(r.get("value_label", "")))]
                if bad_labels:
                    a5d_fails.append(f"{tag}: rate モードで value_label が % 終端でない: {bad_labels}")
                if any(abs(v) > 200 for v in values):
                    a5e_warns.append(f"{tag}: rate モードで |value|>200 検出({values})。指数値ではなく騰落率%(整数または小数)を入れてください")

    if a5a_fails:
        items.append(QAItem(code="A5a", severity="FAIL",
                            message="comparebars compare_mode 未設定/不正",
                            details=a5a_fails))
    else:
        items.append(QAItem(code="A5a", severity="PASS", message="compare_mode 設定 OK"))

    if a5b_fails:
        items.append(QAItem(code="A5b", severity="FAIL",
                            message="absolute モードに負値混入(rate モード推奨)",
                            details=a5b_fails))
    if a5c_warns:
        items.append(QAItem(code="A5c", severity="WARN",
                            message="absolute モードで value_label 単位不一致",
                            details=a5c_warns))
    if a5d_fails:
        items.append(QAItem(code="A5d", severity="FAIL",
                            message="rate モードで value_label が % 終端でない",
                            details=a5d_fails))
    if a5e_warns:
        items.append(QAItem(code="A5e", severity="WARN",
                            message="rate モードで |value|>200(指数値の混入疑い)",
                            details=a5e_warns))


# 区分ラベル・テロップ接頭辞の既定値(汎用)。実運用では news_config.json の "qa" で上書きする。
DEFAULT_MARKET_LABELS = {"primary": ("区分1",), "alt": ("区分2",)}
DEFAULT_TELOP_PREFIXES = ("PART 1", "PART 2", "PART 3", "PART 4")
DEFAULT_TELOP_LAST_PREFIX = "SUMMARY"

def _check_news_config(nc: dict) -> list[QAItem]:
    """news_config.json の構造検査(A1-A6, A13)。"""
    items: list[QAItem] = []

    # A1: intro.label_jp 必須。許容ラベルは設定 nc["qa"]["market_labels"] で渡す。
    qa_cfg = nc.get("qa", {}) or {}
    labels = qa_cfg.get("market_labels") or DEFAULT_MARKET_LABELS
    primary_labels = tuple(labels.get("primary", ()))
    alt_labels = tuple(labels.get("alt", ()))
    intro = nc.get("intro", {})
    label_jp = intro.get("label_jp", "")
    if not label_jp:
        items.append(QAItem(
            code="A1", severity="FAIL",
            message="intro.label_jp が未設定です。区分ラベルを設定してください。",
        ))
    elif label_jp not in primary_labels + alt_labels:
        items.append(QAItem(
            code="A1", severity="WARN",
            message=f"intro.label_jp={label_jp!r} は想定外。許容: {list(primary_labels + alt_labels)}",
        ))
    else:
        items.append(QAItem(code="A1", severity="PASS", message=f"label_jp = {label_jp!r}"))

    # A2: alt 区分のラベルなら theme="alt" 必須(配色の取り違え防止)
    theme = nc.get("theme", "")
    is_alt = label_jp in alt_labels
    if is_alt:
        if theme == "alt":
            items.append(QAItem(code="A2", severity="PASS", message="alt 区分 theme=alt OK"))
        else:
            items.append(QAItem(
                code="A2", severity="FAIL",
                message=f"alt 区分だが theme={theme!r}。トップレベルに 'theme': 'alt' を設定してください。",
            ))
    else:
        if theme == "alt":
            items.append(QAItem(
                code="A2", severity="WARN",
                message="primary 区分なのに theme='alt' になっています。意図的でなければ削除してください。",
            ))
        else:
            items.append(QAItem(code="A2", severity="PASS", message="primary 区分 theme 設定 OK"))
    # A3: topic.scenes >= 3
    topics = nc.get("topics", [])
    a3_warns: list[str] = []
    for t in topics:
        scenes = t.get("scenes", [])
        if len(scenes) < 3:
            a3_warns.append(f"topic={t.get('id', '?')}: scenes={len(scenes)} (最低3必要)")
    if a3_warns:
        items.append(QAItem(
            code="A3", severity="WARN",
            message="scenes が 3 未満のトピックがあります(同一画面が長時間継続します)。",
            details=a3_warns,
        ))
    else:
        items.append(QAItem(code="A3", severity="PASS", message="全 topic.scenes >= 3"))

    # A4: value_main は数値文字列
    a4_fails: list[str] = []
    for t in topics:
        for scene in t.get("scenes", []):
            tmpl = scene.get("template", "")
            if tmpl in ("sparkline", "donut", "comparebars"):
                vm = str(scene.get("value_main", ""))
                if vm and not _NUMERIC_RE.match(vm):
                    a4_fails.append(
                        f"topic={t.get('id', '?')} template={tmpl} value_main={vm!r} (NaN 化リスク)"
                    )
    if a4_fails:
        items.append(QAItem(
            code="A4", severity="FAIL",
            message="value_main に非数値文字列があります。数値・記号・小数のみ使用可。",
            details=a4_fails,
        ))
    else:
        items.append(QAItem(code="A4", severity="PASS", message="value_main 数値チェック OK"))

    # A5: comparebars に単位情報必須
    a5_fails: list[str] = []
    for t in topics:
        for scene in t.get("scenes", []):
            if scene.get("template") != "comparebars":
                continue
            has_unit = bool(scene.get("unit_default"))
            rows = scene.get("rows", [])
            rows_have_label = all(r.get("value_label") or r.get("label") for r in rows) if rows else False
            if not has_unit and not rows_have_label:
                a5_fails.append(
                    f"topic={t.get('id', '?')} scene={scene.get('title', '?')} "
                    f"row={len(rows)}: value_label/unit_default 共に未設定"
                )
    if a5_fails:
        items.append(QAItem(
            code="A5", severity="FAIL",
            message="comparebars に単位情報がありません。value_label(row個別) か unit_default(scene全体) を設定してください。",
            details=a5_fails,
        ))
    else:
        items.append(QAItem(code="A5", severity="PASS", message="comparebars 単位チェック OK"))

    # A5a-e: comparebars compare_mode 整合性チェック(新設)
    _check_comparebars_mode(topics, items)

    # A6: comparebars 0-100 正規化疑い
    a6_warns: list[str] = []
    for t in topics:
        for scene in t.get("scenes", []):
            if scene.get("template") != "comparebars":
                continue
            rows = scene.get("rows", [])
            values = []
            for r in rows:
                try:
                    values.append(float(str(r.get("value", "")).replace(",", "")))
                except ValueError:
                    pass
            if values and max(values) == 100 and min(values) >= 0:
                a6_warns.append(
                    f"topic={t.get('id', '?')}: max=100, min={min(values)}"
                    f" → 0-100 正規化の可能性あり。実数値を使用してください(バー長は自動正規化されます)。"
                )
    if a6_warns:
        items.append(QAItem(
            code="A6", severity="WARN",
            message="comparebars value が 0-100 正規化されている可能性があります。",
            details=a6_warns,
        ))
    else:
        items.append(QAItem(code="A6", severity="PASS", message="comparebars value 正規化チェック OK"))

    # A13: minimal テンプレート 文字長
    a13_fails: list[str] = []
    for t in topics:
        for scene in t.get("scenes", []):
            if scene.get("template") != "minimal":
                continue
            main_lines = scene.get("main_lines", [])
            for ml in main_lines:
                size = ml.get("size", "")
                text = ml.get("text", "")
                if size == "hero" and len(text) > 8:
                    a13_fails.append(f"topic={t.get('id', '?')} hero={text!r}({len(text)}字 > 8字)")
                elif size == "headingSm" and len(text) > 16:
                    a13_fails.append(f"topic={t.get('id', '?')} headingSm={text!r}({len(text)}字 > 16字)")
    if a13_fails:
        items.append(QAItem(
            code="A13", severity="FAIL",
            message="minimal テンプレートの文字長超過。hero<=8字 / headingSm<=16字。",
            details=a13_fails,
        ))
    else:
        items.append(QAItem(code="A13", severity="PASS", message="minimal 文字長 OK"))

    return items


def _check_telop_cat_format(nc: dict) -> list[QAItem]:
    """A17: telop_cat 命名規則チェック(WARN レベル)。

    接頭辞は設定 nc["qa"]["telop_prefixes"](先頭から順)と
    nc["qa"]["telop_last_prefix"](最終 topic)で渡す。
    未指定なら DEFAULT_TELOP_PREFIXES / DEFAULT_TELOP_LAST_PREFIX を使う。
    topics が少ない場合は該当する index のみ検査する。
    """
    items: list[QAItem] = []
    topics = nc.get("topics", [])
    if not topics:
        return items

    qa_cfg = nc.get("qa", {}) or {}
    prefixes = list(qa_cfg.get("telop_prefixes") or DEFAULT_TELOP_PREFIXES)
    last_prefix = qa_cfg.get("telop_last_prefix") or DEFAULT_TELOP_LAST_PREFIX

    a17_warns: list[str] = []
    _expected_prefix: dict[int, str] = dict(enumerate(prefixes))

    for i, t in enumerate(topics):
        topic_id = t.get("id", f"T{i+1}")
        cat = t.get("telop_cat", "")
        is_last = (i == len(topics) - 1)

        if is_last:
            # 最終 topic は last_prefix で始まるべき(ただし 1 topic だけの場合は除外)
            if len(topics) > 1 and not cat.startswith(last_prefix):
                a17_warns.append(
                    f"topic={topic_id}(最終): telop_cat={cat!r} が '{last_prefix}' で始まっていません"
                )
        elif i in _expected_prefix:
            prefix = _expected_prefix[i]
            if not cat.startswith(prefix):
                a17_warns.append(
                    f"topic={topic_id}(T{i+1}): telop_cat={cat!r} が '{prefix}' で始まっていません"
                )
    if a17_warns:
        items.append(QAItem(
            code="A17", severity="WARN",
            message="telop_cat 命名規則の逸脱(設定した接頭辞と不一致)。"
                    "セッション横断再現性のため規則に従ってください。",
            details=a17_warns,
        ))
    else:
        items.append(QAItem(code="A17", severity="PASS", message="telop_cat 命名規則 OK"))

    return items


def _check_source_urls(nc: dict) -> list[QAItem]:
    """A14: 各 topic に source_urls(最低 2 URL、https:// 形式)が必須。"""
    items: list[QAItem] = []
    topics = nc.get("topics", [])
    a14_fails: list[str] = []

    for t in topics:
        topic_id = t.get("id", "?")
        source_urls = t.get("source_urls")
        if not source_urls or len(source_urls) < 2:
            count = len(source_urls) if source_urls else 0
            a14_fails.append(f"topic={topic_id}: source_urls={count}本 (最低 2 必要)")
            continue
        bad = [u for u in source_urls if not u.startswith("https://")]
        if bad:
            a14_fails.append(f"topic={topic_id}: URL が https:// で始まらない: {bad}")

    if a14_fails:
        items.append(QAItem(
            code="A14", severity="FAIL",
            message="各 topic に source_urls(最低 2 URL、全て https://)が必要です。"
                    " scout の Step 2-3 で WebFetch を実施し、一次ソース URL を記録してください。",
            details=a14_fails,
        ))
    else:
        items.append(QAItem(code="A14", severity="PASS", message="source_urls チェック OK"))

    return items


def _check_script(s: dict) -> list[QAItem]:
    """script.json の構造検査(A7-A12)。"""
    items: list[QAItem] = []
    duration = float(s.get("duration_sec", 0.0))

    # A7: duration_sec < 180(>=175 は WARN)
    if duration >= 180.0:
        items.append(QAItem(
            code="A7", severity="FAIL",
            message=(
                f"duration_sec={duration}s が Shorts 3分上限(180s)を超えています。"
                " scout 行数を 28-34行に絞り 175s 未満に収めてください。"
            ),
        ))
    elif duration >= 175.0:
        items.append(QAItem(
            code="A7", severity="WARN",
            message=f"duration_sec={duration}s が推奨上限(175s)に近接しています。余裕を持たせてください。",
        ))
    else:
        items.append(QAItem(code="A7", severity="PASS", message=f"{duration}s < 175s OK"))

    lines = s.get("lines", [])

    # A8: line.text <= 70字
    a8_fails: list[str] = []
    for ln in lines:
        text = ln.get("text", "")
        if len(text) > 70:
            a8_fails.append(f"line={ln.get('id', '?')}: {len(text)}字 → {text[:20]}...")
    if a8_fails:
        items.append(QAItem(
            code="A8", severity="FAIL",
            message="70字超のセリフがあります。読み上げが長くなり尺超過の原因になります。",
            details=a8_fails,
        ))
    else:
        items.append(QAItem(code="A8", severity="PASS", message="全 line.text <= 70字 OK"))

    # A9: 「！？」「?!」併用なし
    a9_warns: list[str] = []
    for ln in lines:
        text = ln.get("text", "")
        if _EXCL_QUEST_RE.search(text):
            a9_warns.append(f"line={ln.get('id', '?')}: {text[:30]}")
    if a9_warns:
        items.append(QAItem(
            code="A9", severity="WARN",
            message="「！？」「?!」併用はVOICEVOX読み上げの事故原因になります。",
            details=a9_warns,
        ))
    else:
        items.append(QAItem(code="A9", severity="PASS", message="「！？」併用なし OK"))

    # A10: 英字+カナ併記なし
    a10_fails: list[str] = []
    for ln in lines:
        text = ln.get("text", "")
        m = _DUAL_NOTATION_RE.search(text)
        if m:
            a10_fails.append(f"line={ln.get('id', '?')}: match={m.group()!r} in {text[:40]!r}")
    if a10_fails:
        items.append(QAItem(
            code="A10", severity="FAIL",
            message="英字+カナ併記があります。VOICEVOX が同じ社名を2回読み上げる事故を引き起こします。"
                    " 英字 OR カナの片方だけを使用してください。",
            details=a10_fails,
        ))
    else:
        items.append(QAItem(code="A10", severity="PASS", message="英字+カナ併記なし OK"))

    # A11: highlight が text に存在
    a11_warns: list[str] = []
    for ln in lines:
        hi = ln.get("highlight", "")
        text = ln.get("text", "")
        if hi and hi not in text:
            a11_warns.append(f"line={ln.get('id', '?')}: highlight={hi!r} が text に含まれていません")
    if a11_warns:
        items.append(QAItem(
            code="A11", severity="WARN",
            message="highlight 文字列が text に存在しないケースがあります(字幕 UI で表示崩れの可能性)。",
            details=a11_warns,
        ))
    else:
        items.append(QAItem(code="A11", severity="PASS", message="highlight 整合 OK"))

    # A12: 投資免責語句なし
    a12_warns: list[str] = []
    for ln in lines:
        text = ln.get("text", "")
        found = [w for w in _DISCLAIMER_WORDS if w in text]
        if found:
            a12_warns.append(f"line={ln.get('id', '?')}: {', '.join(found)} を含む → {text[:40]!r}")
    if a12_warns:
        items.append(QAItem(
            code="A12", severity="WARN",
            message="セリフに投資免責語句が含まれています。免責文は meta.py が概要欄に自動付与するため不要です。",
            details=a12_warns,
        ))
    else:
        items.append(QAItem(code="A12", severity="PASS", message="投資免責語句なし OK"))

    return items


# 数値表現を含む line の正規表現
# - 単位付き数値(% 円 ドル 億 兆 ポイント pt)
# - 4桁以上の数字(日付 2026 等を除外するため、まず単位付きを優先)
_NUMERIC_LINE_RE = _re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|円|ドル|億|兆|ポイント|pt)")
_NUMERIC_TEMPLATES_FOR_LINE = {"comparebars", "sparkline", "donut", "barpulse"}


def _check_scene_line_sync(nc: dict, sc: dict) -> list[QAItem]:
    """A15a-c: lines[i].scene_idx と news_config の scene の整合性チェック。

    A15a: 全 line に scene_idx が必須(INTRO/OUTRO 含む) → FAIL
    A15b: topic 内の各 scene に最低 1 line 紐付くこと → WARN(scene が孤立)
    A15c: 数値を含む line(% / 円 / ドル / 3桁数字)は数値表現 scene
          (comparebars/sparkline/donut/barpulse)を指すべき → WARN
    """
    items: list[QAItem] = []
    lines = sc.get("lines", [])
    topics = nc.get("topics", [])
    topic_by_id = {t.get("id"): t for t in topics}

    a15a_fails: list[str] = []
    a15b_warns: list[str] = []
    a15c_warns: list[str] = []
    out_of_range_fails: list[str] = []

    # A15a: scene_idx 必須
    for ln in lines:
        if "scene_idx" not in ln:
            a15a_fails.append(f"line={ln.get('id', '?')} topic={ln.get('topic', '?')}: scene_idx 未設定")
            continue
        idx = ln.get("scene_idx")
        if not isinstance(idx, int):
            a15a_fails.append(f"line={ln.get('id', '?')}: scene_idx は整数のみ({idx!r})")
            continue
        # 範囲チェック(topic に scenes がある場合のみ)
        topic_id = ln.get("topic", "")
        topic = topic_by_id.get(topic_id)
        if topic is not None:
            n_scenes = len(topic.get("scenes", []))
            if n_scenes > 0 and not (0 <= idx < n_scenes):
                out_of_range_fails.append(
                    f"line={ln.get('id', '?')} topic={topic_id}: scene_idx={idx} 範囲外 (scenes 数={n_scenes})"
                )

    if a15a_fails:
        items.append(QAItem(code="A15a", severity="FAIL",
                            message="line.scene_idx 未設定 or 不正",
                            details=a15a_fails))
    elif out_of_range_fails:
        items.append(QAItem(code="A15a", severity="FAIL",
                            message="line.scene_idx が scenes 範囲外",
                            details=out_of_range_fails))
    else:
        items.append(QAItem(code="A15a", severity="PASS", message="line.scene_idx 整合 OK"))

    # A15b: scene カバレッジ — 各 topic の各 scene に最低 1 line
    for topic in topics:
        topic_id = topic.get("id", "?")
        n_scenes = len(topic.get("scenes", []))
        used = set()
        for ln in lines:
            if ln.get("topic") != topic_id:
                continue
            idx = ln.get("scene_idx")
            if isinstance(idx, int) and 0 <= idx < n_scenes:
                used.add(idx)
        missing = [i for i in range(n_scenes) if i not in used]
        if missing:
            a15b_warns.append(f"topic={topic_id}: scene_idx={missing} に紐付く line がない(scene 孤立)")

    if a15b_warns:
        items.append(QAItem(code="A15b", severity="WARN",
                            message="scene カバレッジ不足(孤立 scene)",
                            details=a15b_warns))

    # A15c: 数値 line → 数値表現 scene であるべき
    for ln in lines:
        text = ln.get("text", "")
        if not _NUMERIC_LINE_RE.search(text):
            continue
        topic_id = ln.get("topic", "")
        topic = topic_by_id.get(topic_id)
        if topic is None:
            continue
        idx = ln.get("scene_idx")
        if not isinstance(idx, int):
            continue
        scenes = topic.get("scenes", [])
        if not (0 <= idx < len(scenes)):
            continue
        target = scenes[idx]
        tmpl = target.get("template", "")
        # minimal でも main_lines の hero に数値があれば許容(+57% 等)
        is_numeric_minimal = False
        if tmpl == "minimal":
            for ml in target.get("main_lines", []):
                if ml.get("size") == "hero" and _NUMERIC_LINE_RE.search(str(ml.get("text", ""))):
                    is_numeric_minimal = True
                    break
        if tmpl not in _NUMERIC_TEMPLATES_FOR_LINE and not is_numeric_minimal:
            a15c_warns.append(
                f"line={ln.get('id', '?')} topic={topic_id} 数値含むが scene_idx={idx} は {tmpl}"
                f" → 数値表現テンプレ({sorted(_NUMERIC_TEMPLATES_FOR_LINE)})推奨"
            )

    if a15c_warns:
        items.append(QAItem(code="A15c", severity="WARN",
                            message="数値含む line が非数値 scene を指している",
                            details=a15c_warns))

    # A16: line ↔ scene 意味マッチ(銘柄・指標・期間)
    items.extend(_check_line_scene_semantic(nc, sc))

    return items


def _check_line_scene_semantic(nc: dict, sc: dict) -> list[QAItem]:
    """A16: line のテキストから抽出した key term が scene と空交差なら WARN。

    A16: 意味マッチ(A社を語る line が B社 scene を指している等)
    """
    from news_pipeline.qa_keyterms import extract_key_terms, scene_text_for_match, has_key_terms

    items: list[QAItem] = []
    lines = sc.get("lines", [])
    topic_by_id = {t.get("id"): t for t in nc.get("topics", [])}

    a16_warns: list[str] = []
    for ln in lines:
        text = ln.get("text", "")
        if not has_key_terms(text):
            continue
        topic_id = ln.get("topic", "")
        topic = topic_by_id.get(topic_id)
        if topic is None:
            continue
        idx = ln.get("scene_idx")
        if not isinstance(idx, int):
            continue
        scenes = topic.get("scenes", [])
        if not (0 <= idx < len(scenes)):
            continue
        scene = scenes[idx]

        line_terms = extract_key_terms(text)
        scene_terms = extract_key_terms(scene_text_for_match(scene))
        # 期間/銘柄/セクター系は厳格に交差を要求(数値だけ一致は無視)
        if not (line_terms & scene_terms):
            a16_warns.append(
                f"line={ln.get('id', '?')} topic={topic_id} scene_idx={idx}: "
                f"line terms={sorted(line_terms)} ∩ scene terms={sorted(scene_terms)} = ∅"
            )

    if a16_warns:
        items.append(QAItem(code="A16", severity="WARN",
                            message="line ↔ scene 意味ミスマッチ(銘柄・指標・期間が一致しない)",
                            details=a16_warns))
    else:
        items.append(QAItem(code="A16", severity="PASS", message="line ↔ scene 意味マッチ OK"))

    return items


def run_structural_qa(work_dir: Path) -> QAReport:
    """work_dir 内の JSON を検査して QAReport を返す。

    Args:
        work_dir: build_news() の作業ディレクトリ
                  (channels/<ch>/videos/news/<date>/<cut_id>/)

    Returns:
        QAReport — has_blocking_failures() で FAIL 判定可能
    """
    import datetime as _dt

    # cut_id / cut_date を work_dir から推測
    cut_id   = work_dir.name if work_dir.exists() else "unknown"
    cut_date = _dt.date.today()
    try:
        cut_date = _dt.date.fromisoformat(work_dir.parent.name)
    except ValueError:
        pass

    report = QAReport(cut_id=cut_id, cut_date=cut_date, work_dir=work_dir)

    # news_config.json
    nc_path = work_dir / "news_config.json"
    if not nc_path.exists():
        report.items.append(QAItem(
            code="A0", severity="FAIL",
            message=f"news_config.json が見つかりません: {nc_path}",
        ))
        return report

    try:
        nc = json.loads(nc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.items.append(QAItem(
            code="A0", severity="FAIL",
            message=f"news_config.json JSON パースエラー: {exc}",
        ))
        return report

    # script.json
    sc_path = work_dir / "script.json"
    if not sc_path.exists():
        report.items.append(QAItem(
            code="A0", severity="FAIL",
            message=f"script.json が見つかりません: {sc_path}",
        ))
        # news_config 検査だけ実施
        report.items.extend(_check_news_config(nc))
        report.items.extend(_check_source_urls(nc))
        return report

    try:
        sc = json.loads(sc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.items.append(QAItem(
            code="A0", severity="FAIL",
            message=f"script.json JSON パースエラー: {exc}",
        ))
        report.items.extend(_check_news_config(nc))
        report.items.extend(_check_source_urls(nc))
        return report

    # 全検査
    report.items.extend(_check_news_config(nc))
    report.items.extend(_check_source_urls(nc))
    report.items.extend(_check_telop_cat_format(nc))
    report.items.extend(_check_script(sc))
    report.items.extend(_check_scene_line_sync(nc, sc))

    return report


def write_qa_report(report: QAReport, work_dir: Path) -> Path:
    """QAReport を work_dir/qa_report.md に書き出して Path を返す。"""
    import datetime as _dt

    overall = report.overall()
    now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")

    lines: list[str] = [
        f"# news-qa レポート — channel-xx {report.cut_id}",
        f"生成: {now_str}",
        "",
        f"## 総合判定: {overall}",
        "",
    ]

    pass_count  = len(report.passes)
    warn_count  = len(report.warnings)
    fail_count  = len(report.failures)
    lines.append(f"## A. 構造検査({pass_count} PASS)")
    lines.append("")

    for item in report.items:
        if item.severity == "PASS":
            lines.append(f"- [OK] [{item.code}] {item.message}")
        elif item.severity == "WARN":
            lines.append(f"- [WARN] [{item.code}] {item.message}")
        else:
            lines.append(f"- [FAIL] [{item.code}] {item.message}")
        for detail in item.details:
            lines.append(f"  - {detail}")

    lines.append("")
    # 非PASS項目を詳細セクションで展開
    non_pass = [item for item in report.items if item.severity != "PASS"]
    if non_pass:
        lines.append("## 詳細所見")
        lines.append("")
        for item in non_pass:
            lines.append(f"### [{item.code}] {item.severity}: {item.message}")
            for detail in item.details:
                lines.append(f"- {detail}")
            lines.append("")
    else:
        lines.append("特記事項なし。")

    out = work_dir / "qa_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> None:
    """channel-xx ニュース QA — news_config.json / script.json の構造検査(A1-A14)"""
    ap = argparse.ArgumentParser(
        description="channel-xx ニュース QA — news_config.json / script.json の構造検査(A1-A14)"
    )
    ap.add_argument(
        "--work-dir",
        required=True,
        help="work_dir (channels/channel-xx/videos/news/<date>/<cut_id>/) の絶対 or 相対パス",
    )
    ap.add_argument(
        "--no-write",
        action="store_true",
        help="qa_report.md を書き出さない(標準出力のみ)",
    )
    args = ap.parse_args()

    work_dir = Path(args.work_dir)
    if not work_dir.exists():
        print(f"[qa] ERROR: work-dir が存在しません: {work_dir}")
        sys.exit(1)

    report = run_structural_qa(work_dir)

    if not args.no_write:
        report_path = write_qa_report(report, work_dir)
        print(f"[qa] report: {report_path}")

    overall = report.overall()
    print(
        f"[qa] {overall}: PASS={len(report.passes)}"
        f" WARN={len(report.warnings)} FAIL={len(report.failures)}"
    )

    if report.has_blocking_failures():
        print("[qa] ブロッキング問題が検出されました。qa_report.md を参照してください。")
        sys.exit(1)


if __name__ == "__main__":
    main()
