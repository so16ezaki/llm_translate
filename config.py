from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TranslationConfig:
    input_pdf:       Path
    output_pdf:      Path
    source_lang:     str            = "en"
    target_lang:     str            = "ja"
    model:           str            = "gemma4:e4b"
    ollama_url:      str            = "http://localhost:11434"
    temperature:     float          = 0.1
    timeout_sec:     int            = 120
    pages:           list[int] | None = None
    # テキストがこの文字数未満のページは図と見なしてスキップ
    min_text_chars:  int            = 50
    # LLMへの同時リクエスト数（Ollamaの場合はメモリ消費に注意して調整）
    # 実測: 5 → 10 で 20% 高速化、10以上は Ollama 側の GPU 限界で飽和
    max_workers:     int            = 10

    # --- バックエンド選択 ---
    backend:         str            = "ollama"  # "ollama" | "dify"
    dify_url:        str            = ""
    dify_api_key:    str            = ""

    # --- 翻訳結果キャッシュ (チェックポイント) ---
    # False なら SQLite キャッシュを読み書きせず、毎回 LLM に問い合わせる
    use_cache:       bool           = True
    # True ならキャッシュ DB を削除してから開始 (強制全再翻訳)
    clear_cache:     bool           = False
    # キャッシュ DB のパスを明示したい場合に指定。None なら入力 PDF に紐付けた
    # .{pdf_stem}_cache.sqlite3 を自動利用
    cache_path:      str | None     = None

    # --- チャンクストリーミング ---
    # N ページごとに Phase 1→2→3→save を完結させる。強制終了時もそこまでは保存される
    # 0 または非常に大きい値で「従来の一括処理」と同等の動作
    chunk_size:      int            = 100
