"""channel-xx ニュースパイプライン フックイメージ生成モジュール。

generate_hook_image(news_config_path, work_dir, force=False) -> Optional[Path]

news_config.json の topics[0].hook_image フィールドが存在する場合に限り、
画像編集 API(edits)を呼んで hook.png を生成して返す。
フィールドがなければ None を返す(後方互換)。

mood(場面の雰囲気)と参照画像の対応は設定ファイルで渡す。
  環境変数 NEWS_HOOK_CONFIG に JSON ファイルのパスを指定する(下の DEFAULT_CONFIG と同じ形)。
  未指定なら、色やモチーフを持たない中立なサンプル 2 件を使う。
参照画像の命名規則: ``<speaker>_<emotion>_open.png``(speaker は設定の left_speaker / right_speaker)。
参照画像は、AI への入力が利用規約で許されている自作または許諾済みの素材に限る。

CLI:
    python -m news_pipeline.hook_image --cut <cut> --date <date> --channel channel-xx [--force]
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from _dotenv import load_dotenv as _load_dotenv  # noqa: E402  共通ローダー
from openai_image_config import (  # noqa: E402
    DEFAULT_IMAGE_MODEL,
    DEFAULT_IMAGE_QUALITY,
    validate_image_defaults,
)

logger = logging.getLogger(__name__)

# ── 設定(中立なサンプル値。実運用の値は NEWS_HOOK_CONFIG の JSON で渡す) ──────

CHAR_BASE_DIR = ROOT / "channels" / "channel-xx" / "assets" / "characters"

DEFAULT_CONFIG: dict = {
    "left_speaker": "speaker_a",
    "right_speaker": "speaker_b",
    # 出力枠(px)。合成側の枠サイズに合わせる
    "frame_size": [960, 720],
    # 参照画像の外見をそのまま保つ汎用文言
    "left_description": "LEFT character: keep the outfit and hairstyle of the first reference image.",
    "right_description": "RIGHT character: keep the outfit and hairstyle of the second reference image.",
    "style": "simple flat illustration, plain light background",
    # mood 名 -> {left / right: 表情名, scene: 背景・小物の短い英語説明}
    "moods": {
        "neutral": {"left": "normal", "right": "normal", "scene": "plain background with a simple chart"},
        "alert": {"left": "surprise", "right": "surprise", "scene": "plain background with a simple warning icon"},
    },
}

DEFAULT_MOOD = "neutral"


def load_config() -> dict:
    """NEWS_HOOK_CONFIG(JSON)を既定値へマージして返す。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = os.environ.get("NEWS_HOOK_CONFIG", "")
    if path:
        try:
            user = json.loads(Path(path).read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in user.items() if k != "moods"})
            if user.get("moods"):
                cfg["moods"] = user["moods"]
        except Exception as exc:  # 設定が壊れていても既定値で続行
            logger.warning("[hook_image] 設定 %s の読み込み失敗: %s", path, exc)
    return cfg


# ── プロンプト構築 ────────────────────────────────────────────────────────────

def _build_prompt(cfg: dict, mood_key: str, subject: str, metric: str) -> str:
    """画像編集 API 用プロンプトを構築して返す。"""
    mood = cfg["moods"].get(mood_key) or cfg["moods"][DEFAULT_MOOD if DEFAULT_MOOD in cfg["moods"] else next(iter(cfg["moods"]))]
    w, h = cfg["frame_size"]

    prompt = f"""Redraw the two characters as in the reference images.

{cfg["left_description"]}
{cfg["right_description"]}

Style: {cfg["style"]}.

Composition: landscape frame. The two characters stand side by side in the lower-center area. Background: {mood["scene"]}. Show the text "{subject}" and "{metric}" in the upper-center area.

Safe area: the final image is shown in a {w}x{h} frame. Keep all text and both characters inside the center 70% of the image, with no cropping at the edges."""

    return prompt


# ── multipart/form-data 送信ユーティリティ ───────────────────────────────────

def _call_edits_api(
    api_key: str,
    prompt: str,
    left_bytes: bytes,
    right_bytes: bytes,
) -> bytes:
    """画像編集 API(edits)を呼び出し、PNG バイナリを返す。

    Raises:
        urllib.error.HTTPError: HTTP エラー発生時
        ValueError: レスポンスに data が含まれない場合
    """
    boundary = f"----boundary{uuid.uuid4().hex}"

    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    def add_file(name: str, filename: str, content: bytes) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'.encode()
            + content
            + b"\r\n"
        )

    add_field("model", DEFAULT_IMAGE_MODEL)
    add_field("prompt", prompt)
    # 枠の比率に近いサポートサイズを選ぶ
    add_field("size", "1536x1024")
    add_field("n", "1")
    validate_image_defaults(DEFAULT_IMAGE_MODEL, DEFAULT_IMAGE_QUALITY)
    add_field("quality", DEFAULT_IMAGE_QUALITY)
    add_file("image[]", "char_left.png", left_bytes)
    add_file("image[]", "char_right.png", right_bytes)
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)

    req = urllib.request.Request(
        "https://api.openai.com/v1/images/edits",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode())

    if "error" in data:
        raise ValueError(f"API error: {data['error']}")

    return base64.b64decode(data["data"][0]["b64_json"])


# ── メイン公開 API ────────────────────────────────────────────────────────────

