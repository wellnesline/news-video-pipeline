"""channel-xx ニュースパイプライン rework ループ orchestrator。

scout が生成した news_config.json + script.json が work_dir に存在することを前提に、
build → qa → rework_request 生成のラウンドループを制御する。

scout 自体の spawn は当面は人間 or Claude Code セッション経由。
orchestrator は rework_request.md を出して終了し、ユーザーが scout を re-run してから
再度 run_pipeline() を呼ぶフロー(Phase 12 で API SDK 自動化予定)。

使用例:
    from news_pipeline.orchestrator import run_pipeline
    from datetime import date
    result = run_pipeline("channel-xx", "cut_b", date(2000, 1, 1))
    # result["verdict"] in {"PASS", "WARN_ACCEPTED", "REWORK_NEEDED", "AUTO_FIX_EXHAUSTED"}
"""
from __future__ import annotations

import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml as _yaml

# ── PATH 設定 ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import os as _os

from news_pipeline.build import build_news  # noqa: E402
from news_pipeline.escalation import (  # noqa: E402
    build_rework_history,
    write_news_escalation,
)
from news_pipeline.fact_check import run_structural_fact_check  # noqa: E402
from news_pipeline.qa import run_structural_qa, write_qa_report, QAReport  # noqa: E402

if TYPE_CHECKING:
    pass

# ── Rework hard cap (audit) ────────────────────────────────────────
#
# 監査で round 24-25 まで暴発した例を検出。
# rework ループ防止ルール(最大2-3回)を
# Python レベルで強制する。`max_rounds` (CLI) は通常運用の AUTO_FIX_EXHAUSTED
# 閾値(default 3)、HARD_CAP は人間 escalate に倒す絶対上限。
#
# - max_rounds <= rework_count < HARD_CAP : AUTO_FIX_EXHAUSTED (従来挙動)
# - rework_count >= HARD_CAP              : HARD_CAP_REACHED + _escalation.json
HARD_CAP: int = 6

# ── Rework 戦略テーブル ───────────────────────────────────────────────────────

_REWORK_STRATEGIES: dict[int, str] = {
    1: "round1_light_fix",
    2: "round2_topic_regen",
    3: "round3_full_redesign",
}

_REWORK_INSTRUCTIONS: dict[int, str] = {
    1: (
        "## 修正指示(round 1: 軽修正)\n\n"
        "該当行のみ修正してください:\n"
        "- 各 QA 警告の対象行を個別に修正して再書き出し\n\n"
        "scout 再起動コマンド:\n"
        "`Claude Code で対応する news-scout-* agent を spawn し、"
        "上記指示に従って rework_round=1 として再生成`"
    ),
    2: (
        "## 修正指示(round 2: トピック再生成)\n\n"
        "問題のあるトピック全体を再生成してください:\n"
        "- QA WARN/FAIL が出たトピックを最初から書き直す\n"
        "- 他トピックは維持する\n\n"
        "scout 再起動コマンド:\n"
        "`Claude Code で対応する news-scout-* agent を spawn し、"
        "上記指示に従って rework_round=2 として再生成`"
    ),
    3: (
        "## 修正指示(round 3: 構造再設計)\n\n"
        "全トピックを再選定してください:\n"
        "- 既存トピック選定を破棄し、別の切り口で 4-5 トピックを選び直す\n"
        "- script.json も全行書き直し\n\n"
        "scout 再起動コマンド:\n"
        "`Claude Code で対応する news-scout-* agent を spawn し、"
        "上記指示に従って rework_round=3 として再生成`"
    ),
}


# ── 内部ユーティリティ ────────────────────────────────────────────────────────

def _work_dir(channel: str, cut_id: str, cut_date: date) -> Path:
    return ROOT / "channels" / channel / "videos" / "news" / str(cut_date) / cut_id


