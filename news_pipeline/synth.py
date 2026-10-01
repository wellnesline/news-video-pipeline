"""VOICEVOX WAV 合成。

変更点:
  - script_path / voice_dir は引数で受け取る(固定パス廃止)
  - GAP / TOPIC_GAP / TUNING は 既定値(設定で上書き可)。SPEAKER_ID は既定値なし(設定で渡す)
  - synth() / wavdur() は関数として export
  - reschedule() は NewsCut.lines を直接書き換えて duration_sec を返す
"""
from __future__ import annotations

import json
import hashlib
import re
import urllib.parse
import urllib.request
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

VOICEVOX = "http://127.0.0.1:50021"

# 話者 ID は既定値を持たない。使う VOICEVOX の話者 ID は、環境変数 NEWS_SYNTH_CONFIG の
# JSON({"speaker_id": {"speaker_a": <id>, ...}, "tuning": {...}})で渡す(未設定のまま合成すると
# RuntimeError)。調整値(TUNING)は中立なサンプル値。
# 使う音声ライブラリごとの利用条件(クレジット表記など)に従うこと。
SPEAKER_ID: dict[str, int] = {}

TUNING: dict[str, dict] = {
    "speaker_a": {"intonationScale": 1.0, "speedScale": 1.0, "pitchScale": 0.0},
    "speaker_b": {"intonationScale": 1.0, "speedScale": 1.0, "pitchScale": 0.0},
    "speaker_c": {"intonationScale": 1.0, "speedScale": 1.0, "pitchScale": 0.0},
}


def _apply_synth_config() -> None:
    import os
    path = os.environ.get("NEWS_SYNTH_CONFIG", "")
    if not path:
        return
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return
    SPEAKER_ID.update({k: int(v) for k, v in cfg.get("speaker_id", {}).items()})
    for k, v in cfg.get("tuning", {}).items():
        TUNING[k] = dict(v)


_apply_synth_config()

GAP = 0.12
TOPIC_GAP = 0.12


# 誤読対策: VOICEVOX に渡す前に置換するルール集(text 直接書き換え)
# 記号や略語は user_dict に登録しても文中の文字列マッチが効かないので前処理する。
# 表記順は「より長いキーから先に」マッチさせるため、長い順に並べる。
TEXT_REPLACEMENTS: list[tuple[str, str]] = [
    # 長いキーから先に置換(部分マッチ防止)
    # ⚠️ 一度置換した結果に再マッチして二重置換しないよう、出力にキーを含めない/または順序で吸収する
    #
    # 株価指数 — 完全一致優先で
    ("S&P500", "エスアンドピーゴヒャク"),
    ("SP500", "エスピーゴヒャク"),
    ("NASDAQ100", "ナスダックヒャク"),
    ("NASDAQ", "ナスダック"),
    ("FTSE100", "フッツィーヒャク"),
    ("FTSE", "フッツィー"),
    ("DAX40", "ダックスヨンジュウ"),
    ("DAX", "ダックス"),
    ("CAC40", "キャックヨンジュウ"),
    ("CSI300", "シーエスアイサンビャク"),
    ("MSCI", "エムエスシーアイ"),
    ("VIX", "ビックス"),
    ("KOSPI", "コスピ"),
    # ティッカー(銘柄略号)— 英字単独の誤読防止
    ("S&P", "エスアンドピー"),
    # 架空の銘柄の例(実運用では設定で渡す)
    ("ALFA", "アルファ"),
    ("BETA", "ベータ"),
    ("GMMA", "ガンマ"),
    # 中央銀行・規制機関
    ("FOMC", "エフオーエムシー"),
    ("FRB", "エフアールビー"),
    ("ECB", "イーシービー"),
    ("BOE", "ビーオーイー"),
    ("BOJ", "ビーオージェイ"),
    ("PBOC", "ピービーオーシー"),
    ("SEC", "エスイーシー"),
    ("CFTC", "シーエフティーシー"),
    # 経済指標
    ("CPI", "シーピーアイ"),
    ("PPI", "ピーピーアイ"),
    ("GDP", "ジーディーピー"),
    ("PMI", "ピーエムアイ"),
    ("ISM", "アイエスエム"),
    ("PCE", "ピーシーイー"),
    # 投資指標
    ("ROE", "アールオーイー"),
    ("ROA", "アールオーエー"),
    ("ROIC", "アールオーアイシー"),
    ("PER", "ピーイーアール"),
    ("PBR", "ピービーアール"),
    ("EPS", "イーピーエス"),
    ("DPS", "ディーピーエス"),
    ("EBITDA", "イービットディーエー"),
    # ファンド・商品
    ("ETF", "イーティーエフ"),
    ("REIT", "リート"),
    ("J-REIT", "ジェイリート"),
    ("IPO", "アイピーオー"),
    ("M&A", "エムアンドエー"),
    ("LBO", "エルビーオー"),
    ("MBO", "エムビーオー"),
    # 仮想通貨
    ("BTC", "ビットコイン"),
    ("ETH", "イーサリアム"),
    ("XRP", "リップル"),
    # 為替
    ("USD/JPY", "ドルえん"),
    ("EUR/JPY", "ユーロえん"),
    ("EUR/USD", "ユーロドル"),
    ("GBP/USD", "ポンドドル"),
    # 期間表記
    ("YoY", "ワイオーワイ"),
    ("QoQ", "キューオーキュー"),
    ("MoM", "エムオーエム"),
    ("FY", "エフワイ"),
    # ビジネス略語
    ("R&D", "アールアンドディー"),
    ("D&I", "ディーアンドアイ"),
    ("ESG", "イーエスジー"),
    ("DX", "ディーエックス"),
    ("AI", "エーアイ"),
    ("DC", "ディーシー"),
    ("EV", "イーブイ"),
    ("SaaS", "サース"),
    ("PaaS", "パース"),
    ("IaaS", "アイアース"),
    # 連結記号(上の固有略語に該当しなかった残り)
    ("&", "アンド"),
]


