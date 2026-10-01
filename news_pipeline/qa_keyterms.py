"""line ↔ scene 意味マッチ用 キーワード辞書(A16 用)。

scout が line で言及する固有名詞・指標・期間と、scene の title/main_lines/rows が
表現するキーワードを抽出する。両者の交差が空ならミスマッチ警告。

辞書は閉じた集合ではなく、ヒューリスティック的にカバレッジを広げる方針。
- ティッカー(英大文字3-5字)は正規表現で動的抽出
- 銘柄カナ(架空の例: アルファ商事)とティッカー(ALFA)を同義として正規化
"""
from __future__ import annotations

import re
from typing import Iterable

# 銘柄正規化マップ(カナ表記 ↔ ティッカー)。以下は架空の社名・ティッカーの例。
# 実運用では設定ファイルから差し替える前提。
TICKER_ALIASES: dict[str, str] = {
    "アルファ商事": "ALFA", "alfa trading": "ALFA",
    "ベータ電機": "BETA", "beta electric": "BETA",
    "ガンマ製作所": "GMMA", "gamma works": "GMMA",
}

# 指標・期間・部門の同義語クラスタ。同じクラスタに属する語は「同じ意味」として扱う。
SEMANTIC_CLUSTERS: list[set[str]] = [
    # 期間
    {"Q1", "第1四半期", "1Q"},
    {"Q2", "第2四半期", "2Q"},
    {"Q3", "第3四半期", "3Q"},
    {"Q4", "第4四半期", "4Q"},
    # 指標カテゴリ
    {"売上", "売上高", "総収入", "Revenue"},
    {"営業利益", "営業益", "Operating"},
    {"EPS", "1株利益"},
    {"株価", "終値"},
    {"時価総額", "Market Cap"},
    # 部門(架空の例。実運用では設定で渡す)
    {"部門A", "セグメントA"},
    {"部門B", "セグメントB"},
]
# ティッカー正規表現(2-5 文字の英大文字、単独語境界)
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
# 騰落率(%)
_PCT_RE = re.compile(r"[+\-]?\d+(?:\.\d+)?%")


def _normalize(term: str) -> str:
    """ティッカーは大文字・カナ表記はマップ経由で正規化。"""
    low = term.lower()
    if low in TICKER_ALIASES:
        return TICKER_ALIASES[low]
    if term.upper() in TICKER_ALIASES:
        return TICKER_ALIASES[term.upper()]
    if _TICKER_RE.fullmatch(term):
        return term.upper()
    return term


def extract_key_terms(text: str) -> set[str]:
    """テキストから銘柄・指標・期間・セクターのキーワードを抽出。

    抽出ルール:
    1. TICKER_ALIASES に登録されたカナ/英表記を検索
    2. 単独ティッカー(英大文字 2-5 文字、ただし一般語除外)を検索
    3. SEMANTIC_CLUSTERS の各語を検索 → クラスタ代表語に正規化
    """
    if not text:
        return set()
    terms: set[str] = set()
    text_low = text.lower()

    # 1. カナ/英表記から正規化
    for alias, ticker in TICKER_ALIASES.items():
        if alias.lower() in text_low:
            terms.add(ticker)

    # 2. ティッカー直接抽出(but 一般語フィルタ)
    common_words = {"AI", "EPS", "WTI", "GDP", "API", "OS", "PC"}  # 文脈次第なのでクラスタ側に任せる
    for m in _TICKER_RE.finditer(text):
        t = m.group(1)
        if t in common_words:
            continue
        if 2 <= len(t) <= 5:
            terms.add(t)

    # 3. SEMANTIC_CLUSTERS — クラスタ代表語(0番目の要素)で正規化
    for cluster in SEMANTIC_CLUSTERS:
        rep = sorted(cluster)[0]  # 安定化(辞書順)
        for word in cluster:
            if word.lower() in text_low:
                terms.add(f"sem:{rep}")
                break

    return terms


def has_key_terms(text: str) -> bool:
    """line に意味マッチ対象のキーワードがあるか(無ければ A16 適用外)。"""
    return bool(extract_key_terms(text))


def scene_text_for_match(scene: dict) -> str:
    """scene 全体から照合対象テキストを連結。"""
    parts: list[str] = []
    for k in ("title", "sub", "value_main_text", "value_main", "chip"):
        v = scene.get(k)
        if v:
            parts.append(str(v))
    for ml in scene.get("main_lines", []) or []:
        if ml.get("text"):
            parts.append(str(ml.get("text")))
    if ml_t := scene.get("tagline"):
        parts.append(str(ml_t))
    for r in scene.get("rows", []) or []:
        if r.get("label"):
            parts.append(str(r.get("label")))
        if r.get("value_label"):
            parts.append(str(r.get("value_label")))
    return " ".join(parts)
