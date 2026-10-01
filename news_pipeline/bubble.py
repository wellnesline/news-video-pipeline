"""吹き出し PNG 生成。

変更点:
  - _build_color_map() を新設: channel_resolver.channel_config(channel) から
    characters.<role>.bubble_color を読み、フォールバックとして COLORS デフォルトを使用。
  - 固定パスのハードコードなし(引数で受け取る)。
  - build_all_bubbles() は script_path / out_dir / channel を受け取る signature に変更。
  - CANVAS_W は compose.py の W=1080 と揃える。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from _platform import resolve_font  # noqa: E402

# デフォルトカラーマップ(channel_resolver から取得できない場合のフォールバック)
COLORS: dict[str, dict] = {
    # 中立なサンプル値。実運用の色は channel 設定の characters.<role>.bubble_color で渡す
    "speaker_a": {"border": (0x60, 0x7d, 0x8b), "highlight": (0x37, 0x47, 0x4f), "tail_dir": "left"},
    "speaker_b": {"border": (0x8d, 0x6e, 0x63), "highlight": (0x5d, 0x40, 0x37), "tail_dir": "right"},
    "speaker_c": {"border": (0x7e, 0x57, 0xc2), "highlight": (0x4a, 0x14, 0x8c), "tail_dir": "left"},
}

WRAP_WIDTH   = 16
LINE_SPACING = 14
PADDING_X    = 36
PADDING_Y    = 28
BORDER_W     = 8
CANVAS_W     = 1080  # compose.py の W=1080 と揃える


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """'#112233' → (17, 34, 51)"""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _build_color_map(channel: str | None) -> dict[str, dict]:
    """channel_resolver から bubble_color を読んでカラーマップを構築。

    取得できない場合は COLORS デフォルトを返す。
    """
    if not channel:
        return COLORS

    try:
        ROOT = Path(__file__).resolve().parents[2]
        if str(ROOT / "src") not in sys.path:
            sys.path.insert(0, str(ROOT / "src"))
        from channel_resolver import channel_config
        cfg = channel_config(channel)
        chars = cfg.get("characters", {})
        result: dict[str, dict] = {}
        for role, char_cfg in chars.items():
            if "bubble_color" in char_cfg:
                bc = char_cfg["bubble_color"]
                border_rgb = _hex_to_rgb(bc.get("border", "#607d8b"))
                highlight_rgb = _hex_to_rgb(bc.get("highlight", "#37474f"))
                entry = {
                    "border": border_rgb,
                    "highlight": highlight_rgb,
                    "tail_dir": bc.get("tail_dir", "left"),
                }
                # opt-in ダークテーマ: body/text が指定された話者だけボディ色・本文色を上書き。
                # 未指定(channel-xx 等)は entry に入らない → render_bubble で従来の白ボディ+黒文字。
                if bc.get("body"):
                    entry["body"] = _hex_to_rgb(bc["body"])
                if bc.get("text"):
                    entry["text"] = _hex_to_rgb(bc["text"])
                result[role] = entry
        if result:
            # 既存デフォルトにマージ(取得できたロールのみ上書き)
            merged = dict(COLORS)
            merged.update(result)
            return merged
    except Exception:
        pass

    return COLORS


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(resolve_font("bold" if bold else "regular"), size)


def render_bubble(text: str, highlight: str, color_set: dict, max_lines: int | None = None) -> Image.Image:
    """吹き出し PIL Image を生成して返す。角丸+チャンネル色ボーダー。

    max_lines: 1吹き出しあたりの最大行数。指定があれば長文時に1行を広げて行数で頭打ち
    (channel.yaml bubble.max_lines 由来。追加)。
    """
    body_size, highlight_size = 38, 48
    f_body = font(body_size, bold=True)
    f_hi   = font(highlight_size, bold=True)

    # ダークテーマ(body 指定)時は本文色を明るい text 色に、ボディ塗りをダーク色にする。
    # 未指定なら従来どおり白ボディ(255,255,255,245)+黒文字(30,30,30)。
    body_fill = (*color_set["body"], 255) if color_set.get("body") else (255, 255, 255, 245)
    body_text_col = color_set.get("text", (30, 30, 30))

    segments: list[tuple[str, ImageFont.FreeTypeFont, tuple]] = []
    if highlight and highlight in text:
        i = text.index(highlight)
        if i > 0:
            segments.append((text[:i], f_body, body_text_col))
        segments.append((highlight, f_hi, color_set["highlight"]))
        if i + len(highlight) < len(text):
            segments.append((text[i + len(highlight):], f_body, body_text_col))
    else:
        segments.append((text, f_body, body_text_col))

    tokens: list[tuple[str, ImageFont.FreeTypeFont, tuple]] = []
    for seg_text, seg_font, seg_color in segments:
        for ch in seg_text:
            tokens.append((ch, seg_font, seg_color))

    n = len(tokens)
    if n <= WRAP_WIDTH:
        rows = [tokens]
    else:
        num_lines = max(2, (n + WRAP_WIDTH - 1) // WRAP_WIDTH)
        # 行数上限キャップ(channel.yaml bubble.max_lines)。長文時は1行を広げて行数で頭打ち
        if max_lines is not None and num_lines > max_lines:
            num_lines = max_lines
        target = (n + num_lines - 1) // num_lines
        rows = []
        idx = 0
        while idx < n:
            remaining_lines = num_lines - len(rows)
            remaining_chars = n - idx
            if remaining_lines <= 1:
                rows.append(tokens[idx:])
                break
            ideal = idx + max(target, remaining_chars // remaining_lines)
            best = ideal
            for delta in range(0, 4):
                for cand in (ideal - delta, ideal + delta):
                    if 0 < cand <= n:
                        prev_ch = tokens[cand - 1][0] if cand > 0 else ""
                        next_ch = tokens[cand][0] if cand < n else ""
                        if prev_ch in "、。" and next_ch not in "！？。、":
                            best = cand
                            break
                else:
                    continue
                break
            while best < n and tokens[best][0] in "！？。、":
                best += 1
            rows.append(tokens[idx:best])
            idx = best

    tmp = Image.new("RGBA", (10, 10))
    td = ImageDraw.Draw(tmp)

    def row_size(row):
        w = sum(td.textlength(c, font=fnt) for c, fnt, _ in row)
        h = max(fnt.size for _, fnt, _ in row) if row else body_size
        return int(w), int(h)

    sizes = [row_size(r) for r in rows]
    text_w = max(w for w, _ in sizes) if sizes else 0
    text_h = sum(h for _, h in sizes) + LINE_SPACING * max(0, len(rows) - 1)

    box_w = text_w + PADDING_X * 2
    box_h = text_h + PADDING_Y * 2

    SS = 2
    img_w = box_w
    img_h = box_h
    big = Image.new("RGBA", (img_w * SS, img_h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)

    radius = 28
    pts: list[tuple[float, float]] = []

    def arc(cx: float, cy: float, start_deg: float, end_deg: float, steps: int = 12):
        for i in range(steps + 1):
            t = i / steps
            a = math.radians(start_deg + (end_deg - start_deg) * t)
            pts.append((cx + math.cos(a) * radius, cy + math.sin(a) * radius))

    arc(radius, radius, 180, 270)
    arc(box_w - radius, radius, 270, 360)
    arc(box_w - radius, box_h - radius, 0, 90)
    arc(radius, box_h - radius, 90, 180)

    pts_ss = [(x * SS, y * SS) for x, y in pts]
    d.polygon(pts_ss, fill=body_fill)
    d.line(pts_ss + [pts_ss[0]], fill=color_set["border"], width=BORDER_W * SS, joint="curve")

    img = big.resize((img_w, img_h), Image.LANCZOS)
    d = ImageDraw.Draw(img)

    y = PADDING_Y
    for row, (rw, rh) in zip(rows, sizes):
        x = PADDING_X + (text_w - rw) // 2
        for ch, fnt, col in row:
            d.text((x, y + (rh - fnt.size) // 2), ch, font=fnt, fill=col)
            x += int(td.textlength(ch, font=fnt))
        y += rh + LINE_SPACING

    return img


def center_bubble_on_canvas(bubble: Image.Image, canvas_w: int = CANVAS_W) -> Image.Image:
    """bubble を canvas_w に中央配置した透過 PNG を返す。"""
    bw, bh = bubble.size
    if bw > canvas_w:
        return bubble  # フォールバック
    canvas = Image.new("RGBA", (canvas_w, bh), (0, 0, 0, 0))
    x_offset = (canvas_w - bw) // 2
    canvas.paste(bubble, (x_offset, 0), bubble)
    return canvas


def _resolve_max_lines(channel: str | None) -> int | None:
    """channel.yaml の bubble.max_lines を読み出す。未指定なら None(無制限)。"""
    if not channel:
        return None
    try:
        ROOT = Path(__file__).resolve().parents[2]
        if str(ROOT / "src") not in sys.path:
            sys.path.insert(0, str(ROOT / "src"))
        from channel_resolver import channel_config
        cfg = channel_config(channel)
        bubble_cfg = (cfg.get("bubble", {}) or {})
        ml = bubble_cfg.get("max_lines")
        return int(ml) if ml else None
    except Exception:
        return None


def build_all_bubbles(
    script_path: Path,
    bubble_dir: Path,
    channel: str = "channel-xx",
) -> None:
    """各 line に対し PIL で吹き出し PNG (bubble_<id>.png) を生成。

    キャラ別の highlight 色(COLORS または channel 設定)で highlight 字句を強調。
    canvas は (1080, h) で中央寄せ。
    """
    color_map = _build_color_map(channel)
    max_lines = _resolve_max_lines(channel)
    script = json.loads(script_path.read_text(encoding="utf-8"))
    bubble_dir.mkdir(parents=True, exist_ok=True)

    for line in script["lines"]:
        lid  = line["id"]
        spk  = line["speaker"]
        text = line["text"]
        hi   = line.get("highlight", "")
        cs   = color_map.get(spk, color_map.get("speaker_b", list(color_map.values())[0]))

        bubble = render_bubble(text, hi, cs, max_lines=max_lines)
        centered = center_bubble_on_canvas(bubble)
        out_path = bubble_dir / f"bubble_{lid}.png"
        centered.save(out_path)
        print(f"  {lid} {spk} {bubble.size} -> canvas{centered.size} -> {out_path.name}")