def _decimal_to_kana(num_str: str) -> str:
    """小数文字列(8.83 等)をカナに変換。

    VOICEVOX は文中の `8.83%` を「はち、はちじゅうさんパーセント」と
    切って読むことがあるため、preprocess で「ハチテンハチサン」のように
    一語化したカナに直してから渡す。

    処理ルール:
      - 整数部: 桁の数字をそのまま並べる(8 → ハチ、123 → ヒャクニジュウサン)
        ただし VOICEVOX が安定して読める範囲は素直に数値文字列のまま渡す方がよい
        ことが多いので、整数部はそのまま残し、小数点以降だけカナ化する形にする。
    """
    # 後述の正規表現で利用される実装本体は preprocess_text 側に持つ
    return num_str


# 数字 1 桁 → 読みカナ(小数点以降の 1 桁ずつ読みに使う)
_DIGIT_KANA = {
    "0": "ゼロ", "1": "イチ", "2": "ニ", "3": "サン", "4": "ヨン",
    "5": "ゴ", "6": "ロク", "7": "ナナ", "8": "ハチ", "9": "キュウ",
}


def _decimal_to_natural_kana(int_part: str, frac_part: str) -> str:
    """整数部はそのまま数字、小数部は数字を「テン<カナ>」に変換して返す。

    例:
      "8", "83" → "8テンハチサン"
      "1", "5"  → "1テンゴ"
      "0", "5"  → "0テンゴ"

    こうすると VOICEVOX は「8」を「ハチ」、「テンハチサン」を「テン・ハチ・サン」と
    安定して読み、文の途中で切られない。
    """
    frac_kana = "".join(_DIGIT_KANA.get(c, c) for c in frac_part)
    return f"{int_part}テン{frac_kana}"


ROOT = Path(__file__).resolve().parents[2]


def _channel_from_script_path(script_path: Path) -> str | None:
    """``channels/<channel>/.../script.json`` からチャンネル名を安全に得る。"""
    for parent in script_path.resolve().parents:
        if parent.parent.name == "channels":
            return parent.name
    return None


