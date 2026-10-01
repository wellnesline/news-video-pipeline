"""channel-xx ニュースパイプライン 構造的 fact-check モジュール。

構造的 fact-check(URL 存在チェック / 形式チェック)を機械的に実施する。
実 WebFetch による数値照合は news-fact-checker agent が担当する。

orchestrator から呼ばれる2つの公開関数:
  run_structural_fact_check(work_dir)  — URL 形式・個数チェック
  run_numeric_fact_check(work_dir)     — Stooq CSV 数値照合 (Phase 2)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── PATH 設定 ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


# ─────────────────────────────────────────────────────────────────────────────
# 構造的 fact-check (Phase 0 / 既存)
# ─────────────────────────────────────────────────────────────────────────────

def run_structural_fact_check(work_dir: Path) -> dict:
    """news_config.json の source_urls について構造的検査を実施する。

    WebFetch は行わない。URL の形式・個数チェックのみ。
    実数値照合は news-fact-checker agent が担当する。

    Args:
        work_dir: build_news() の作業ディレクトリ
                  (channels/<ch>/videos/news/<date>/<cut_id>/)

    Returns:
        {
            "has_failures": bool,
            "topics": int,
            "topics_ok": int,
            "urls_checked": int,
            "details": list[{"code": str, "message": str}],
            "stats": {"topics": int, "urls_checked": int},
        }
    """
    nc_path = work_dir / "news_config.json"
    details: list[dict] = []

    if not nc_path.exists():
        return {
            "has_failures": True,
            "topics": 0,
            "topics_ok": 0,
            "urls_checked": 0,
            "details": [{"code": "FC", "message": f"news_config.json が見つかりません: {nc_path}"}],
            "stats": {"topics": 0, "urls_checked": 0},
        }

    try:
        nc = json.loads(nc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "has_failures": True,
            "topics": 0,
            "topics_ok": 0,
            "urls_checked": 0,
            "details": [{"code": "FC", "message": f"news_config.json JSON パースエラー: {exc}"}],
            "stats": {"topics": 0, "urls_checked": 0},
        }

    topics: list[dict] = nc.get("topics", [])
    topics_ok = 0
    urls_checked = 0

    for t in topics:
        topic_id = t.get("id", "?")
        source_urls = t.get("source_urls")
        if not source_urls or len(source_urls) < 2:
            count = len(source_urls) if source_urls else 0
            details.append({
                "code": "A14",
                "message": f"[A14] topic={topic_id}: source_urls={count}本 (最低 2 必要)",
            })
            continue

        # 型チェック
        if not all(isinstance(u, str) for u in source_urls):
            details.append({
                "code": "A14",
                "message": f"[A14] topic={topic_id}: source_urls に文字列以外が含まれています: {source_urls}",
            })
            continue

        # https:// チェック
        bad_urls = [u for u in source_urls if not u.startswith("https://")]
        if bad_urls:
            details.append({
                "code": "A14",
                "message": f"[A14] topic={topic_id}: URL が https:// で始まらない: {bad_urls}",
            })
            continue

        urls_checked += len(source_urls)
        topics_ok += 1

    return {
        "has_failures": len(details) > 0,
        "topics": len(topics),
        "topics_ok": topics_ok,
        "urls_checked": urls_checked,
        "details": details,
        "stats": {"topics": len(topics), "urls_checked": urls_checked},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: 数値 fact-check (Stooq CSV)
# ─────────────────────────────────────────────────────────────────────────────

# value_main に含まれる数値を抽出する正規表現
_NUMERIC_EXTRACT_RE = re.compile(r"[+\-]?[\d,]+(?:\.\d+)?")

_NUMERIC_TEMPLATES = {"sparkline", "donut", "comparebars"}

# 騰落率と判定するキーワード
_RATE_KEYWORDS_RE = re.compile(r"%|前日比|変化率|騰落|上昇率|下落率")


def _extract_numeric(value_str: str) -> float | None:
    """value_main 文字列から数値を抽出する。カンマ区切りを除去。"""
    if not value_str:
        return None
    m = _NUMERIC_EXTRACT_RE.search(value_str)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def run_numeric_fact_check(work_dir: Path, channel: str = "channel-xx") -> dict:
    """Stooq CSV を使って news_config.json の数値を照合する。

    Args:
        work_dir: ビルド作業ディレクトリ
        channel:  チャンネル名(現在 channel-xx のみ対応)

    Returns:
        {
            "overall": "PASS"|"WARN"|"FAIL",
            "has_failures": bool,
            "checks": list[dict],   # 各照合結果
            "report_path": Path | None,   # fact_check_report.md の書き出し先
            "skipped": bool,
            "skip_reason": str | None,
        }
    """
    nc_path = work_dir / "news_config.json"
    report_path = work_dir / "fact_check_report.md"

    # Stooq クライアント import (ネットワーク依存なので遅延 import)
    try:
        from news_pipeline.sources.stooq import fetch_quote, url_to_ticker, compare_value
    except ImportError as exc:
        return _skip_result(f"stooq モジュール import 失敗: {exc}")

    if not nc_path.exists():
        return _skip_result(f"news_config.json が見つかりません: {nc_path}")

    try:
        nc = json.loads(nc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _skip_result(f"news_config.json パースエラー: {exc}")

    topics: list[dict] = nc.get("topics", [])
    checks: list[dict] = []
    connection_errors = 0

    for topic in topics:
        topic_id = topic.get("id", "?")
        source_urls: list[str] = topic.get("source_urls", [])
        scenes: list[dict] = topic.get("scenes", [])

        # source_urls から ticker を生成
        tickers: list[str] = []
        for url in source_urls:
            t = url_to_ticker(url)
            if t:
                tickers.append(t)

        if not tickers:
            # ticker 変換できない topic はスキップ
            checks.append({
                "topic_id": topic_id,
                "result": "SKIP",
                "reason": "source_urls から ticker を生成できません",
                "items": [],
            })
            continue

        # シーンを走査して数値チェック対象を特定
        topic_items: list[dict] = []
        for scene in scenes:
            tmpl = scene.get("template", "")
            if tmpl not in _NUMERIC_TEMPLATES:
                continue

            value_main = str(scene.get("value_main", ""))
            news_num = _extract_numeric(value_main)
            if news_num is None:
                continue

            # ticker ごとに Stooq 照合を試みる(最初に成功したものを使う)
            stooq_quote = None
            used_ticker = None
            for ticker in tickers:
                q = fetch_quote(ticker)
                if q is not None:
                    stooq_quote = q
                    used_ticker = ticker
                    break
                else:
                    connection_errors += 1

            if stooq_quote is None:
                topic_items.append({
                    "scene_template": tmpl,
                    "news_value": news_num,
                    "stooq_close": None,
                    "ticker": tickers[0] if tickers else "?",
                    "result": "WARN",
                    "reason": "Stooq から数値取得失敗、検証スキップ",
                })
                continue

            stooq_close = stooq_quote["close"]

            # 騰落率か株価かを判定(value_unit / value_label / unit_default 全てをチェック)
            label_parts = " ".join([
                str(scene.get("value_label", "") or ""),
                str(scene.get("value_unit", "") or ""),
                str(scene.get("unit_default", "") or ""),
            ])
            is_rate = bool(_RATE_KEYWORDS_RE.search(label_parts))
            # donut は構造的にシェア/比率を表すので常に rate
            if tmpl == "donut":
                is_rate = True

            if is_rate:
                # Stooq の close は絶対価格なので、rate(%)との照合は意味的に不可
                # → SKIP 扱い(false positive を排除)
                topic_items.append({
                    "scene_template": tmpl,
                    "news_value": news_num,
                    "stooq_close": stooq_close,
                    "ticker": used_ticker,
                    "mode": "rate",
                    "result": "SKIP",
                    "reason": "rate(%)軸はStooq close(絶対価格)と比較不可、SKIP",
                })
                continue

            cmp = compare_value(news_num, stooq_close, mode="price")
            topic_items.append({
                "scene_template": tmpl,
                "news_value": news_num,
                "stooq_close": stooq_close,
                "ticker": used_ticker,
                "mode": "price",
                "diff_pct": cmp["diff_pct"],
                "diff_abs": cmp["diff_abs"],
                "result": cmp["result"],
                "reason": None,
            })

        if not topic_items:
            overall_topic = "SKIP"
        elif any(i["result"] == "FAIL" for i in topic_items):
            overall_topic = "FAIL"
        elif any(i["result"] == "WARN" for i in topic_items):
            overall_topic = "WARN"
        elif all(i["result"] == "SKIP" for i in topic_items):
            overall_topic = "SKIP"
        else:
            overall_topic = "PASS"

        checks.append({
            "topic_id": topic_id,
            "result": overall_topic,
            "reason": None,
            "items": topic_items,
        })

    # 全体判定
    if connection_errors > 0 and all(
        c["result"] in ("SKIP", "WARN") for c in checks if c["result"] != "SKIP"
    ):
        overall = "WARN"
    elif any(c["result"] == "FAIL" for c in checks):
        overall = "FAIL"
    elif any(c["result"] == "WARN" for c in checks):
        overall = "WARN"
    else:
        overall = "PASS"

    has_failures = overall == "FAIL"

    # レポート書き出し
    _write_numeric_report(report_path, overall, checks, connection_errors)

    return {
        "overall": overall,
        "has_failures": has_failures,
        "checks": checks,
        "report_path": report_path,
        "skipped": False,
        "skip_reason": None,
    }


def _skip_result(reason: str) -> dict:
    """スキップ時の戻り値を生成する。"""
    print(f"  [fact_check] numeric check SKIP: {reason}")
    return {
        "overall": "WARN",
        "has_failures": False,
        "checks": [],
        "report_path": None,
        "skipped": True,
        "skip_reason": reason,
    }


def _write_numeric_report(
    report_path: Path,
    overall: str,
    checks: list[dict],
    connection_errors: int,
) -> None:
    """fact_check_report.md を書き出す。"""
    pass_count = sum(1 for c in checks for i in c.get("items", []) if i.get("result") == "PASS")
    warn_count = sum(1 for c in checks for i in c.get("items", []) if i.get("result") == "WARN")
    fail_count = sum(1 for c in checks for i in c.get("items", []) if i.get("result") == "FAIL")
    skip_count = sum(1 for c in checks for i in c.get("items", []) if i.get("result") == "SKIP")

    lines: list[str] = [
        "# 数値 fact-check レポート",
        "",
        f"## 総合判定: {overall}",
        "",
        "## Topic 別",
        "",
    ]

    for chk in checks:
        topic_id = chk["topic_id"]
        topic_result = chk["result"]
        lines.append(f"### {topic_id} ({topic_result})")
        if chk.get("reason"):
            lines.append(f"- {chk['reason']}")
        for item in chk.get("items", []):
            icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "SKIP": "⏭"}.get(
                item["result"], "?"
            )
            if item["result"] == "SKIP":
                lines.append(
                    f"- {icon} {item['scene_template']}: {item.get('reason', 'SKIP')}"
                )
            elif item.get("stooq_close") is not None and "diff_pct" in item:
                lines.append(
                    f"- {icon} {item['scene_template']}: "
                    f"news_config {item['news_value']} vs Stooq {item['stooq_close']}"
                    f"(誤差 {item['diff_pct']:.3f}%、{item['result']})"
                )
            else:
                lines.append(
                    f"- {icon} {item['scene_template']}: {item.get('reason', 'チェックスキップ')}"
                )
        lines.append("")

    if connection_errors > 0:
        lines.append(f"> Stooq 接続エラー: {connection_errors}件 — 該当 ticker は WARN 扱い")
        lines.append("")

    lines.append("## サマリ")
    lines.append(f"- PASS: {pass_count}件")
    lines.append(f"- WARN: {warn_count}件")
    lines.append(f"- FAIL: {fail_count}件")
    lines.append(f"- SKIP: {skip_count}件 (rate軸など比較不可)")
    lines.append("")

    try:
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  [fact_check] numeric report: {report_path}")
    except Exception as exc:
        print(f"  [fact_check] レポート書込失敗: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI エントリポイント
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="channel-xx 構造的 fact-check — source_urls の存在・形式チェック"
    )
    ap.add_argument(
        "--work-dir",
        required=True,
        help="work_dir (channels/channel-xx/videos/news/<date>/<cut_id>/) の絶対 or 相対パス",
    )
    ap.add_argument(
        "--numeric",
        action="store_true",
        help="Phase 2 数値 fact-check (Stooq CSV) も実行する",
    )
    args = ap.parse_args()

    work_dir = Path(args.work_dir)
    if not work_dir.exists():
        print(f"[fact_check] ERROR: work-dir が存在しません: {work_dir}")
        sys.exit(1)

    result = run_structural_fact_check(work_dir)
    print(
        f"[fact_check] topics={result['topics']}"
        f" topics_ok={result['topics_ok']}"
        f" urls_checked={result['urls_checked']}"
    )
    if result["has_failures"]:
        print("[fact_check] 構造的 FAIL:")
        for d in result["details"]:
            print(f"  {d}")
        sys.exit(2)
    else:
        print("[fact_check] 構造チェック PASS")

    if args.numeric:
        num_result = run_numeric_fact_check(work_dir)
        print(f"[fact_check] numeric overall={num_result['overall']}")
        if num_result["has_failures"]:
            sys.exit(2)


if __name__ == "__main__":
    main()
