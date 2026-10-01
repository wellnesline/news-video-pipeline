# news-video-pipeline

Build pipeline for short news-explainer videos: structured news config → VOICEVOX narration → speech bubbles → Remotion graphics → ffmpeg composition, with a rule-based QA and an escalation-capped rework loop.

構造化したニュース設定（`news_config.json`）と台本（`script.json`）から、解説動画を組み立てるパイプラインです。公開用に整理したサンプル実装です。

## 概要

ニュース動画は「数字の取り違え」「単位の不一致」「根拠 URL の欠落」といった事故が起きやすい題材です。このリポジトリは、ニュースの取材（調査）と動画の組み立てを分離し、組み立て側に機械的な検査（QA）と、無限に手戻りしないための上限付き差し戻しループを持たせています。取材は外部のエージェントが `news_config.json` / `script.json` を書き出す前提で、本リポジトリは読み込み側です。

## 主な機能・設計のポイント

- **ビルドの流れ（`news_pipeline/build.py`）**: カット読み込み → VOICEVOX で音声合成 → タイムライン確定 → 吹き出し PNG 生成 → Remotion でグラフィック描画 → ffmpeg で立ち絵・吹き出し・音声・BGM を合成 → 構造 QA
- **構造 QA（`qa.py`）**: 設定ファイルを読み、主要数値が数値文字列か、比較グラフの単位と値の範囲（絶対値モード / 変化率モードの整合）、シーン数、文字長など、複数の項目を機械検査して `qa_report.md` を出します。重大度は FAIL / WARN で区別します
- **構造的 fact-check（`fact_check.py`）**: 根拠 URL の形式・個数を機械的に検査。実際の数値照合は別担当（エージェント）の役割として分離しています
- **ルールベース自動修正（`auto_fix.py`）**: LLM が不要な軽微な WARN だけを機械的な置換で直します
- **差し戻しループ（`orchestrator.py` / `escalation.py`）**: build → QA → 差し戻し依頼の生成を繰り返し、回数に上限（ハードキャップ）を置きます。上限を超えたら人に引き継ぐ `_escalation.json` を出して止まります
- **古い成果物を残さない（`artifacts.py`）**: 台本を作り直したら、前回のビルド成果物を使い回さないよう整理します
- **読み上げの誤読対策（`synth.py`）**: 数値表記などを VOICEVOX に渡す前に置換するルール集
- **メタデータ生成（`meta.py`）**: LLM を使わず、テンプレートとトピックから投稿用のタイトル・タグ・概要を組み立て。テンプレートは JSON 設定（`NEWS_META_CONFIG`）で渡します
- **数値照合用データ（`sources/stooq.py`）**: 公開 CSV エンドポイントからの数値取得クライアント（テストではモック前提。素直な User-Agent で、取得頻度は利用条件に従う）

## ディレクトリ構成（主要ファイル）

```
news-video-pipeline/
├ news_pipeline/
│  ├ build.py  orchestrator.py  escalation.py  artifacts.py
│  ├ synth.py  bubble.py  compose.py  hook_image.py
│  ├ qa.py  qa_keyterms.py  fact_check.py  auto_fix.py  meta.py  base.py
│  ├ sources/stooq.py
│  └ variants/cut_a.py  cut_b.py    # カットごとの読み込み
├ config/layout.example.json
├ audio_master.py  channel_resolver.py  _platform.py  _dotenv.py  openai_image_config.py   ← 合成スタブ
```

## 動かし方

- 必要なもの: Python 3、ffmpeg、Node.js（Remotion）、VOICEVOX エンジン（ローカルのポート 50021）
- 環境変数: `OPENAI_API_KEY`（フック画像の生成を使うときだけ）、`NEWS_META_CONFIG`（メタデータ設定 JSON のパス）、`NEWS_LAYOUT_CONFIG`（立ち絵の原寸・表示側・配置座標の JSON。`config/layout.example.json` を参照）、`NEWS_HOOK_CONFIG`（フック画像の mood と参照画像の対応）、`NEWS_SYNTH_CONFIG`（話者 ID と調整値）、`NEWS_FONT_REGULAR` / `NEWS_FONT_BOLD`（日本語フォントのパス）
- 構成の前提: 本コードはホストプロジェクトの `src/` に置く前提です。Remotion の描画エントリは `src/remotion/news/index.ts` を探します（`remotion-explainer-templates` に同梱）
- 配置・色・文言・ワイプ枠サイズなどの固定値は、中立なサンプル値を既定にしてあります。VOICEVOX の話者 ID とフォントのパスは既定値を持たず、`NEWS_SYNTH_CONFIG`（`{"speaker_id": {"speaker_a": <ID>, ...}}` の JSON）と `NEWS_FONT_REGULAR` / `NEWS_FONT_BOLD` で必ず渡します（未設定なら明示的なエラーで止まります）。実際の値は上の設定ファイルで渡してください。
- 立ち絵の命名規則: `<speaker>_<emotion>_<mouth>.png`（例: `speaker_a_normal_close.png`）。参照画像として画像生成 AI に渡す素材は、AI への入力が規約で許される自作または許諾済みのものに限ります
- フック画像の生成: `python -m news_pipeline.hook_image --cut <cut> --date <YYYY-MM-DD> --channel <name>`

## 制限事項（閉包）

`ast` と import 文の調査で、リポジトリ内・標準ライブラリに無く、`requirements.txt` も無いため、次を同梱していない依存として挙げます。

- **同梱していない Python パッケージ（3 件）**: `PIL`（Pillow）, `yaml`（PyYAML）, `ruamel`（ruamel.yaml）
- **他の公開リポジトリへの依存**: `remotion-explainer-templates`（Remotion の `src/remotion/news`）。`video-qa-gates` の一部（誤読検出、根拠 URL テスト）が `news_pipeline` を import します
- **合成スタブ（5 件）**: `audio_master.py`（ラウドネス調整は何もしない）, `channel_resolver.py`（固定のサンプル設定を返す）, `_platform.py`（環境変数からのフォント解決と H.264 エンコーダ選択のサンプル）, `_dotenv.py`, `openai_image_config.py`（定数のみ）
- **非同梱の実行時データ**: 立ち絵・背景などの画像素材、BGM、チャンネル設定ファイル、`news_config.json` / `script.json`（取材結果）
- 外部サービス: VOICEVOX、OpenAI 画像生成、Stooq の公開 CSV。VOICEVOX の各音声ライブラリは、公開時のクレジット表記（`VOICEVOX:<キャラクター名>` の形）が利用条件になっていることがあります。概要欄などに、使った話者の表記を入れてください。BGM、画像、取得したデータも、各サービス・各素材の利用規約と権利者の許諾の範囲で使ってください

## ライセンス

All rights reserved. 閲覧目的で公開しています。詳細は [LICENSE](LICENSE) を参照。