@lru_cache(maxsize=None)
def _load_reading_dict(channel: str) -> dict[str, Any]:
    """チャンネル固有の読み辞書を読む。存在しないチャンネルは空辞書。"""
    path = ROOT / "channels" / channel / "reading_dict.yaml"
    if not path.exists():
        return {}
    data = YAML(typ="safe").load(path) or {}
    if not isinstance(data, dict) or data.get("channel") != channel:
        raise ValueError(f"Invalid reading dictionary: {path}")
    return data


def _channel_replacements(channel: str | None) -> list[tuple[str, str]]:
    if not channel:
        return []
    data = _load_reading_dict(channel)
    entries = data.get("abbreviation_replacements", [])
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        if entry.get("enabled", False):
            pairs.append((str(entry["surface"]), str(entry["reading"])))
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def _channel_numeric_units(channel: str | None) -> dict[str, str]:
    if not channel:
        return {}
    data = _load_reading_dict(channel)
    return {
        str(entry["surface"]): str(entry["reading"])
        for entry in data.get("numeric_units", [])
        if entry.get("enabled", False)
    }


def _replace_channel_numeric_notation(s: str, channel: str | None) -> str:
    """数字の直後だけをチャンネル固有の単位読みへ変換する。

    単独の英字は対象にしない。``a/b`` 形式の分数表記は、辞書で明示されたチャンネルだけ
    ``b分のa`` の読みにするため、それ以外の比率表記やティッカーには影響しない。
    """
    if not channel:
        return s
    data = _load_reading_dict(channel)
    fraction = data.get("ratio_reading") or {}
    if fraction.get("enabled", False):
        connector = str(fraction.get("connector", "ブンノ"))
        s = re.sub(
            r"(?<![\d/])([\d,]+)\s*/\s*([\d,]+)(?![\d/])",
            lambda m: f"{m.group(2)}{connector}{m.group(1)}",
            s,
        )

    units = _channel_numeric_units(channel)
    if not units:
        return s
    unit_alt = "|".join(re.escape(unit) for unit in sorted(units, key=len, reverse=True))
    return re.sub(
        r"(?<![\d,])([\d,]+)(" + unit_alt + r")",
        lambda m: f"{m.group(1)}{units[m.group(2)]}",
        s,
    )


def preprocess_text(text: str, channel: str | None = None) -> str:
    """VOICEVOX に渡す前のテキスト整形。誤読しやすい記号・略語・数値を整形する。

    処理順:
      1. 数字に隣接する +/- → プラス/マイナス
      2. range 記号 ~/〜 → から
      3. チャンネル固有の略語(長いキー優先)。指定なしでは何もしない
      4. チャンネル固有の数字+単位・分数表記の読み。単独の英字は置換しない
      5. 小数 N.M → "Nテン<Mカナ>" に変換(8.83% → 8テンハチサン% 等)
      6. TEXT_REPLACEMENTS を **キー長の降順** で適用(部分マッチによる二重置換防止)
    """
    import re as _re
    if not text:
        return text
    s = text
    # 1. 数字に隣接する +/- を「プラス/マイナス」に置換
    s = _re.sub(r"\+(?=\d)", "プラス", s)
    s = _re.sub(r"(?<![a-zA-Z\d])-(?=\d)", "マイナス", s)
    # 2. range 表現
    s = _re.sub(r"(?<=\d)[~〜](?=\d)", "から", s)
    # 3. チャンネル固有略語。小数を含む語(例: "1.5倍")を先に保護する。
    for src, dst in _channel_replacements(channel):
        s = s.replace(src, dst)
    # 4. 小数処理で末尾の数字がカナ化される前に、数字+単位を処理する。
    s = _replace_channel_numeric_notation(s, channel)
    # 5. 小数(整数部.小数部 + 単位) を「整数テンカナ単位カナ」に変換
    #    VOICEVOX はカナ直後の単位記号(%/円/ドル等)を読まないため、単位もカナ化する
    #    例: 8.83% → 8テンハチサンパーセント、1.46% → 1テンヨンロクパーセント
    #        1,234.56円 → 1,234テンゴロクエン
    UNIT_KANA = {
        "%": "パーセント",
        "%": "パーセント",  # 全角
        "円": "エン",
        "ドル": "ドル",
        "ユーロ": "ユーロ",
        "ポンド": "ポンド",
        "兆": "チョウ",
        "億": "オク",
        "万": "マン",
        "千": "セン",
        "百": "ヒャク",
        "ポイント": "ポイント",
        "pt": "ポイント",
        "倍": "バイ",
        "bps": "ビーピーエス",
    }
    # 単位を長い順でマッチ
    units_sorted = sorted(UNIT_KANA.keys(), key=len, reverse=True)
    units_alt = "|".join(_re.escape(u) for u in units_sorted)
    decimal_re = _re.compile(r"([\d,]+)\.(\d+)(" + units_alt + r")?")

    def _decimal_repl(m: "_re.Match") -> str:
        int_part = m.group(1)
        frac_part = m.group(2)
        unit = m.group(3) or ""
        kana = _decimal_to_natural_kana(int_part, frac_part)
        if unit:
            kana += UNIT_KANA[unit]
        return kana
    s = decimal_re.sub(_decimal_repl, s)
    # 6. 共通の単純置換: キー長の降順で(長いキー優先 → 部分マッチ事故防止)
    for src, dst in sorted(TEXT_REPLACEMENTS, key=lambda x: -len(x[0])):
        s = s.replace(src, dst)
    return s


