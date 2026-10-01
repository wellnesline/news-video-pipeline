"""news_pipeline/escalation.py — news_pipeline 専用 _escalation.json 出力ヘルパ。

監査由来: rework_count hard cap (cross-turn 永続化) のため新設。
remotion_pipeline.escalation の news_pipeline 版で、書き出し先は
`channels/<ch>/videos/news/<date>/<cut_id>/_escalation.json`。

rework ループ防止ルール(最大2-3回)と hard cap=6 のポリシーを Python レベルで強制する。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Literal

EscalationGate = Literal[
    "rework_hard_cap",
    "fact_check_fail",
    "build_fail",
    "numeric_fact_check_fail",
]

NextAction = Literal[
    "human_review",
    "manual_edit",
    "skip_day",
]


def write_news_escalation(
    work_dir: Path,
    *,
    channel: str,
    cut_id: str,
    cut_date: str,
    gate: EscalationGate,
    attempts: int,
    rework_history: list[dict],
    hint: str,
    next_action: NextAction = "human_review",
    last_report_path: str | None = None,
) -> Path:
    """work_dir/_escalation.json を書き出す。 既存ファイルは上書き。

    Args:
        work_dir:        topic 作業ディレクトリ (channels/<ch>/videos/news/<date>/<cut_id>/)
        channel:         チャンネル名
        cut_id:          カット識別子 (cut_b / cut_a)
        cut_date:        ISO 日付文字列 (YYYY-MM-DD)
        gate:            escalation 発火元ゲート
        attempts:        累積 rework 回数
        rework_history:  rounds サマリ (orchestrator が _build_rework_history で生成)
        hint:            人間向け対応ヒント
        next_action:     次に人間が取るべきアクション
        last_report_path: 最新 qa_report.md への相対パス (optional)

    Returns:
        書き出した escalation_path
    """
    now_jst = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
    payload: dict = {
        "schema_version": 1,
        "created_at": now_jst,
        "channel": channel,
        "cut_id": cut_id,
        "cut_date": cut_date,
        "gate": gate,
        "attempts": attempts,
        "rework_history": rework_history,
        "hint": hint,
        "next_action": next_action,
        "owner": "human",
    }
    if last_report_path:
        payload["last_report_path"] = last_report_path
    p = work_dir / "_escalation.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def has_news_escalation(work_dir: Path) -> bool:
    return (work_dir / "_escalation.json").exists()


def read_news_escalation(work_dir: Path) -> dict | None:
    p = work_dir / "_escalation.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def clear_news_escalation(work_dir: Path) -> bool:
    """resume 完了時に escalation を消す。 存在しなければ False。"""
    p = work_dir / "_escalation.json"
    if not p.exists():
        return False
    p.unlink()
    return True


def build_rework_history(log_data: dict) -> list[dict]:
    """log_data["rounds"] を rework_history (簡易サマリ) に圧縮。

    既存 log.yaml の rounds は冗長(qa_warns/qa_fails 詳細含む)。
    escalation 用には round / qa_verdict / rework_strategy / started_at のみ。
    後方互換: rounds キー欠落でも空リスト返却。
    """
    out: list[dict] = []
    for r in (log_data.get("rounds") or []):
        out.append(
            {
                "round": r.get("round"),
                "qa_verdict": r.get("qa_verdict"),
                "rework_strategy": r.get("rework_strategy"),
                "started_at": r.get("started_at"),
                "warns": [w.get("code") for w in (r.get("qa_warns") or []) if isinstance(w, dict)],
                "fails": [f.get("code") for f in (r.get("qa_fails") or []) if isinstance(f, dict)],
            }
        )
    return out
