"""Phase 2: Stooq CSV クライアント。

Stooq の公開 CSV エンドポイントから株価・指数データを取得する。
実行時に外部ネットワークアクセスが発生するため、テスト時は mock を利用すること。
利用にあたっては Stooq の利用条件に従い、取得頻度を抑えること(自動で大量に叩かない)。

Ticker 規則:
  US 株:  <ticker>.us  (例: xxx.us)
  US 指数: ^spx / ^ndq / ^dji / ^vix
  US 商品: cl.f(WTI), gc.f(金)
  JP 株:  <4桁>.jp (例: 1234.jp)
  指数: ^<code> 形式 (例: ^xxx)
"""
from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.request
from typing import Optional

STOOQ_CSV_URL = "https://stooq.com/q/l/?s={ticker}&f=sd2t2ohlcv&h&e=csv"
_REQUEST_TIMEOUT = 10  # seconds


def fetch_quote(ticker: str) -> Optional[dict]:
    """Stooq CSV エンドポイントから最新の Quote を取得する。

    Args:
        ticker: Stooq 形式のティッカー (例: "xxx.us", "^spx", "1234.jp")

    Returns:
        {"symbol": ..., "date": ..., "close": float, "open": float,
         "high": float, "low": float, "volume": int}
        またはエラー時 None
    """
    url = STOOQ_CSV_URL.format(ticker=ticker.lower())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "news-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(f"  [stooq] URLError for {ticker}: {exc}")
        return None
    except Exception as exc:
        print(f"  [stooq] Error fetching {ticker}: {exc}")
        return None

    rows = list(csv.DictReader(io.StringIO(raw.strip())))
    if not rows:
        print(f"  [stooq] Empty response for {ticker}")
        return None

    row = rows[0]
    # Stooq が「N/D」などを返す場合は Close が空になる
    close_str = row.get("Close", "").strip()
    if not close_str or close_str.upper() in ("N/D", "N/A", ""):
        print(f"  [stooq] No valid Close for {ticker}: {row}")
        return None

    try:
        return {
            "symbol": row.get("Symbol", ticker).strip(),
            "date": row.get("Date", "").strip(),
            "open": float(row.get("Open", 0) or 0),
            "high": float(row.get("High", 0) or 0),
            "low": float(row.get("Low", 0) or 0),
            "close": float(close_str),
            "volume": int(float(row.get("Volume", 0) or 0)),
        }
    except (ValueError, KeyError) as exc:
        print(f"  [stooq] Parse error for {ticker}: {exc} / row={row}")
        return None


def url_to_ticker(url: str) -> Optional[str]:
    """Yahoo Finance URL を Stooq ticker に変換する。

    対応 URL:
      https://finance.yahoo.com/quote/XXXX       → xxxx.us
      https://finance.yahoo.co.jp/quote/1234.T  → 1234.jp
      https://stooq.com/q/l/?s=^spx&...          → ^spx (そのまま)

    Args:
        url: Yahoo Finance または Stooq の URL 文字列

    Returns:
        Stooq ticker 文字列。変換できない場合は None。
    """
    # Stooq URL から s= パラメータ抽出
    m = re.search(r"stooq\.com.*[?&]s=([^&]+)", url, re.IGNORECASE)
    if m:
        return m.group(1)

    # Yahoo Finance US: finance.yahoo.com/quote/TICKER
    m = re.search(r"finance\.yahoo\.com/quote/([A-Za-z0-9\^=\-]+)", url)
    if m:
        ticker_raw = m.group(1).upper()
        # 指数はそのまま
        if ticker_raw.startswith("^"):
            return ticker_raw.lower()
        # 先物: CL=F → cl.f, GC=F → gc.f
        if ticker_raw.endswith("=F"):
            return f"{ticker_raw[:-2].lower()}.f"
        return f"{ticker_raw.lower()}.us"

    # Yahoo Finance JP: finance.yahoo.co.jp/quote/1234.T or 1234
    # (?!\d) で 4桁ピッタリ縛り。5桁以上の数字コード(例: 6桁の指数コード)は None を返す。
    m = re.search(r"finance\.yahoo\.co\.jp/quote/(\d{4})(?!\d)", url)
    if m:
        return f"{m.group(1)}.jp"

    return None


def compare_value(
    news_value: float,
    stooq_value: float,
    mode: str = "price",
) -> dict:
    """news_config の数値と Stooq Close を比較して PASS/WARN/FAIL を返す。

    Args:
        news_value:   scout が記録した数値
        stooq_value:  Stooq Close
        mode:         "price"(株価・指数) または "rate"(騰落率%)

    Returns:
        {"result": "PASS"|"WARN"|"FAIL", "diff_pct": float, "diff_abs": float}
    """
    if stooq_value == 0:
        return {"result": "WARN", "diff_pct": 0.0, "diff_abs": abs(news_value)}

    diff_abs = abs(news_value - stooq_value)
    diff_pct = diff_abs / abs(stooq_value) * 100

    if mode == "rate":
        # 騰落率: 絶対誤差基準
        if diff_abs <= 0.5:
            result = "PASS"
        elif diff_abs <= 1.5:
            result = "WARN"
        else:
            result = "FAIL"
    else:
        # 株価・指数: %誤差基準
        if diff_pct <= 2.0:
            result = "PASS"
        elif diff_pct <= 5.0:
            result = "WARN"
        else:
            result = "FAIL"

    return {"result": result, "diff_pct": round(diff_pct, 4), "diff_abs": round(diff_abs, 4)}