def _speaker_id(speaker: str) -> int:
    """設定済みの話者 ID を返す。未知の役割は speaker_b の設定へ、何も無ければ明示的に失敗する。"""
    if speaker in SPEAKER_ID:
        return SPEAKER_ID[speaker]
    if "speaker_b" in SPEAKER_ID:
        return SPEAKER_ID["speaker_b"]
    raise RuntimeError(
        "VOICEVOX の話者 ID が未設定です。環境変数 NEWS_SYNTH_CONFIG が指す JSON に "
        "{\"speaker_id\": {\"speaker_b\": <id>, ...}} を書いてください(既定値はありません)"
    )


def synth(text: str, speaker: str, out_path: Path, channel: str | None = None) -> None:
    """VOICEVOX API を呼んで WAV を生成。outputSamplingRate=48000 固定。

    text は preprocess_text() で記号・略語を整形してから VOICEVOX に渡す。
    """
    spk = _speaker_id(speaker)
    tuning = TUNING.get(speaker, TUNING["speaker_b"])
    text = preprocess_text(text, channel=channel)

    qs = urllib.parse.urlencode({"text": text, "speaker": spk})
    aq = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                f"{VOICEVOX}/audio_query?{qs}", method="POST"
            ),
            timeout=60,
        ).read()
    )
    aq.update({**tuning, "outputSamplingRate": 48000, "outputStereo": False})

    qs2 = urllib.parse.urlencode({"speaker": spk})
    wav = urllib.request.urlopen(
        urllib.request.Request(
            f"{VOICEVOX}/synthesis?{qs2}",
            data=json.dumps(aq).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        ),
        timeout=120,
    ).read()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(wav)


def wavdur(p: Path) -> float:
    """WAV ファイルの再生時間(秒)を返す。"""
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / w.getframerate()


_VOICE_CACHE_MANIFEST = ".voice_cache.json"


def _voice_input_payload(script: dict, channel: str | None) -> dict[str, Any]:
    """Return exactly the script fields that determine synthesized speech."""
    return {
        "version": 1,
        "channel": channel or "",
        "lines": [
            {
                "id": str(ln["id"]),
                "speaker": str(ln["speaker"]),
                "text": str(ln["text"]),
            }
            for ln in script.get("lines", [])
        ],
    }