def _load_log(log_path: Path) -> dict:
    if not log_path.exists():
        return {}
    try:
        return _yaml.safe_load(log_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_log(log_path: Path, log_data: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        _yaml.dump(log_data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _count_rounds(log_data: dict) -> int:
    return len(log_data.get("rounds", []))


def _append_round(
    log_data: dict,
    round_num: int,
    build_status: str,
    qa_verdict: str,
    qa_fails: list[dict],
    qa_warns: list[dict],
    rework_strategy: str | None,
) -> None:
    log_data.setdefault("rounds", []).append(
        {
            "round": round_num,
            "started_at": datetime.now().isoformat(),
            "build_status": build_status,
            "qa_verdict": qa_verdict,
            "qa_fails": qa_fails,
            "qa_warns": qa_warns,
            "rework_strategy": rework_strategy,
        }
    )


def _qa_items_to_dicts(items: list) -> list[dict]:
    return [{"code": i.code, "message": i.message} for i in items]


def _backup_qa_report(work_dir: Path, round_num: int) -> None:
    """qa_report.md を qa_history/round{N}_qa_report.md にコピー保存する。"""
    src = work_dir / "qa_report.md"
    if not src.exists():
        return
    hist_dir = work_dir / "qa_history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    dst = hist_dir / f"round{round_num}_qa_report.md"
    shutil.copy2(str(src), str(dst))


def _write_rework_request(
    work_dir: Path,
    round_num: int,
    cut_date: date,
    cut_id: str,
    qa_report: "QAReport",
    strategy: str | None = None,
    instruction: str | None = None,
) -> Path:
    """rework_request.md を work_dir に書き出して Path を返す。"""
    strategy = strategy or "round_unknown"
    instruction = instruction or "## 修正指示\n\nQA 報告を参照して修正してください。"

    lines: list[str] = [f"# rework round {round_num}"]
    lines.append("")
    lines.append("## QA で検出された問題")
    lines.append("")

    for item in qa_report.failures:
        d = {"code": item.code, "message": item.message, "details": item.details}
        lines.append(f"- [{d['code']}] FAIL: {d['message']}")
        for detail in d.get("details", []):
            lines.append(f"  - {detail}")

    for item in qa_report.warnings:
        d = {"code": item.code, "message": item.message, "details": item.details}
        lines.append(f"- [{d['code']}] WARN: {d['message']}")
        for detail in d.get("details", []):
            lines.append(f"  - {detail}")

    lines.append("")
    lines.append(instruction)
    lines.append("")
    lines.append(f"<!-- strategy: {strategy} -->")

    out = work_dir / "rework_request.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ── メイン公開 API ────────────────────────────────────────────────────────────

def run_pipeline(
    channel: str,
    cut_id: str,
    cut_date: date,
    *,
    max_rounds: int = 3,
    accept_warn: bool = False,
    skip_fact_check: bool = False,
    auto_fix: bool = True,
    numeric_fact_check: bool = True,
) -> dict:
    """build → fact-check(構造) → qa → 必要なら rework_request 生成。

    work_dir (channels/<ch>/videos/news/<date>/<cut_id>/) に
    news_config.json + script.json が既に存在することを前提とする。

    Args:
        channel:              チャンネル名("channel-xx")
        cut_id:               カット識別子("cut_b" / "cut_a")
        cut_date:             配信日
        max_rounds:           最大ラウンド数(デフォルト 3)
        accept_warn:          True の場合、WARN を WARN_ACCEPTED として合格扱い
        skip_fact_check:      True の場合、構造的 fact-check をスキップ
        auto_fix:             True の場合、auto-fixable WARN を自動修正して re-build
        numeric_fact_check:   True の場合、Stooq CSV 数値 fact-check を実施

    Returns:
        {
            "verdict": "PASS" | "WARN_ACCEPTED" | "REWORK_NEEDED" | "AUTO_FIX_EXHAUSTED",
            "rework_count": int,
            "log_path": Path,
            "rework_request_path": Path | None,
            "fact_check_result": dict | None,
        }
    """
    work_dir = _work_dir(channel, cut_id, cut_date)
    log_path = work_dir / "log.yaml"

    # OPENAI_API_KEY チェック: hook_image 生成に必要
    if not _os.environ.get("OPENAI_API_KEY"):
        # .env からの自動ロードは build.py / hook_image.py 側で行われるが、
        # 未設定の場合は hook_image がスキップされることを事前通知する
        print(
            "[orchestrator] INFO: OPENAI_API_KEY が環境変数に未設定。"
            " hook_image 生成はスキップされます(.env に設定すると有効化)"
        )

    # log.yaml 読込または初期化
    log_data = _load_log(log_path)
    if not log_data:
        log_data = {
            "slug": f"{cut_id}_{cut_date}",
            "created_at": datetime.now().isoformat(),
            "rounds": [],
            "status": "IN_PROGRESS",
            "final_verdict": None,
        }

    rework_count = _count_rounds(log_data)
    fact_check_result = None
    numeric_result = None

    # ── HARD CAP ゲート (audit) ────────────────────────────────────
    # 既に HARD_CAP に到達済みなら即 escalation。新規 round は積まない。
    if rework_count >= HARD_CAP:
        rework_history = build_rework_history(log_data)
        hint = (
            f"rework_count={rework_count} が HARD_CAP={HARD_CAP} に到達。"
            " 同パターンの failure が繰り返されている可能性が高いため、"
            " (a) 手動で script.json / news_config.json を編集後 build_news.py で続行、"
            " (b) この回をスキップ、"
            " (c) qa rule の構造的見直し(false-positive 連鎖の調査)、"
            " のいずれかを人間が判断してください。"
        )
        qa_report_path = work_dir / "qa_report.md"
        esc_path = write_news_escalation(
            work_dir,
            channel=channel,
            cut_id=cut_id,
            cut_date=str(cut_date),
            gate="rework_hard_cap",
            attempts=rework_count,
            rework_history=rework_history,
            hint=hint,
            next_action="human_review",
            last_report_path=str(qa_report_path.relative_to(ROOT)) if qa_report_path.exists() else None,
        )
        log_data["status"] = "HARD_CAP_REACHED"
        log_data["final_verdict"] = None
        log_data.setdefault("rework_history", rework_history)
        _save_log(log_path, log_data)
        print(
            f"[orchestrator] HARD_CAP_REACHED: rework_count={rework_count} >= {HARD_CAP}"
            f" → escalation: {esc_path}"
        )
        return {
            "verdict": "HARD_CAP_REACHED",
            "rework_count": rework_count,
            "log_path": log_path,
            "rework_request_path": None,
            "fact_check_result": None,
            "escalation_path": esc_path,
        }

    # ── 構造的 fact-check ─────────────────────────────────────────────────────
    if not skip_fact_check:
        fact_check_result = run_structural_fact_check(work_dir)
        if fact_check_result["has_failures"]:
            print(f"[orchestrator] fact-check FAIL: {len(fact_check_result['details'])} 件")
            for d in fact_check_result["details"]:
                print(f"  {d}")

            rework_count += 1
            _append_round(
                log_data,
                round_num=rework_count,
                build_status="skipped(fact_check_fail)",
                qa_verdict="FACT_CHECK_FAIL",
                qa_fails=[{"code": d["code"], "message": d["message"]} for d in fact_check_result["details"]],
                qa_warns=[],
                rework_strategy=_REWORK_STRATEGIES.get(rework_count, "roundN_fix"),
            )
            log_data["status"] = "REWORK_NEEDED"
            log_data["final_verdict"] = None
            log_data["rework_history"] = build_rework_history(log_data)
            _save_log(log_path, log_data)

            # rework_request.md 生成(fact-check FAIL 専用)
            rework_lines = [
                "## 修正指示\n\nscout を再起動して各 topic の `source_urls` を修正してください:",
                "- 各 topic に `source_urls` 配列(最低 2 URL、全て `https://` で始まる)を追加",
                "- Step 2(一次ソース WebFetch)を実施し、取得した URL を記録すること",
            ]
            rework_path = work_dir / "rework_request.md"
            content_lines = [
                f"# rework round {rework_count}",
                "",
                "## fact-check で検出された問題",
                "",
            ]
            for d in fact_check_result["details"]:
                content_lines.append(f"- [{d['code']}] {d['message']}")
            content_lines.append("")
            content_lines.extend(rework_lines)
            content_lines.append("")
            content_lines.append(
                f"<!-- strategy: {_REWORK_STRATEGIES.get(rework_count, 'roundN_fix')} -->"
            )
            rework_path.write_text("\n".join(content_lines), encoding="utf-8")
            print(f"[orchestrator] rework_request.md -> {rework_path}")

            return {
                "verdict": "REWORK_NEEDED",
                "rework_count": rework_count,
                "log_path": log_path,
                "rework_request_path": rework_path,
                "fact_check_result": fact_check_result,
                "escalation_path": None,
            }
        else:
            print(
                f"[orchestrator] fact-check PASS: topics={fact_check_result.get('stats', {}).get('topics', '?')}"
                f" urls_checked={fact_check_result.get('stats', {}).get('urls_checked', '?')}"
            )
    else:
        print("[orchestrator] fact-check SKIP(--skip-fact-check)")

    # ── build + QA + auto-fix ループ ─────────────────────────────────────────
    build_status = "ok"
    try:
        build_news(channel, cut_id, cut_date)
    except Exception as exc:
        build_status = f"error: {exc}"
        print(f"[orchestrator] build FAILED: {exc}")

        rework_count += 1
        _append_round(
            log_data,
            round_num=rework_count,
            build_status=build_status,
            qa_verdict="BUILD_FAIL",
            qa_fails=[],
            qa_warns=[],
            rework_strategy=_REWORK_STRATEGIES.get(rework_count, "roundN_fix"),
        )
        log_data["status"] = "BUILD_FAIL"
        log_data["final_verdict"] = None
        log_data["rework_history"] = build_rework_history(log_data)
        _save_log(log_path, log_data)
        return {
            "verdict": "BUILD_FAIL",
            "rework_count": rework_count,
            "log_path": log_path,
            "rework_request_path": None,
            "fact_check_result": fact_check_result,
            "escalation_path": None,
        }

    # QA 実行
    qa_result: QAReport = run_structural_qa(work_dir)
    write_qa_report(qa_result, work_dir)
    overall = qa_result.overall()

    print(
        f"[orchestrator] QA {overall}: PASS={len(qa_result.passes)}"
        f" WARN={len(qa_result.warnings)} FAIL={len(qa_result.failures)}"
    )

    rework_count += 1
    this_round = rework_count

    # ── Phase 1: auto-fix 適用 ────────────────────────────────────────────────
    auto_fixed_codes: list[str] = []
    if (
        auto_fix
        and not qa_result.has_blocking_failures()
        and qa_result.warnings
    ):
        from news_pipeline.auto_fix import auto_fix_warns  # noqa: E402
        script_path = work_dir / "script.json"
        fixed, codes = auto_fix_warns(qa_result, work_dir, script_path)
        if fixed:
            auto_fixed_codes = codes
            print(f"[orchestrator] auto-fix applied: {codes} → re-build + re-QA")

            # re-build
            try:
                build_news(channel, cut_id, cut_date)
            except Exception as exc:
                build_status = f"error(after_auto_fix): {exc}"
                print(f"[orchestrator] re-build FAILED: {exc}")

            # re-QA
            qa_result = run_structural_qa(work_dir)
            write_qa_report(qa_result, work_dir)
            overall = qa_result.overall()
            print(
                f"[orchestrator] re-QA {overall}: PASS={len(qa_result.passes)}"
                f" WARN={len(qa_result.warnings)} FAIL={len(qa_result.failures)}"
            )

    # ── Phase 2: 数値 fact-check ─────────────────────────────────────────────
    if numeric_fact_check and not skip_fact_check:
        try:
            from news_pipeline.fact_check import run_numeric_fact_check
            numeric_result = run_numeric_fact_check(work_dir, channel)
            print(f"[orchestrator] numeric fact-check: overall={numeric_result['overall']}")
            if numeric_result.get("has_failures"):
                # 数値照合 FAIL → REWORK_NEEDED
                overall = "FAIL(numeric)"
        except Exception as exc:
            print(f"[orchestrator] numeric fact-check WARN(error): {exc}")
            numeric_result = {"overall": "WARN", "skipped": True, "skip_reason": str(exc)}
    else:
        print("[orchestrator] numeric fact-check SKIP")

    # ── verdict 決定 ──────────────────────────────────────────────────────────
    strategy: str | None
    verdict: str

    if not qa_result.has_blocking_failures() and not qa_result.warnings:
        strategy = None
        verdict = "PASS"
    elif (
        not qa_result.has_blocking_failures()
        and qa_result.warnings
        and accept_warn
    ):
        strategy = None
        verdict = "WARN_ACCEPTED"
    elif this_round < max_rounds:
        strategy = _REWORK_STRATEGIES.get(this_round, "roundN_fix")
        verdict = "REWORK_NEEDED"
    elif this_round >= max_rounds:
        strategy = _REWORK_STRATEGIES.get(this_round, "roundN_fix")
        verdict = "AUTO_FIX_EXHAUSTED"
    else:
        strategy = _REWORK_STRATEGIES.get(this_round, "roundN_fix")
        verdict = "REWORK_NEEDED"

    # 数値 FAIL があれば REWORK_NEEDED に格下げ(PASS/WARN_ACCEPTED を上書き)
    if numeric_result and numeric_result.get("has_failures"):
        if verdict in ("PASS", "WARN_ACCEPTED"):
            strategy = _REWORK_STRATEGIES.get(this_round, "roundN_fix")
            verdict = "REWORK_NEEDED"

    _append_round(
        log_data,
        round_num=this_round,
        build_status=build_status,
        qa_verdict=overall,
        qa_fails=_qa_items_to_dicts(qa_result.failures),
        qa_warns=_qa_items_to_dicts(qa_result.warnings),
        rework_strategy=strategy,
    )

    # auto_fixed_codes を記録
    if auto_fixed_codes and log_data.get("rounds"):
        log_data["rounds"][-1]["auto_fixed_codes"] = auto_fixed_codes

    log_data["status"] = verdict
    log_data["final_verdict"] = verdict if verdict in ("PASS", "WARN_ACCEPTED") else None
    # rework_history (cross-turn 永続化): rounds と並走、escalation 共有用
    log_data["rework_history"] = build_rework_history(log_data)
    _save_log(log_path, log_data)

    _backup_qa_report(work_dir, this_round)

    # rework_request.md 生成
    rework_request_path: Path | None = None
    if verdict in ("REWORK_NEEDED", "AUTO_FIX_EXHAUSTED"):
        rework_request_path = _write_rework_request(
            work_dir,
            this_round,
            cut_date,
            cut_id,
            qa_report=qa_result,
            strategy=strategy,
            instruction=_REWORK_INSTRUCTIONS.get(this_round),
        )
        print(f"[orchestrator] rework_request.md -> {rework_request_path}")

        if verdict == "AUTO_FIX_EXHAUSTED":
            print(
                "[orchestrator] AUTO_FIX_EXHAUSTED: 最大ラウンド到達。"
                " 人間が手動でスクリプト編集後 build_news.py で続行、"
                " またはこの回をスキップしてください。"
            )

    # ── HARD_CAP 到達ガード: 今回 round で HARD_CAP に達したら escalation 併発 ──
    # (run_pipeline 冒頭ガードは「次回呼出を止める」用、ここは「今回到達」用)
    escalation_path: Path | None = None
    if this_round >= HARD_CAP and verdict not in ("PASS", "WARN_ACCEPTED"):
        hint = (
            f"rework_count={this_round} が HARD_CAP={HARD_CAP} に到達。"
            f" 直近 verdict={verdict}。同パターン繰り返しの疑いがあるため人間判断を要請。"
        )
        qa_report_path = work_dir / "qa_report.md"
        escalation_path = write_news_escalation(
            work_dir,
            channel=channel,
            cut_id=cut_id,
            cut_date=str(cut_date),
            gate="rework_hard_cap",
            attempts=this_round,
            rework_history=log_data["rework_history"],
            hint=hint,
            next_action="human_review",
            last_report_path=str(qa_report_path.relative_to(ROOT)) if qa_report_path.exists() else None,
        )
        log_data["status"] = "HARD_CAP_REACHED"
        _save_log(log_path, log_data)
        print(
            f"[orchestrator] HARD_CAP_REACHED at this round={this_round}"
            f" → escalation: {escalation_path}"
        )
        verdict = "HARD_CAP_REACHED"

    print(f"[orchestrator] verdict={verdict} round={this_round}/{max_rounds} (HARD_CAP={HARD_CAP})")

    return {
        "verdict": verdict,
        "rework_count": this_round,
        "log_path": log_path,
        "rework_request_path": rework_request_path,
        "fact_check_result": fact_check_result,
        "escalation_path": escalation_path,
    }