def generate_hook_image(
    news_config_path: Path,
    work_dir: Path,
    force: bool = False,
) -> Optional[Path]:
    """フックイメージを生成して hook.png のパスを返す。

    news_config.json の topics[0].hook_image フィールドが存在しない場合は
    None を返す(後方互換)。

    Args:
        news_config_path: news_config.json の絶対パス
        work_dir:         出力先ディレクトリ(hook.png を書き出す)
        force:            True の場合、既存 hook.png を上書き再生成

    Returns:
        生成した hook.png の Path、または None(スキップ / エラー時)
    """
    # .env ロード
    _load_dotenv()

    # news_config.json 読み込み
    try:
        news_config = json.loads(news_config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[hook_image] news_config.json 読み込み失敗: %s", exc)
        return None

    topics = news_config.get("topics", [])
    if not topics:
        logger.warning("[hook_image] topics が空 → スキップ")
        return None

    hook_cfg = topics[0].get("hook_image")
    if not hook_cfg:
        # hook_image フィールドなし → 後方互換で None 返却
        return None

    out_path = work_dir / "hook.png"

    # skip チェック
    if out_path.exists() and not force:
        logger.info("[hook_image] 既存 hook.png あり → スキップ (force=False)")
        return out_path

    # OPENAI_API_KEY チェック
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        logger.warning("[hook_image] OPENAI_API_KEY が未設定 → hook_image スキップ")
        return None

    cfg = load_config()

    # mood / subject / metric 抽出
    mood_key = hook_cfg.get("mood", DEFAULT_MOOD)
    subject  = hook_cfg.get("subject", "")
    metric   = hook_cfg.get("metric", "")

    if mood_key not in cfg["moods"]:
        fallback = DEFAULT_MOOD if DEFAULT_MOOD in cfg["moods"] else next(iter(cfg["moods"]))
        logger.warning("[hook_image] 未知の mood '%s' → '%s' にフォールバック", mood_key, fallback)
        mood_key = fallback

    mood = cfg["moods"][mood_key]

    # 参照画像パス解決: <speaker>_<emotion>_open.png
    left_path = CHAR_BASE_DIR / f"{cfg['left_speaker']}_{mood['left']}_open.png"
    right_path = CHAR_BASE_DIR / f"{cfg['right_speaker']}_{mood['right']}_open.png"

    for p in (left_path, right_path):
        if not p.exists():
            logger.warning("[hook_image] 参照画像が見つかりません: %s → スキップ", p)
            return None

    # プロンプト構築
    prompt = _build_prompt(cfg, mood_key, subject, metric)
    logger.info("[hook_image] mood=%s subject=%s metric=%s", mood_key, subject, metric)

    # API 呼び出し
    try:
        left_bytes = left_path.read_bytes()
        right_bytes = right_path.read_bytes()

        print(f"[hook_image] 画像編集 API 呼び出し中 (mood={mood_key}) ...", flush=True)
        png_bytes = _call_edits_api(api_key, prompt, left_bytes, right_bytes)

    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:600]
        logger.warning("[hook_image] API HTTPError %s: %s → hook なしで続行", exc.code, body)
        return None
    except Exception as exc:
        logger.warning("[hook_image] API エラー: %s → hook なしで続行", exc)
        return None

    # 出力(設定の枠サイズへ contain 戦略でリサイズ)
    # cover ではなく contain(letterbox)— テキストが端で切れるのを防ぐ
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / "hook_raw.png"
    raw_path.write_bytes(png_bytes)  # デバッグ用 raw 保存
    try:
        from io import BytesIO
        from PIL import Image
        WIPE_W, WIPE_H = (int(v) for v in cfg["frame_size"])
        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        sw, sh = img.size
        target_ratio = WIPE_W / WIPE_H
        src_ratio    = sw / sh
        # contain 戦略: 元画像が枠に収まるように同比率縮小、余白は背景色で埋める
        if src_ratio > target_ratio:
            new_w = WIPE_W
            new_h = int(WIPE_W / src_ratio)
        else:
            new_h = WIPE_H
            new_w = int(WIPE_H * src_ratio)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        # 背景色は左上1pxを採用
        bg = img.getpixel((0, 0))
        canvas = Image.new("RGB", (WIPE_W, WIPE_H), bg)
        x0 = (WIPE_W - new_w) // 2
        y0 = (WIPE_H - new_h) // 2
        canvas.paste(resized, (x0, y0))
        canvas.save(out_path, format="PNG")
        print(f"[hook_image] hook.png 生成完了 ({WIPE_W}x{WIPE_H}, contain): {out_path}", flush=True)
    except Exception as exc:
        out_path.write_bytes(png_bytes)
        logger.warning("[hook_image] Pillow リサイズ失敗、raw PNG を保存: %s", exc)
    return out_path


# ── CLI エントリポイント ──────────────────────────────────────────────────────

def _cli() -> None:
    import argparse
    from datetime import date

    parser = argparse.ArgumentParser(description="channel-xx フックイメージ生成")
    parser.add_argument("--cut",     required=True, help="カット識別子(例: cut_a)")
    parser.add_argument("--date",    required=True, help="配信日 YYYY-MM-DD")
    parser.add_argument("--channel", default="channel-xx", help="チャンネル名")
    parser.add_argument("--force",   action="store_true", help="既存ファイルを上書き再生成")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cut_date = date.fromisoformat(args.date)
    work_dir = ROOT / "channels" / args.channel / "videos" / "news" / str(cut_date) / args.cut
    nc_path  = work_dir / "news_config.json"

    if not nc_path.exists():
        print(f"[hook_image] ERROR: news_config.json が見つかりません: {nc_path}")
        sys.exit(1)

    result = generate_hook_image(nc_path, work_dir, force=args.force)
    if result:
        print(f"[hook_image] 完了: {result}")
    else:
        print("[hook_image] スキップまたはエラー(hook.png 未生成)")
        sys.exit(0)


if __name__ == "__main__":
    _cli()
