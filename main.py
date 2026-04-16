"""
PDF翻訳ツール — Ollama / Dify バックエンドでページ単位翻訳する

設定をここで直接編集して python main.py で実行する。

CLI オプション:
  --no-cache     翻訳結果キャッシュ (チェックポイント) を無視して毎回 LLM に問い合わせる
  --clear-cache  キャッシュ DB を削除してから開始する (強制全再翻訳)
  --cache-path PATH  キャッシュ DB のパスを明示指定 (省略時は PDF に紐付けた自動パス)
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from config import TranslationConfig
from pipeline import run_translation

# =====================================================================
# ★ 設定エリア — ここを編集する
# =====================================================================

INPUT_PDF = Path(r"S32K3XXRM.pdf")
OUTPUT_PDF = Path(r"output.pdf")
SOURCE_LANG = "en"
TARGET_LANG = "ja"
MODEL = "gemma4:e4b"
OLLAMA_URL = "http://localhost:11434"

# バックエンド選択: "ollama" または "dify"
BACKEND = "ollama"
DIFY_URL = ""
DIFY_API_KEY = ""

# 翻訳するページ番号（0始まり）。None → 全ページ
PAGES: list[int] | None = None

# =====================================================================


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PDF 翻訳 (Ollama / Dify)")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="翻訳結果キャッシュを無視して毎回 LLM に問い合わせる (書き込みもしない)",
    )
    parser.add_argument(
        "--clear-cache", action="store_true",
        help="既存のキャッシュ DB を削除してから開始する (強制全再翻訳)",
    )
    parser.add_argument(
        "--cache-path", default=None,
        help="キャッシュ DB のパスを明示指定 (省略時は入力 PDF に紐付けた自動パス)",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=None,
        help="N ページごとに抽出→翻訳→描画→保存を完結させる (デフォルト 100、0 で一括処理)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not INPUT_PDF.exists():
        print(f"エラー: 入力ファイルが見つかりません: {INPUT_PDF}")
        sys.exit(1)

    kw = dict(
        input_pdf=INPUT_PDF,
        output_pdf=OUTPUT_PDF,
        source_lang=SOURCE_LANG,
        target_lang=TARGET_LANG,
        model=MODEL,
        ollama_url=OLLAMA_URL,
        pages=[int(p) for p in PAGES] if PAGES is not None else None,
        backend=BACKEND,
        dify_url=DIFY_URL,
        dify_api_key=DIFY_API_KEY,
        use_cache=not args.no_cache,
        clear_cache=args.clear_cache,
        cache_path=args.cache_path,
    )
    if args.chunk_size is not None:
        kw["chunk_size"] = args.chunk_size
    config = TranslationConfig(**kw)

    run_translation(config)


if __name__ == "__main__":
    main()
