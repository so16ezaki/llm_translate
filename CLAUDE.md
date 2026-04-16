# CLAUDE.md — llm_translate

## プロジェクト概要
Python で構築されたプロジェクト

## 技術スタック
- 言語: Python
- フレームワーク: なし
- パッケージマネージャ: 不明

## ディレクトリ構造
```
├── .claude
│   └── settings.local.json
├── .pytest_cache
│   ├── v
│   ├── .gitignore
│   ├── CACHEDIR.TAG
│   └── README.md
├── .S32K3XXRM_checkpoints
│   ├── page_000000.json
│   ├── page_000001.json
│   ├── page_000002.json
│   ├── page_000003.json
│   ├── page_000004.json
│   ├── page_000005.json
│   ├── page_000006.json
│   ├── page_000007.json
│   ├── page_000008.json
│   ├── page_000009.json
│   ├── page_000010.json
│   ├── page_000011.json
│   ├── page_000012.json
│   ├── page_000013.json
│   ├── page_000014.json
│   ├── page_000015.json
│   ├── page_000016.json
│   ├── page_000017.json
│   ├── page_000018.json
│   ├── page_000019.json
│   ├── page_000020.json
│   ├── page_000021.json
│   ├── page_000022.json
│   ├── page_000023.json
│   ├── page_000024.json
│   ├── page_000025.json
│   ├── page_000026.json
│   ├── page_000027.json
│   ├── page_000028.json
│   └── page_000029.json
├── checkpoint
│   ├── __init__.py
│   └── store.py
├── pdf
│   ├── __init__.py
│   ├── extractor.py
│   └── renderer.py
├── tests
│   ├── test_checkpoint.py
│   ├── test_client.py
│   ├── test_e2e_real.py
│   ├── test_grouper.py
│   ├── test_layout_pages.py
│   ├── test_mapper.py
│   ├── test_pipeline.py
│   └── test_reconstructor.py
├── translation
│   ├── __init__.py
│   └── client.py
├── ._tmp_output_wtktn0zu.pdf
├── .gitignore
├── config.py
├── embedded_design_guide.pdf
├── main.py
├── output.pdf
├── output_translated.pdf
├── page6_analysis.txt
├── page6_debug.txt
├── page6_preview.png
├── page6_preview_fixed.png
├── pipeline.py
├── requirements.txt
├── S32K3XXRM.pdf
├── test_flexcan_p3187.pdf
├── test_output.pdf
└── test_render.pdf
```

## コーディング規約
- リンター: 未設定
- テストフレームワーク: 未設定

## MCP ツール運用ルール（必須）

このプロジェクトには2つのMCPツールが常駐している。必ず以下の順序で使うこと。

### 1. code-review-graph（構造クエリ）
- 最初に必ず呼ぶ: 任意のコード関連タスクを始めるとき、まず get_minimal_context(task="タスクの説明") を実行する。
- ファイルを直接読む前にグラフに聞く: Read ツールでファイルを開く前に、query_graph で必要な関数・クラスだけを特定する。
- 変更影響を事前に確認する: コードを修正する前に review_changes で blast radius を確認する。

### 2. Repomix（全体パック）
- 全体像が必要なときだけ使う: プロジェクト概要の把握、オンボーディング資料、モジュール横断の調査時に pack_codebase を呼ぶ。
- compress オプションを常に使う。
- includePatterns で対象を絞る。

### ツール選択の判断基準
| やりたいこと | 使うツール |
|---|---|
| 関数の依存関係を調べる | code-review-graph |
| 変更の影響範囲を知る | code-review-graph |
| クラス一覧・関数一覧 | code-review-graph |
| プロジェクト全体の概要 | Repomix |
| 特定ディレクトリのコード全文 | Repomix（includePatterns指定） |
| 設計方針・経緯の確認 | memory-bank/*.md を Read |

## シェル経由フォールバック（MCP が使えないエージェント用）

`.mcp.json` が自動ロードされない環境（Cline 等）では、MCP ツール名（`get_minimal_context_tool`, `pack_codebase` 等）は直接呼べない。
その場合は **必ず以下の絶対パス + `-X utf8` フラグで Python を起動すること**。
- 絶対パス: 対象プロジェクトの PATH 上の python には repomix / code-review-graph は入っていない
- `-X utf8`: Python UTF-8 Mode を有効化。Windows の cp932 で絵文字出力がクラッシュするのを防ぐ

```
C:\Users\so16e\Documents\vscode\python\analyzeCode\.venv\Scripts\python.exe
```

### code-review-graph (CLI)
```bash
"C:\Users\so16e\Documents\vscode\python\analyzeCode\.venv\Scripts\python.exe" -X utf8 -m code_review_graph build
"C:\Users\so16e\Documents\vscode\python\analyzeCode\.venv\Scripts\python.exe" -X utf8 -m code_review_graph status
"C:\Users\so16e\Documents\vscode\python\analyzeCode\.venv\Scripts\python.exe" -X utf8 -m code_review_graph wiki
```

### repomix (CLI)
```bash
"C:\Users\so16e\Documents\vscode\python\analyzeCode\.venv\Scripts\python.exe" -X utf8 -m repomix . --compress --output repomix-output.xml
```
生成された `repomix-output.xml` を Read ツールで読むことで全体像を把握できる。

### 禁止
- `python -m repomix ...` のような PATH の python を使う呼び方は必ず失敗する（repomix / code-review-graph は対象プロジェクトの環境に入っていないため）
- **`-X utf8` フラグを省略しない**（Windows で cp932 エンコードエラーになる）
- `chcp 65001` で回避しようとしない（Python 側のエンコーディングには効かない）
- 対象プロジェクトに `pip install repomix code-review-graph` しない

## Memory Bank 運用ルール

### セッション開始時
1. memory-bank/activeContext.md を読む
2. タスクに関連する場合 memory-bank/systemPatterns.md も読む

### タスク完了時
- memory-bank/activeContext.md を更新する
- memory-bank/progress.md を更新する

## 資料作成ルール

### 手順
1. get_minimal_context → 対象の全体像を把握
2. memory-bank/systemPatterns.md を読む → 設計方針を確認
3. query_graph で関連コードを絞り込む
4. 不足分だけ Repomix で補完
5. Markdown で出力

### 禁止事項
- グラフを使わずにファイルを片っ端から Read しない
- 推測でコードの依存関係を書かない（必ずグラフから取得する）

## コンテキスト最適化
- **ファイル全文を読むな、グラフに聞け**（最重要ルール）
- **/compact を戦略的に使う**: タスクのフェーズ切替時（調査 → 設計 → 実装 → レビュー）に毎回手動実行する。長いセッションで自動任せにしない。
- **調査タスクはサブエージェントに委譲する**: Task ツール経由で別コンテキストに切り出し、本体のコンテキストに調査ログを残さない。
- **ツール使い分け（RAG と Graph の使い分け）**:
  - 構造クエリ（依存関係・影響範囲・関数/クラス一覧・呼び出し関係）→ **code-review-graph**
  - 意味検索・キーワード grep・特定の語句がどこに現れるか → **repomix の grep_repomix_output**（事前に pack_codebase が必要）
  - 全文読み込み（Read）は最後の手段。グラフで特定できた関数・クラスに絞って読む。