def voice_input_hash(script: dict, channel: str | None = None) -> str:
    """Stable hash for every input that selects a line's VOICEVOX output."""
    encoded = json.dumps(
        _voice_input_payload(script, channel), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def voice_cache_matches(script_path: Path, voice_dir: Path, channel: str | None = None) -> bool:
    """Whether *all* cached WAVs were produced for this script's speech inputs."""
    script = json.loads(script_path.read_text(encoding="utf-8"))
    channel = channel or _channel_from_script_path(script_path)
    manifest_path = voice_dir / _VOICE_CACHE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("voice_input_hash") != voice_input_hash(script, channel):
        return False
    return all((voice_dir / f"{ln['id']}.wav").is_file() for ln in script.get("lines", []))


def _clear_voice_wavs(voice_dir: Path) -> None:
    """Drop raw and loudnorm WAV cache entries, but no user-authored files."""
    if not voice_dir.exists():
        return
    for path in voice_dir.glob("*.wav"):
        path.unlink()
    (voice_dir / _VOICE_CACHE_MANIFEST).unlink(missing_ok=True)


def synth_script(script_path: Path, voice_dir: Path, channel: str | None = None) -> dict[str, float]:
    """script.json の全 lines を合成し、{line_id: duration_sec} を返す。

    音声入力(行ID・本文・話者・チャンネル)の内容ハッシュが一致するときだけ
    WAV を再利用する。行IDだけの存在判定は、台本更新時に旧音声を混入させるため
    使用しない。
    """
    script = json.loads(script_path.read_text(encoding="utf-8"))
    voice_dir.mkdir(parents=True, exist_ok=True)
    channel = channel or _channel_from_script_path(script_path)
    input_hash = voice_input_hash(script, channel)
    if not voice_cache_matches(script_path, voice_dir, channel):
        print("  [synth] voice cache miss: discard stale WAVs")
        _clear_voice_wavs(voice_dir)

    durations: dict[str, float] = {}
    for ln in script["lines"]:
        out = voice_dir / f"{ln['id']}.wav"
        if not out.exists():
            print(f"  synth {ln['id']} {ln['speaker']:9} «{ln['text'][:30]}»")
            synth(ln["text"], ln["speaker"], out, channel=channel)
        durations[ln["id"]] = wavdur(out)

    (voice_dir / _VOICE_CACHE_MANIFEST).write_text(
        json.dumps({"version": 1, "voice_input_hash": input_hash}, indent=2) + "\n",
        encoding="utf-8",
    )

    return durations


def reschedule(script_path: Path, durations: dict[str, float]) -> float:
    """script.json の start_sec / duration_sec を実測値で再設定し、total_sec を返す。

    トピック切替時の TOPIC_GAP は GAP と同値なので差分 = 0(設計上余分 pause 撤廃)。
    script_path を上書き保存する。
    """
    script = json.loads(script_path.read_text(encoding="utf-8"))
    lines = script["lines"]

    t = 0.0
    prev_topic = None
    for ln in lines:
        d = durations[ln["id"]]
        topic = ln.get("topic", "")
        if prev_topic is not None and topic != prev_topic and topic != "" and prev_topic != "":
            t += TOPIC_GAP - GAP  # 既に GAP 入ってるので差分のみ
        ln["start_sec"] = round(t, 3)
        ln["duration_sec"] = round(d, 3)
        t += d + GAP
        prev_topic = topic

    total = round(t - GAP, 3)
    script["duration_sec"] = total

    script_path.write_text(
        json.dumps(script, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return total


def write_timing(script_path: Path, voice_subdir: str, timing_path: Path) -> None:
    """timing_<base>.json を書き出す(Remotion render の参照用)。"""
    script = json.loads(script_path.read_text(encoding="utf-8"))
    lines = script["lines"]
    total = float(script.get("duration_sec", 0.0))
    fps = 30

    timing = {
        "total_sec": total,
        "fps": fps,
        "total_frames": int(round(total * fps)),
        "lines": [
            {
                "id": ln["id"],
                "speaker": ln["speaker"],
                "expression": ln["expression"],
                "start": ln["start_sec"],
                "end": round(ln["start_sec"] + ln["duration_sec"], 3),
                "wav": f"{voice_subdir}/{ln['id']}.wav",
            }
            for ln in lines
        ],
    }

    timing_path.write_text(
        json.dumps(timing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
