"""ffmpeg + Remotion 合成 — Pillow-pipe 2トラック方式(改訂)

変更点:
  - ffmpeg overlay チェーン(立ち絵+吹き出し)を Pillow-pipe 方式に刷新。
    build_portrait_track_news() / build_bubble_track_news() で透過 .mov を事前生成し、
    ffmpeg overlay を 2段固定(背景 → portrait → bubble)にする。
  - BGM mix ロジック(_resolve_bgm / _resolve_bgm_volume)は維持。
  - compose_news() のシグネチャは不変(build.py / orchestrator.py との後方互換を保つ)。
  - prescale_characters() は既存のまま維持。
  - 50段ガードは回帰防止のため維持(実質発動しない)。

教訓適用:
  A: subprocess パイプ → proc.stdin.close() → proc.wait() → proc.stderr.read()
  B: dim 穴バグ → line_idx 整数フレーム管理、t_abs 境界比較なし
  C: BUBBLE_Y は top-left Y 規約(by = BUBBLE_Y)
  x座標: right speaker は W - sw - CHAR_EDGE_OFFSET(abs 使わない)

旧実装:
  - 立ち絵パスを character_path() 経由に変更(固定パスのハードコード廃止)。
  - prescale_characters() でチャンネル立ち絵を prescale し、CHARS_DIR_SCALED へキャッシュ。
  - CHARS_DIR_ORIG / CHARS_DIR_SCALED は work_dir 引数に基づく。
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import warnings
from pathlib import Path

from PIL import Image

# 親 src/ を import パスに追加(audio_master を news_pipeline 外から取り込むため)
_SRC_DIR = Path(__file__).resolve().parent.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
from audio_master import pre_bake_loudnorm  # noqa: E402  speech -16 LUFS 統一
from _platform import hw_video_encoder  # noqa: E402  OS 別の H.264 エンコーダ

# ── レイアウト定数(不変条件) ─────────────
W  = 1080
H  = 1920
FPS = 30

# 配置・原寸などの固定値は設定でまとめて渡す(下はすべて中立なサンプル値)。
# 環境変数 NEWS_LAYOUT_CONFIG に JSON ファイルのパスを指定すると上書きされる。
# JSON の形は config/layout.example.json を参照。
DEFAULT_LAYOUT: dict = {
    "char_target_h": 640,
    "char_y_base": 1180,
    "char_edge_offset": -32,
    "bubble_y": 1420,
    # speaker ごとの立ち絵原寸(px)・表示側・左右反転
    "speakers": {
        "speaker_a": {"orig": [1000, 1500], "side": "left",  "hflip": False},
        "speaker_b": {"orig": [1000, 1500], "side": "right", "hflip": False},
        "speaker_c": {"orig": [1000, 1500], "side": "left",  "hflip": False},
    },
}


def _load_layout() -> dict:
    import os
    layout = json.loads(json.dumps(DEFAULT_LAYOUT))
    path = os.environ.get("NEWS_LAYOUT_CONFIG", "")
    if path:
        try:
            user = json.loads(Path(path).read_text(encoding="utf-8"))
            for k, v in user.items():
                if k == "speakers" and isinstance(v, dict):
                    layout["speakers"].update(v)
                else:
                    layout[k] = v
        except Exception as exc:
            warnings.warn(f"layout config {path} を読めないため既定値を使います: {exc}")
    return layout


_LAYOUT = _load_layout()

CHAR_TARGET_H    = int(_LAYOUT["char_target_h"])
CHAR_Y_BASE      = int(_LAYOUT["char_y_base"])
CHAR_EDGE_OFFSET = int(_LAYOUT["char_edge_offset"])
BUBBLE_Y         = int(_LAYOUT["bubble_y"])

LIP_HZ  = 7
BOB_HZ  = 2.6
BOB_AMP = 4
DIM_GAIN = 0.7        # 非発話時の輝度(0.55→0.7、speaker切替時のフラッシュ軽減)
DIM_FADE_FRAMES = 6   # 発話開始/終了時に dim ↔ bright を線形補間する遷移フレーム数(0.2秒)

BGM_VOLUME = 0.10  # 既定の BGM 音量。news は _resolve_bgm_volume() で cut単位 override 可


def _char_scaled_w(orig_w: int, orig_h: int) -> int:
    return int(orig_w * CHAR_TARGET_H / orig_h)


# speaker → prescale 後の幅 / 表示側 / 左右反転(すべて _LAYOUT から導出)
SPEAKER_W: dict[str, int] = {
    sp: _char_scaled_w(int(c["orig"][0]), int(c["orig"][1]))
    for sp, c in _LAYOUT["speakers"].items()
}
_DEFAULT_SPEAKER_W = next(iter(SPEAKER_W.values()))

EXPRESSIONS = ["normal", "joy", "surprise", "worry", "fear", "sad", "neutral", "smile", "serious"]

# ── PATH 設定 ──────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))


def _resolve_bgm_volume(channel: str, cut_id: str | None) -> float:
    """news cut の BGM volume を channel.yaml から解決(news 例外: 0.10デフォルトを cut 単位で override 可)。

    既定の BGM 音量は全体共通だが、news 系では cut 単位で override 可能
    (うるさい/小さい問題への対応)。channel.yaml の `cuts[i].bgm.volume` を読む。
    無ければ BGM_VOLUME(0.10)を返す。
    """
    if not cut_id:
        return BGM_VOLUME
    try:
        ch_yaml = _ROOT / "channels" / channel / "channel.yaml"
        import yaml as _yaml
        cfg = _yaml.safe_load(ch_yaml.read_text(encoding="utf-8")) or {}
        for ct in cfg.get("content_types", []):
            if ct.get("id") == "news":
                for cut in ct.get("cuts", []):
                    if cut.get("id") == cut_id:
                        # cut.bgm.volume(ネスト)を優先、後方互換で cut.volume も見る
                        bgm_cfg = cut.get("bgm", {}) or {}
                        v = bgm_cfg.get("volume")
                        if v is None:
                            v = cut.get("volume")
                        if v is not None:
                            return float(v)
    except Exception:
        pass
    return BGM_VOLUME


def _resolve_bgm(
    channel: str,
    cut_id: str | None,
    _root_override: Path | None = None,
) -> str | None:
    """channel.yaml の content_types[id=news].cuts[id=<cut_id>].bgm.path を返す。

    BGM が設定されているのにファイルが存在しない場合はビルドを停止する。
    BGM 設定自体がない場合と旧スキーマ(content_types なし)では None を返す。

    Args:
        channel:        チャンネル名
        cut_id:         カット識別子
        _root_override: テスト用: プロジェクトルートを上書きするパス(通常は None)
    """
    root = _root_override or _ROOT
    if not cut_id:
        return None
    try:
        ch_yaml = root / "channels" / channel / "channel.yaml"
        import yaml as _yaml
        cfg = _yaml.safe_load(ch_yaml.read_text(encoding="utf-8")) or {}
        for ct in cfg.get("content_types", []):
            if ct.get("id") == "news":
                for cut in ct.get("cuts", []):
                    if cut.get("id") == cut_id:
                        bgm_cfg = cut.get("bgm", {})
                        bgm_path_str = bgm_cfg.get("path")
                        if bgm_path_str:
                            bgm_path = root / bgm_path_str
                            if not bgm_path.is_file() or bgm_path.stat().st_size == 0:
                                raise FileNotFoundError(
                                    "Configured news BGM is missing or empty: "
                                    f"channel={channel} cut={cut_id} path={bgm_path}"
                                )
                            return str(bgm_path)
    except FileNotFoundError:
        raise
    except Exception as exc:
        print(f"[compose] _resolve_bgm error: {exc}")
    return None


def _get_char_path(
    channel: str,
    speaker: str,
    expression: str,
    mouth: str,
    scaled_dir: Path,
) -> Path | None:
    """channel_resolver.character_path() 経由で立ち絵 PNG を解決。"""
    # scaled_dir から先に探す(prescale 済み)
    p = scaled_dir / f"{speaker}_{expression}_{mouth}.png"
    if p.exists():
        return p
    p = scaled_dir / f"{speaker}_normal_{mouth}.png"
    if p.exists():
        return p
    return None


def prescale_characters(
    channel: str,
    work_dir: Path,
) -> tuple[str, ...]:
    """チャンネル立ち絵を CHAR_TARGET_H にリサイズしてキャッシュ。hflip 指定の speaker は左右反転。"""
    scaled_dir = work_dir / "_prescale" / "characters_prescaled"
    scaled_dir.mkdir(parents=True, exist_ok=True)

    # 立ち絵パス解決: <channel> の characters/ 配下を最優先
    # 順序: channels/<ch>/assets/characters/<file> > channels/<ch>/assets/<file> >
    #       shared/characters/<file> > assets/<file>
    ch_chars = _ROOT / "channels" / channel / "assets" / "characters"
    ch_assets = _ROOT / "channels" / channel / "assets"
    shared_chars = _ROOT / "shared" / "characters"
    assets_dir = _ROOT / "assets"

    for speaker in _LAYOUT["speakers"]:
        sw = SPEAKER_W.get(speaker, _DEFAULT_SPEAKER_W)
        do_hflip = bool(_LAYOUT["speakers"][speaker].get("hflip", False))
        for expr in EXPRESSIONS:
            for mouth in ("close", "open"):
                dst = scaled_dir / f"{speaker}_{expr}_{mouth}.png"
                src = None
                for adir in (ch_chars, ch_assets, shared_chars, assets_dir):
                    candidate = adir / f"{speaker}_{expr}_{mouth}.png"
                    if candidate.exists():
                        src = candidate
                        break
                if src is None:
                    continue
                # 元素材の復旧・差し替え後も古い prescale を使い続けない。
                # 以前は dst の存在だけでスキップしていたため、channel-xx 素材が
                # 一時欠落した際に fallback 素材が永続的に残ってしまっていた。
                if dst.exists() and dst.stat().st_mtime_ns >= src.stat().st_mtime_ns:
                    continue
                img = Image.open(src).resize((sw, CHAR_TARGET_H), Image.LANCZOS)
                if do_hflip:
                    img = img.transpose(Image.FLIP_LEFT_RIGHT)
                img.save(dst)

    return (str(scaled_dir),)


def _char_path_scaled(scaled_dir: Path, speaker: str, expression: str, mouth: str) -> Path | None:
    p = scaled_dir / f"{speaker}_{expression}_{mouth}.png"
    if p.exists():
        return p
    p = scaled_dir / f"{speaker}_normal_{mouth}.png"
    if p.exists():
        return p
    return None


def _quantize_frames(duration_sec: float, fps: int = FPS) -> int:
    """duration_sec をフレーム数に変換(整数丸め)。"""
    return int(round(duration_sec * fps))


def line_frame_intervals(lines: list[dict], fps: int = FPS) -> list[tuple[dict, int, int]]:
    """script.json の各行を同じ half-open フレーム区間へ正規化する。

    ``[start_frame, end_frame)`` は audio の ``start_sec`` と同じ行を
    bubble / portrait が参照するための唯一のフレーム境界である。
    end を含めないことで、次行の開始フレームを前行が上書きしない。
    """
    intervals: list[tuple[dict, int, int]] = []
    for ln in lines:
        start_frame = _quantize_frames(float(ln["start_sec"]), fps)
        end_frame = _quantize_frames(
            float(ln["start_sec"]) + float(ln["duration_sec"]), fps
        )
        intervals.append((ln, start_frame, max(start_frame, end_frame)))
    return intervals


def _resolve_char_image(scaled_dir: Path, speaker: str, expr: str, mouth: str) -> Image.Image | None:
    """scaled prescale から (speaker, expr, mouth) に対応する PNG を返す。fallback: normal。"""
    p = _char_path_scaled(scaled_dir, speaker, expr, mouth)
    if p is None:
        return None
    return Image.open(p).convert("RGBA")


def build_bubble_track_news(
    script_data: dict,
    bubble_dir: Path,
    out_mov: Path,
    fps: int = FPS,
) -> None:
    """News 用 bubble Pillow-pipe トラック生成。

    BUBBLE_Y は top-left Y 規約(news/Shorts と同一)。本編の中心規約と異なる。
    line_idx は整数フレームで管理(dim 穴バグ予防、教訓B)。

    各 line は ``line_frame_intervals()`` の ``[start, end)`` に表示する。
    """
    lines: list[dict] = script_data.get("lines", [])
    total_sec = float(script_data.get("duration_sec", 0.0)) + 0.5
    total_frames = int(round(total_sec * fps))

    # フレーム → bubble_png のマッピング構築
    frame_bubble: dict[int, Path | None] = {}
    for ln, start_frame, end_frame in line_frame_intervals(lines, fps):
        bubble_path = bubble_dir / f"bubble_{ln['id']}.png"
        if not bubble_path.exists():
            print(f"  [bubble_track_news] WARN: {bubble_path.name} not found")
            continue
        for f in range(start_frame, min(end_frame, total_frames)):
            frame_bubble[f] = bubble_path

    # ffmpeg Pillow-pipe: rawvideo RGBA → qtrle ARGB .mov
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{W}x{H}", "-r", str(fps),
        "-i", "pipe:0",
        "-c:v", "qtrle", "-pix_fmt", "argb",
        str(out_mov),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    blank = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cache: dict[Path, Image.Image] = {}

    for fi in range(total_frames):
        bpath = frame_bubble.get(fi)
        if bpath is not None:
            if bpath not in cache:
                cache[bpath] = Image.open(bpath).convert("RGBA")
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            bub = cache[bpath]
            bx = (W - bub.width) // 2
            by = BUBBLE_Y
            img.paste(bub, (bx, by), bub)
        else:
            img = blank

        if fi % 100 == 0:
            print(f"  [bubble_track_news] frame {fi}/{total_frames}")

        proc.stdin.write(img.tobytes())  # type: ignore[union-attr]

    # 教訓A: stdin.close() → wait() → stderr.read()
    proc.stdin.close()  # type: ignore[union-attr]
    rc = proc.wait()
    err = proc.stderr.read().decode("utf-8", errors="replace")  # type: ignore[union-attr]
    if rc != 0:
        raise RuntimeError(f"build_bubble_track_news ffmpeg failed (rc={rc})\n{err[-2000:]}")
    print(f"  [bubble_track_news] done: {total_frames} frames -> {out_mov.name}")


# 立ち絵左右配置マップ(speaker → side)
_SPEAKER_SIDE: dict[str, str] = {
    sp: str(c.get("side", "right")) for sp, c in _LAYOUT["speakers"].items()
}


def build_portrait_track_news(
    script_data: dict,
    scaled_dir: Path,
    out_mov: Path,
    fps: int = FPS,
) -> None:
    """News 用 portrait Pillow-pipe トラック生成。

    現行挙動維持: BOB(縦揺れ)のみ、x揺れ・surprise 跳ね等は実装しない。

    教訓 B: line_idx 整数フレーム管理、t_abs 境界比較は使わない。
    教訓 C: CHAR_Y_BASE は top-left Y 規約、`y = CHAR_Y_BASE + dy` でそのまま使用。
    教訓 A: subprocess は wait()+stderr.read() パターン。
    教訓 x座標: right は W - sw - CHAR_EDGE_OFFSET(abs 使わない)。
    """
    lines: list[dict] = script_data.get("lines", [])
    total_sec = float(script_data.get("duration_sec", 0.0)) + 0.5
    total_frames = int(round(total_sec * fps))

    # ── 各フレームで各 speaker がどの状態かを事前計算 ─────────────────────────
    # speaker → [(start_frame, end_frame, expression)]
    speak_intervals: dict[str, list[tuple[int, int, str]]] = {}
    for ln, sf, ef in line_frame_intervals(lines, fps):
        sp = ln["speaker"]
        expr = ln.get("expression", "normal")
        speak_intervals.setdefault(sp, []).append((sf, ef, expr))

    all_speakers = list({ln["speaker"] for ln in lines})

    def _load(speaker: str, expr: str, mouth: str) -> Image.Image | None:
        p = _char_path_scaled(scaled_dir, speaker, expr, mouth)
        if p is None:
            return None
        return Image.open(p).convert("RGBA")

    def _load_dim(speaker: str, gain: float = DIM_GAIN) -> Image.Image | None:
        p = _char_path_scaled(scaled_dir, speaker, "normal", "close")
        if p is None:
            return None
        img = Image.open(p).convert("RGBA")
        # DIM_GAIN 適用: PIL で輝度を落とす
        import PIL.ImageEnhance
        return PIL.ImageEnhance.Brightness(img).enhance(gain)

    dim_cache: dict[str, Image.Image | None] = {sp: _load_dim(sp) for sp in all_speakers}
    img_cache: dict[tuple, Image.Image | None] = {}
    # フェード途中の中間輝度キャッシュ(speaker, expr, mouth, round(gain,2))
    fade_cache: dict[tuple, Image.Image | None] = {}

    def get_at_gain(speaker: str, expr: str, mouth: str, gain: float) -> Image.Image | None:
        """指定 gain(0.0-1.0)で発話画像の輝度を補間。1.0 で素通し。"""
        if gain >= 0.99:
            return get_img(speaker, expr, mouth)
        key = (speaker, expr, mouth, round(gain, 2))
        if key in fade_cache:
            return fade_cache[key]
        base = get_img(speaker, expr, mouth)
        if base is None:
            fade_cache[key] = None
            return None
        import PIL.ImageEnhance
        fade_cache[key] = PIL.ImageEnhance.Brightness(base).enhance(gain)
        return fade_cache[key]

    def get_img(speaker: str, expr: str, mouth: str) -> Image.Image | None:
        key = (speaker, expr, mouth)
        if key not in img_cache:
            img_cache[key] = _load(speaker, expr, mouth)
        return img_cache[key]

    def _paste(canvas: Image.Image, img: Image.Image | None, speaker: str) -> None:
        if img is None:
            return
        sw = img.width
        if _SPEAKER_SIDE.get(speaker, "right") == "left":
            x = CHAR_EDGE_OFFSET
        else:
            x = W - sw - CHAR_EDGE_OFFSET
        canvas.paste(img, (x, CHAR_Y_BASE), img)

    # ffmpeg Pillow-pipe
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba",
        "-s", f"{W}x{H}", "-r", str(fps),
        "-i", "pipe:0",
        "-c:v", "qtrle", "-pix_fmt", "argb",
        str(out_mov),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    for fi in range(total_frames):
        t = fi / fps
        # BOB
        dy = int(BOB_AMP * math.sin(2 * math.pi * t * BOB_HZ))
        # LIP: close=0, open=1 を交互
        lip_open = (int(t * LIP_HZ) % 2) == 1

        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        for sp in all_speakers:
            # このフレームで喋っているか確認 + 直近の発話区間との距離(フェード補間用)
            speaking = False
            current_expr = "normal"
            # 各発話区間との距離 d_in (区間開始からの経過 frame 数, 0=開始フレーム)
            #                    d_out (区間終了からの経過 frame 数, 0=終了直後)
            d_in_min = None
            d_out_min = None
            cur_expr_in = "normal"
            for sf, ef, expr in speak_intervals.get(sp, []):
                if sf <= fi < ef:
                    speaking = True
                    current_expr = expr
                    # 区間内: 区間開始からの距離(フェードイン用)
                    d_in_min = fi - sf
                    break
                if fi >= ef:
                    d = fi - ef
                    if d_out_min is None or d < d_out_min:
                        d_out_min = d
                if fi < sf:
                    d = sf - fi
                    if d_in_min is None or d < d_in_min:
                        d_in_min = d
                        cur_expr_in = expr

            # 立ち絵 X 座標
            if _SPEAKER_SIDE.get(sp, "right") == "left":
                base_img_for_x = get_img(sp, current_expr, "close") or dim_cache.get(sp)
                if base_img_for_x is None:
                    continue
                x = CHAR_EDGE_OFFSET
            else:
                base_img_for_x = get_img(sp, current_expr, "close") or dim_cache.get(sp)
                if base_img_for_x is None:
                    continue
                x = W - base_img_for_x.width - CHAR_EDGE_OFFSET

            if speaking:
                mouth = "open" if lip_open else "close"
                # 区間冒頭の DIM_FADE_FRAMES だけ dim → bright にフェードイン
                if d_in_min is not None and d_in_min < DIM_FADE_FRAMES:
                    t_in = (d_in_min + 1) / DIM_FADE_FRAMES  # 0〜1
                    gain = DIM_GAIN + (1.0 - DIM_GAIN) * t_in
                    img = get_at_gain(sp, current_expr, mouth, gain)
                else:
                    img = get_img(sp, current_expr, mouth)
                if img is None:
                    img = get_img(sp, "normal", mouth)
                if img is not None:
                    y = CHAR_Y_BASE + dy
                    canvas.paste(img, (x, y), img)
            else:
                # 非発話: 区間終了直後の DIM_FADE_FRAMES は bright → dim にフェードアウト
                # 区間開始直前の DIM_FADE_FRAMES は dim → bright のフェードイン助走
                gain = DIM_GAIN
                if d_out_min is not None and d_out_min < DIM_FADE_FRAMES:
                    # 直近に発話終了 → bright(1.0) → DIM_GAIN へ線形
                    t_out = (d_out_min + 1) / DIM_FADE_FRAMES
                    gain = 1.0 - (1.0 - DIM_GAIN) * t_out
                if gain >= 0.99:
                    img = get_img(sp, "normal", "close")
                else:
                    img = get_at_gain(sp, "normal", "close", gain)
                if img is not None:
                    canvas.paste(img, (x, CHAR_Y_BASE), img)

        if fi % 100 == 0:
            print(f"  [portrait_track_news] frame {fi}/{total_frames}")

        proc.stdin.write(canvas.tobytes())  # type: ignore[union-attr]

    # 教訓A
    proc.stdin.close()  # type: ignore[union-attr]
    rc = proc.wait()
    err = proc.stderr.read().decode("utf-8", errors="replace")  # type: ignore[union-attr]
    if rc != 0:
        raise RuntimeError(f"build_portrait_track_news ffmpeg failed (rc={rc})\n{err[-2000:]}")
    print(f"  [portrait_track_news] done: {total_frames} frames -> {out_mov.name}")


def compose_news(
    script_path: Path,
    remotion_mp4: Path,
    bubble_dir: Path,
    voice_dir: Path,
    out_mp4: Path,
    channel: str = "channel-xx",
    work_dir: Path | None = None,
    cut_id: str | None = None,
) -> None:
    """script.json + Remotion MP4 + 吹き出し + WAV を ffmpeg で合成して output.mp4 を生成。

    Pillow-pipe 2トラック方式:
      1. prescale_characters() で立ち絵を prescale
      2. build_portrait_track_news() で portrait_track_news.mov 生成
      3. build_bubble_track_news() で bubble_track_news.mov 生成
      4. ffmpeg: remotion + portrait_track + bubble_track + voice*N (+ bgm)
      5. filter_complex: [bg][portrait]overlay → [v_pt][bubble]overlay → [vout]

    Args:
        script_path:  script.json のパス
        remotion_mp4: Remotion が生成した背景 MP4
        bubble_dir:   bubble_<id>.png が入ったディレクトリ
        voice_dir:    <lid>.wav が入ったディレクトリ
        out_mp4:      出力先 MP4 パス
        channel:      チャンネル名(立ち絵解決に使用)
        work_dir:     prescale キャッシュ置き場(省略時は out_mp4 の兄弟ディレクトリ)
        cut_id:       カット識別子(BGM 解決に使用。None の場合は BGM なし)
    """
    if work_dir is None:
        work_dir = out_mp4.parent / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    script_data = json.loads(script_path.read_text(encoding="utf-8"))
    lines: list[dict] = script_data.get("lines", [])
    total_sec = float(script_data.get("duration_sec", 0.0)) + 0.5

    # ── Step 1: prescale ────────────────────────────────────────────────────────
    scaled_dir_tuple = prescale_characters(channel, work_dir)
    scaled_dir = Path(scaled_dir_tuple[0])

    # ── Step 2: portrait track ──────────────────────────────────────────────────
    portrait_mov = work_dir / "portrait_track_news.mov"
    build_portrait_track_news(script_data, scaled_dir, portrait_mov)

    # ── Step 3: bubble track ───────────────────────────────────────────────────
    bubble_mov = work_dir / "bubble_track_news.mov"
    build_bubble_track_news(script_data, bubble_dir, bubble_mov)

    # ── Step 4: ffmpeg compose ─────────────────────────────────────────────────
    # inputs: [0]=remotion(末尾バッファは tpad で最終フレーム保持), [1]=portrait_mov, [2]=bubble_mov,
    #         [3..N]=WAVs, [N+1]=BGM(optional)
    # 旧来は -stream_loop -1 で先頭 frame にループ → 末尾バッファ 0.5s で INTRO 風が一瞬出る事故。
    # 修正: tpad=stop_mode=clone で最終フレームを duplicate して詰める。
    ffmpeg_inputs: list[str] = []
    ffmpeg_inputs += ["-i", str(remotion_mp4)]                         # [0]
    ffmpeg_inputs += ["-i", str(portrait_mov)]                         # [1]
    ffmpeg_inputs += ["-i", str(bubble_mov)]                           # [2]

    # WAV inputs [3..]
    # speech は audio_master.pre_bake_loudnorm() で -16 LUFS に正規化
    # 各 line WAV を 2-pass loudnorm し <id>_norm_i16.wav として feed
    for ln in lines:
        wav_path = voice_dir / f"{ln['id']}.wav"
        norm_path = pre_bake_loudnorm(wav_path)
        ffmpeg_inputs += ["-i", str(norm_path)]

    bgm_path = _resolve_bgm(channel, cut_id)
    bgm_vol = _resolve_bgm_volume(channel, cut_id)

    # filter_complex: 2段固定 overlay
    fc_parts: list[str] = []
    # remotion 動画は script.duration_sec で終わるが、portrait/bubble/audio は +0.5s の余韻バッファを持つ。
    # 旧来 -stream_loop で先頭 frame にループ → 末尾に INTRO が一瞬映る事故。
    # 修正: tpad=stop_mode=clone で remotion 最終 frame を total_sec まで複製
    fc_parts.append(f"[0:v]tpad=stop_mode=clone:stop_duration={0.5 + 0.1}[v_bg]")
    # overlay 1: bg + portrait
    fc_parts.append("[v_bg][1:v]overlay=0:0:format=auto[v_pt]")
    # overlay 2: v_pt + bubble
    fc_parts.append("[v_pt][2:v]overlay=0:0:format=auto[vout]")

    # 音声: WAVs ミックス
    n_lines = len(lines)
    base_audio_idx = 3
    audio_mix_parts: list[str] = []
    for i, ln in enumerate(lines):
        idx = base_audio_idx + i
        delay_ms = int(round(float(ln["start_sec"]) * 1000))
        if delay_ms > 0:
            audio_mix_parts.append(
                f"[{idx}:a]aresample=48000,adelay={delay_ms}|{delay_ms}[a_{ln['id']}]"
            )
        else:
            audio_mix_parts.append(
                f"[{idx}:a]aresample=48000,asetpts=PTS-STARTPTS[a_{ln['id']}]"
            )
    fc_parts.extend(audio_mix_parts)

    speech_inputs = "".join(f"[a_{ln['id']}]" for ln in lines)
    if n_lines > 0:
        fc_parts.append(
            f"{speech_inputs}amix=inputs={n_lines}:dropout_transition=0:normalize=0,"
            f"atrim=0:{total_sec},asetpts=N/SR/TB[speech]"
        )
    else:
        fc_parts.append(
            f"aevalsrc=0:duration={total_sec}[speech]"
        )

    if bgm_path:
        bgm_idx = base_audio_idx + n_lines
        ffmpeg_inputs += ["-i", str(bgm_path)]
        # speech の line WAV は mono。amix の出力レイアウトが先頭入力の mono に
        # 固定されると、stereo BGM が downmix されて BGM presence QA で検出不能に
        # なるため、BGM 合成時だけ speech を明示的に stereo 化する。
        fc_parts.append("[speech]aformat=channel_layouts=stereo[speech_stereo]")
        fc_parts.append(
            f"[{bgm_idx}:a]aloop=loop=-1:size=2000000000,aresample=48000,volume={bgm_vol},"
            f"atrim=0:{total_sec},asetpts=N/SR/TB[bgm_v]"
        )
        # alimiter limit=0.95 で最終ピーク制御(audio_master 規格)
        fc_parts.append(
            "[speech_stereo][bgm_v]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[aout]"
        )
        print(f"  [compose] BGM: {Path(bgm_path).name} vol={bgm_vol}")
    else:
        # alimiter limit=0.95 で最終ピーク制御(audio_master 規格)
        fc_parts.append("[speech]asetpts=PTS-STARTPTS,alimiter=limit=0.95[aout]")

    fc = "; ".join(fc_parts)

    # 50段ガード(回帰防止)
    overlay_count = sum(1 for p in fc_parts if "overlay" in p)
    if overlay_count > 50:
        warnings.warn(f"  [compose] WARNING: overlay chain={overlay_count} > 50, ffmpeg may fail")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = ["ffmpeg", "-y"] + ffmpeg_inputs
    cmd += ["-filter_complex", fc]
    cmd += ["-map", "[vout]", "-map", "[aout]"]
    # ★encoder 名は固定せず、hw_video_encoder() が実行環境で使えるものを返す
    #   (存在しない encoder を指定すると ffmpeg が `Unknown encoder` で落ちるため)。
    cmd += ["-c:v", hw_video_encoder(), "-b:v", "6M"]
    cmd += ["-r", str(FPS), "-t", str(total_sec)]
    cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [str(out_mp4)]

    print(
        f"compose: {script_path.name} -> {out_mp4.name}"
        f"  inputs={len(ffmpeg_inputs)//2} parts={len(fc_parts)} dur={total_sec}s"
        f" overlay={overlay_count}"
    )

    # ★encoding を明示する 。text=True だけだと Windows は既定の cp932 で
    #   読もうとし、ffmpeg が出す UTF-8 のバイトで UnicodeDecodeError になる。
    #   しかも例外は出力読み取りスレッドの中で起きるので run() は素通りし、
    #   result.stderr が None のまま返る → 次行の .splitlines() が
    #   「'NoneType' object has no attribute 'splitlines'」になって
    #   **本当の失敗原因が握りつぶされる**。errors="replace" で読めない字は捨てる。
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print("FAILED")
        for line in (result.stderr or "").splitlines()[-20:]:
            print(line)
        raise RuntimeError(f"ffmpeg failed (rc={result.returncode})\n{result.stderr[-2000:]}")

    size_mb = out_mp4.stat().st_size / 1024 / 1024
    print(f"SUCCESS: {out_mp4}  ({size_mb:.1f}MB)")
