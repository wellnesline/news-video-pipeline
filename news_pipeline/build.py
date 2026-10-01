"""ニュースパイプライン メインオーケストレータ。

build_news(channel, cut_id, date) の流れ:
  1. variants/<cut_id>.load_news_cut(date) で NewsCut ロード
  2. synth.synth_script() でWAV合成
  3. synth.reschedule() でタイムライン確定
  4. bubble.build_all_bubbles() で吹き出しPNG生成
  5. Remotion render (npx remotion render) — NEWS_CONFIG / SCRIPT を env 経由で渡す
  6. compose.compose_news() でffmpeg合成
  7. output.mp4 を出力
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from news_pipeline.base import NewsCut  # noqa: E402
from news_pipeline import synth as _synth  # noqa: E402
from news_pipeline import bubble as _bubble  # noqa: E402
from news_pipeline import compose as _compose  # noqa: E402
from news_pipeline import meta as _meta  # noqa: E402
from news_pipeline.hook_image import generate_hook_image  # noqa: E402
from news_pipeline.artifacts import discard_news_intermediates  # noqa: E402


def _load_cut(cut_id: str, cut_date: date, work_dir: Path) -> NewsCut:
    """variants/<cut_id>.load_news_cut(date) を動的ロードして NewsCut を返す。

    work_dir を渡すと variants 側が news_config.json / script.json を書き出す。
    """
    module_name = f"news_pipeline.variants.{cut_id}"
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise ValueError(
            f"未知の cut_id: '{cut_id}'. variants/"
            f"{cut_id}.py が存在しません。"
        )
    return mod.load_news_cut(cut_date, work_dir=work_dir)


def _news_work_dir(channel: str, cut_id: str, cut_date: date) -> Path:
    """作業ディレクトリ: channels/<ch>/videos/news/<date>/<cut_id>/"""
    return ROOT / "channels" / channel / "videos" / "news" / str(cut_date) / cut_id


def _calc_hook_until_frame(script_data: dict, until_line: str, fps: int = 30) -> int:
    """script.json の until_line に対応するフレーム番号を算出する。

    until_line の解釈:
        - "L05" 形式 → その line の終端時刻
        - "T1" 形式  → その topic を含むセクション全体の終端 = 次セクション(T2 など)の最初の line の開始 frame
        - "INTRO" 形式 → INTRO セクション全体の終端 = T1 の最初の line の開始 frame
        - "PROLOGUE" 形式 → INTRO + T1 セクション全体の終端 = T2 の最初の line の開始 frame

    Remotion の sectionStart 計算と完全に一致させるため、`round(start_sec * fps)` を使う。
    INTRO / Tn は「次セクション開始 frame」を返すことで、hook と通常 Remotion の境界に
    1 frame の隙間も発生しないようにする(SceneIntro 一瞬表示バグの根治)。

    対象が見つからない場合はデフォルト値 90(3秒 @30fps)を返す。
    """
    lines = script_data.get("lines", [])
    target = until_line.upper().strip()

    if not lines:
        return 90

    def _topic_of(line: dict) -> str:
        return (line.get("topic") or "").upper()

    def _start_frame(line: dict) -> int:
        return round(float(line.get("start_sec", 0.0)) * fps)

    def _end_frame(line: dict) -> int:
        end_sec = float(line.get("start_sec", 0.0)) + float(line.get("duration_sec", 3.0))
        return round(end_sec * fps)

    def _first_line_of_topic(topic_name: str) -> "dict | None":
        for ln in lines:
            if _topic_of(ln) == topic_name:
                return ln
        return None

    # PROLOGUE: T2 の最初の line の開始 frame(= T1 セクションの終端)
    if target == "PROLOGUE":
        first_t2 = _first_line_of_topic("T2")
        if first_t2 is not None:
            return _start_frame(first_t2)

    # INTRO: T1 の最初の line の開始 frame(= INTRO セクションの終端)
    if target == "INTRO":
        first_t1 = _first_line_of_topic("T1")
        if first_t1 is not None:
            return _start_frame(first_t1)

    # Tn: T(n+1) の最初の line の開始 frame
    if target.startswith("T") and target[1:].isdigit():
        n = int(target[1:])
        next_topic = f"T{n+1}"
        first_next = _first_line_of_topic(next_topic)
        if first_next is not None:
            return _start_frame(first_next)
        # 次トピックが無ければ OUTRO 探索
        first_outro = _first_line_of_topic("OUTRO")
        if first_outro is not None:
            return _start_frame(first_outro)

    # Lnn: 個別 line の終端
    for line in lines:
        line_id = (line.get("id") or line.get("line_id") or "").upper()
        if line_id == target:
            return _end_frame(line)

    # フォールバック: 3秒
    return 90


def _render_remotion(
    news_config: dict,
    script_data: dict,
    remotion_root: Path,
    out_mp4: Path,
    composition_id: str | None = None,
    hook_image_path: "Path | None" = None,
) -> None:
    """Remotion でバックグラウンド MP4 をレンダリング。

    --props='{"config": {...}, "script": {...}}' でデータを渡す(inputProps 方式)。
    env 変数渡しは廃止済み。
    hook_image_path が指定された場合、inputProps に hook_image_path と
    hook_image_until_frame を追加して Remotion に渡す。
    """
    # 明示指定は専用 Composition のために優先する。
    # 呼び出し元が指定しない既存 cut は従来どおり topic 数で選択する。
    if not composition_id:
        topics = news_config.get("topics", [])
        if len(topics) <= 4:
            composition_id = "News4Topic"
        else:
            composition_id = "News5Topic"

    input_props: dict = {"config": news_config, "script": script_data}

    # hook_image があれば inputProps に追加
    # public/<channel>/news/<slug>/hook.png にコピーして staticFile 経由で渡す
    # (Remotion の Chromium はセキュリティ上 file:// ローカルパス読込不可)
    if hook_image_path is not None:
        channel    = news_config.get("channel", "channel-xx")
        cut_id     = news_config.get("cut_id") or news_config.get("format", "news")
        date_label = news_config.get("date", "unknown").replace(".", "-").replace(" ", "")
        rel_dir    = f"{channel}/hook/{date_label}_{cut_id}"
        public_hook_dir = ROOT / "public" / rel_dir
        public_hook_dir.mkdir(parents=True, exist_ok=True)
        public_hook = public_hook_dir / "hook.png"
        import shutil as _sh
        _sh.copy2(hook_image_path, public_hook)
        input_props["hook_image_path"] = f"{rel_dir}/hook.png"
        # until_line は topics[0].hook_image.until_line から取得
        hook_cfg   = topics[0].get("hook_image", {}) if topics else {}
        until_line = hook_cfg.get("until_line", "L02")
        fps        = news_config.get("fps", 30)
        input_props["hook_image_until_frame"] = _calc_hook_until_frame(
            script_data, until_line, fps=fps
        )
        print(f"[build] hook_image: public/{rel_dir}/hook.png until_frame={input_props['hook_image_until_frame']}")

    # ★--props はファイル渡しにする。news_config + script30行を JSON 直書きすると
    #   数万文字になり、Windows のコマンドライン長上限(8191字)で
    #   「The command line is too long.」になる (実測)。
    #   Remotion CLI は --props に JSON 文字列とファイルパスの両方を受ける。
    props_file = out_mp4.parent / "remotion_props.json"
    props_file.write_text(json.dumps(input_props, ensure_ascii=False), encoding="utf-8")

    # remotion_root は src/remotion/news/(build_news() で渡される)→ index.ts を直接指す
    entry_point = remotion_root / "index.ts" if remotion_root.is_dir() else remotion_root

    # インストール済みの CLI を直接起動し、npx の解決・ネットワーク待ちを避ける。
    local_cli = ROOT / "node_modules" / ".bin" / ("remotion.cmd" if os.name == "nt" else "remotion")
    launcher = [str(local_cli)] if local_cli.exists() else [shutil.which("npx") or "npx", "remotion"]

    cmd = [
        *launcher, "render",
        str(entry_point),
        composition_id,
        str(out_mp4),
        f"--props={props_file}",
        "--concurrency=4",
        # ★追加 (フォント取得遅延でフレーム待ちがタイムアウトする件):
        #   NewsFlexible.tsx:33 の loadFont は weights は 3 つに絞っているが
        #   subsets を渡していない。@remotion/google-fonts/NotoSansJP.mjs:46 の
        #     const subsets = options?.subsets ?? Object.keys(...)
        #   で 124 subsets 全部を取りに行き、3 x 124 = 372 本の delayRender が立つ。
        #   フォント側の猶予は同 :64 で 60 秒あるのに、フレーム待ちは既定 30 秒
        #   (seek-to-frame.js:45 が +3000 して 33 秒) しかなく、
        #   フォントが遅れた時は必ずフレーム側が先に諦める。
        #   特定のフレーム番号に意味はない (concurrency=4 のどのページが
        #   担当したフレームでも同じことが起きうる)。
        #   フォント側の 60 秒より上に置くことで、本当に取れなかった場合は
        #   「フレーム N でタイムアウト」ではなくフォント側の原因が出るようにする。
        #   ★これは応急処置。372 リクエストは残るのでレンダリングは遅いままで、
        #     subsets 未指定は Remotion v5 では例外になる (NotoSansJP.mjs:28)。
        "--timeout=120000",
    ]

    print(f"[build] remotion render: {composition_id} -> {out_mp4.name}")
    # ★encoding を明示する 。compose.py:656 と同じ理由。
    #   text=True だけだと Windows は cp932 で読もうとし、Remotion が出す UTF-8 で
    #   UnicodeDecodeError になる。例外は読み取りスレッド内で起きるため run() は
    #   素通りし、stderr が None のまま返って失敗理由が消える。
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT),
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        stderr = result.stderr or ""
        print("  [remotion] FAILED")
        for line in stderr.splitlines()[-20:]:
            print(line)
        raise RuntimeError(f"Remotion render failed (rc={result.returncode})\n{stderr[-2000:]}")
    print(f"  [remotion] OK: {out_mp4.name}")


def build_news(channel: str, cut_id: str, cut_date: date) -> Path:
    """ニュースパイプラインのメイン関数。output.mp4 のパスを返す。

    Args:
        channel:   チャンネル名("channel-xx")
        cut_id:    カット識別子("cut_b")
        cut_date:  配信日

    Returns:
        生成した output.mp4 の絶対パス
    """
    print(f"[build] channel={channel} cut={cut_id} date={cut_date}")
    work_dir = _news_work_dir(channel, cut_id, cut_date)
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"[build] work_dir={work_dir}")

    # Step 1: NewsCut ロード
    news_cut = _load_cut(cut_id, cut_date, work_dir)
    print(f"[build] topics={len(news_cut.topics)}")

    # script.json 存在確認
    script_path = work_dir / "script.json"
    if not script_path.exists():
        raise FileNotFoundError(
            f"script.json が見つかりません: {script_path}\n"
            f"variants/{cut_id}.load_news_cut() が work_dir へ script.json を書き出すことを確認してください。"
        )

    # 誤読 監査 ループ Phase 1 — synth 前に qa_misread_auto を非 blocking で実行。
    # 検出結果は <work_dir>/qa_misread_auto.json に書かれ、 後で `make build-dict-pending` で
    # channels/<ch>/dict_pending.yaml に集約される。channel.yaml qa.misread_check で gate。
    try:
        import yaml as _yaml_local
        _ch_yaml = ROOT / "channels" / channel / "channel.yaml"
        _qa_cfg = {}
        if _ch_yaml.exists():
            _cfg = _yaml_local.safe_load(_ch_yaml.read_text(encoding="utf-8")) or {}
            _qa_cfg = _cfg.get("qa") or {}
        if _qa_cfg.get("misread_check", False):
            import sys as _sys_local
            qa_mr_path = ROOT / "src" / "qa_misread_auto.py"
            _cmd = [
                _sys_local.executable, str(qa_mr_path),
                "--channel", channel,
                "--topic", "_news",
                "--news-date", cut_date.strftime("%Y-%m-%d"),
                "--news-topic-id", cut_id,
                "--force",
            ]
            print(f"[misread-audit] pre-synth detect: news/{cut_date}/{cut_id}")
            subprocess.run(_cmd, cwd=str(ROOT), timeout=120)
    except Exception as _mre:
        print(f"[warn] pre-synth misread audit skipped: {_mre}")

    # Step 2: WAV 合成
    voice_dir = work_dir / "voice"
    # A changed speech script invalidates every derived artifact, not merely a
    # same-named L01.wav.  synth_script performs the same check defensively.
    if not _synth.voice_cache_matches(script_path, voice_dir):
        removed = discard_news_intermediates(work_dir)
        if removed:
            print("[build] stale intermediates discarded: " + ", ".join(p.name for p in removed))
    print("[build] synth WAV ...")
    durations = _synth.synth_script(script_path, voice_dir)

    # Step 3: reschedule
    total_sec = _synth.reschedule(script_path, durations)
    print(f"[build] total_sec={total_sec:.1f}s")

    # timing.json
    timing_path = work_dir / "timing.json"
    _synth.write_timing(script_path, "voice", timing_path)

    # Step 4: 吹き出し生成
    bubble_dir = work_dir / "bubbles"
    print("[build] gen bubbles ...")
    _bubble.build_all_bubbles(script_path, bubble_dir, channel=channel)

    # Step 5: Remotion render
    remotion_mp4 = work_dir / "remotion.mp4"
    nc_path = work_dir / "news_config.json"
    if not nc_path.exists():
        raise FileNotFoundError(f"news_config.json が見つかりません: {nc_path}")

    news_config = json.loads(nc_path.read_text(encoding="utf-8"))
    script_data  = json.loads(script_path.read_text(encoding="utf-8"))

    # bg_image: channels/<ch>/assets/news_bg.png を public/<ch>/news_bg.png に同期
    # Remotion の staticFile() が public/ をルートに参照するので、相対パスは <ch>/news_bg.png
    src_bg = ROOT / "channels" / channel / "assets" / "news_bg.png"
    if src_bg.exists():
        public_bg_dir = ROOT / "public" / channel
        public_bg_dir.mkdir(parents=True, exist_ok=True)
        public_bg = public_bg_dir / "news_bg.png"
        if not public_bg.exists() or src_bg.stat().st_mtime > public_bg.stat().st_mtime:
            import shutil as _sh
            _sh.copy2(src_bg, public_bg)
        news_config["bg_image_static"] = f"{channel}/news_bg.png"
        print(f"[build] bg_image: public/{channel}/news_bg.png")

    # Step 4.5: フックイメージ生成(topics[0].hook_image がある場合のみ)
    hook_image_path: "Path | None" = None
    try:
        hook_image_path = generate_hook_image(nc_path, work_dir)
        if hook_image_path:
            print(f"[build] hook_image: {hook_image_path}")
        else:
            print("[build] hook_image: なし(フィールド未設定 or スキップ)")
    except Exception as exc:
        print(f"  [build] WARN: hook_image 生成失敗 → hook なしで続行: {exc}")
        hook_image_path = None

    remotion_root = ROOT / "remotion" / "news"
    if not remotion_root.exists():
        # fallback: src/remotion/news
        remotion_root = ROOT / "src" / "remotion" / "news"

    if not remotion_root.exists():
        print(f"  [build] WARN: remotion_root not found: {remotion_root}, Remotion render をスキップ")
    else:
        _render_remotion(
            news_config,
            script_data,
            remotion_root,
            remotion_mp4,
            composition_id=news_config.get("composition_id", ""),
            hook_image_path=hook_image_path,
        )

    # Step 6: ffmpeg compose
    out_mp4 = work_dir / "output.mp4"
    if remotion_mp4.exists():
        print("[build] compose (ffmpeg) ...")
        _compose.compose_news(
            script_path=script_path,
            remotion_mp4=remotion_mp4,
            bubble_dir=bubble_dir,
            voice_dir=voice_dir,
            out_mp4=out_mp4,
            channel=channel,
            work_dir=work_dir / "_prescale",
            cut_id=cut_id,
        )
    else:
        print("  [build] remotion.mp4 なし → ffmpeg compose をスキップ")

    # Step 7 (=step 9): 構造 QA。
    try:
        from news_pipeline.qa import run_structural_qa, write_qa_report  # noqa: E402
        qa_result = run_structural_qa(work_dir)
        write_qa_report(qa_result, work_dir)
        overall = qa_result.overall()
        print(
            f"[build] QA {overall}: PASS={len(qa_result.passes)}"
            f" WARN={len(qa_result.warnings)} FAIL={len(qa_result.failures)}"
        )
        if qa_result.has_blocking_failures():
            fail_count = len(qa_result.failures)
            raise RuntimeError(
                f"QA FAIL: {fail_count} 件のブロッキング問題。"
                f" {work_dir}/qa_report.md を参照してください。"
            )
    except ImportError as exc:
        print(f"  [build] WARN: qa モジュール import 失敗のため QA をスキップ: {exc}")

    # Step 8: meta.json 書出
    _write_meta(news_cut, channel, cut_id, work_dir)

    print(f"[build] DONE: {out_mp4}")
    return out_mp4


def _resolve_content_type(channel: str, cut_id: str) -> dict:
    """channel.yaml から cut_id を含む content_type エントリを返す。

    content_types[].cuts[id=cut_id] を検索。見つからなければ空 dict を返す。
    """
    try:
        import yaml as _yaml
        ch_yaml = ROOT / "channels" / channel / "channel.yaml"
        cfg = _yaml.safe_load(ch_yaml.read_text(encoding="utf-8")) or {}
        for ct in cfg.get("content_types", []):
            cuts = ct.get("cuts", [])
            if any(c.get("id") == cut_id for c in cuts):
                return ct
    except Exception:
        pass
    return {}


def _write_meta(news_cut: NewsCut, channel: str, cut_id: str, work_dir: Path) -> None:
    """アップロード用 meta.json を work_dir に書き出す。

    形式: {"title": "...", "description": "...", "tags": [...], "category_id": "25",
           "content_type": "news", "shorts_only": true}
    """
    try:
        # タイトル/概要/タグのテンプレートは meta.py の設定 (NEWS_META_CONFIG) で渡す
        title       = _meta.gen_title(news_cut, cut_id)
        description = _meta.gen_description(news_cut, cut_id)
        tags        = _meta.gen_tags(news_cut, cut_id)
        meta_obj = {
            "title":        title,
            "description":  description,
            "tags":         tags,
            "category_id":  "25",
            "content_type": "news",
            "shorts_only":  True,
        }

        out = work_dir / "meta.json"
        out.write_text(
            json.dumps(meta_obj, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[build] meta.json -> {out}")
    except Exception as exc:
        print(f"  [build] WARN: meta.json 生成失敗: {exc}")
