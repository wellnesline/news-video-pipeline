"""Phase 1: ルールベース WARN 自動修正モジュール。

A9 / A8 / A12 の3コードを対象に機械的置換で script.json を修正する。
LLM が不要な修正のみを対象とし、A3 / A6 等は Phase 3 で対応予定。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from news_pipeline.qa import QAReport

# ── 修正ルール定数 ────────────────────────────────────────────────────────────

# A9: 「！？」「?!」「！?」→「！」「!」「！」
_A9_PATTERNS: list[tuple[str, str]] = [
    (r"！？", "！"),
    (r"\?!", "!"),
    (r"！\?", "！"),
    (r"\?！", "！"),
]

# A12: セリフから削除すべき投資免責語句(運用ルール準拠)
_A12_DISCLAIMER_WORDS = [
    "自己責任",
    "投資推奨",
    "元本",
    "免責",
    "リスク管理",
]


def _fix_a9(text: str) -> str:
    """「！？」系の複合記号を単純な感嘆符に置換する。"""
    result = text
    for pattern, replacement in _A9_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result


def _fix_a8(lines: list[dict], line_idx: int) -> list[dict]:
    """70字超の行を「、」「。」で2行に分割する。

    分割できない場合は修正スキップ(70字以内になるまで繰り返す構造は持たない)。
    新行の id は Lxx-2 形式。speaker / expression は継承。
    """
    line = lines[line_idx]
    text = line.get("text", "")
    if len(text) <= 70:
        return lines  # 既に修正済み

    # 最後の「、」または「。」位置を探す
    split_pos = -1
    for i in range(min(len(text) - 1, 70), 0, -1):
        if text[i] in ("、", "。"):
            split_pos = i + 1  # 句読点を含む位置の直後
            break

    if split_pos <= 0:
        # 句読点が見つからない場合は強制 70字で分割
        split_pos = 70

    part1 = text[:split_pos].strip()
    part2 = text[split_pos:].strip()
    if not part2:
        return lines  # 分割不可

    orig_id = line.get("id", f"L{line_idx + 1:02d}")
    new_id = f"{orig_id}-2"

    new_line = {
        "id": new_id,
        "speaker": line.get("speaker", ""),
        "expression": line.get("expression", "normal"),
        "text": part2,
        "highlight": line.get("highlight", ""),
    }
    # start_sec / duration_sec は再合成で再計算されるため省略

    updated_lines = list(lines)
    updated_lines[line_idx] = {**line, "text": part1}
    updated_lines.insert(line_idx + 1, new_line)
    return updated_lines


def _fix_a12(text: str) -> str:
    """投資免責語句を text から削除する(行ごとではなく語句のみ削除)。"""
    result = text
    for word in _A12_DISCLAIMER_WORDS:
        result = result.replace(word, "")
    # 連続する句読点・空白を整理
    result = re.sub(r"[、。]{2,}", "。", result)
    result = re.sub(r"\s{2,}", " ", result)
    return result.strip()


def _delete_stale_artifacts(work_dir: Path, line_id: str) -> None:
    """修正行の WAV / bubble PNG を削除して次 build での再合成を促す。"""
    voice_wav = work_dir / "voice" / f"{line_id}.wav"
    bubble_png = work_dir / "bubbles" / f"bubble_{line_id}.png"
    for path in (voice_wav, bubble_png):
        if path.exists():
            path.unlink()
            print(f"  [auto_fix] deleted stale artifact: {path.name}")


def auto_fix_warns(
    qa_report: "QAReport",
    work_dir: Path,
    script_path: Path,
) -> tuple[bool, list[str]]:
    """auto-fixable WARN コードを script.json に直接適用する。

    Args:
        qa_report:   run_structural_qa() の結果
        work_dir:    ビルド作業ディレクトリ
        script_path: script.json の絶対パス

    Returns:
        (fixed_anything, fixed_codes): 修正が1件以上あれば True / 修正済みコード一覧
    """
    # auto-fix 対象コード
    AUTO_FIXABLE = {"A9", "A8", "A12"}

    warn_codes = {item.code for item in qa_report.warnings if item.code in AUTO_FIXABLE}
    if not warn_codes:
        return False, []

    # script.json 読込
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [auto_fix] script.json 読込失敗: {exc}")
        return False, []

    lines: list[dict] = script.get("lines", [])
    fixed_any = False
    fixed_codes: list[str] = []
    fixed_line_ids: list[str] = []

    # A9: 全行を走査して「！？」系を置換
    if "A9" in warn_codes:
        for i, line in enumerate(lines):
            original = line.get("text", "")
            fixed = _fix_a9(original)
            if fixed != original:
                lines[i] = {**line, "text": fixed}
                line_id = line.get("id", f"L{i + 1:02d}")
                print(f"  [auto_fix] A9 applied: line={line_id} '{original}' → '{fixed}'")
                fixed_line_ids.append(line_id)
                fixed_any = True
        if fixed_any or "A9" in warn_codes:
            fixed_codes.append("A9")

    # A12: 投資免責語句を削除
    if "A12" in warn_codes:
        a12_fixed = False
        for i, line in enumerate(lines):
            original = line.get("text", "")
            fixed = _fix_a12(original)
            if fixed != original:
                lines[i] = {**line, "text": fixed}
                line_id = line.get("id", f"L{i + 1:02d}")
                print(f"  [auto_fix] A12 applied: line={line_id}")
                fixed_line_ids.append(line_id)
                a12_fixed = True
                fixed_any = True
        if a12_fixed:
            fixed_codes.append("A12")

    # A8: 70字超行を分割(後ろから処理してインデックスずれを防ぐ)
    if "A8" in warn_codes:
        a8_fixed = False
        i = len(lines) - 1
        while i >= 0:
            line = lines[i]
            text = line.get("text", "")
            if len(text) > 70:
                line_id = line.get("id", f"L{i + 1:02d}")
                lines = _fix_a8(lines, i)
                print(f"  [auto_fix] A8 applied: line={line_id} ({len(text)}字 → 分割)")
                fixed_line_ids.append(line_id)
                a8_fixed = True
                fixed_any = True
            i -= 1
        if a8_fixed:
            fixed_codes.append("A8")

    if not fixed_any:
        return False, []

    # script.json 上書き保存(アトミック: 一時ファイル経由)
    script["lines"] = lines
    tmp_path = script_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.move(str(tmp_path), str(script_path))
    except Exception as exc:
        print(f"  [auto_fix] script.json 書込失敗: {exc}")
        if tmp_path.exists():
            tmp_path.unlink()
        return False, []

    # 修正行の古い成果物を削除して再合成を促す
    seen_ids: set[str] = set()
    for line_id in fixed_line_ids:
        if line_id not in seen_ids:
            _delete_stale_artifacts(work_dir, line_id)
            seen_ids.add(line_id)

    print(f"  [auto_fix] 修正完了: codes={fixed_codes}")
    return True, fixed_codes
