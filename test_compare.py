"""
翻訳前後の比較テスト

元PDFの各ページを markdown 化し、翻訳後PDFも同様に markdown 化して
両者を並べて出力する。構造が維持されているか目視確認する。

使い方:
  python test_compare.py                          # 全ページ翻訳して比較
  python test_compare.py 0 1 3                    # 指定ページのみ
"""
from __future__ import annotations
import sys
from pathlib import Path

import pymupdf
import utils  # noqa: F401 — SafePopenパッチを適用する
from utils import page_to_markdown

from config import TranslationConfig
from pipeline import run_translation


def _table_summary(doc: pymupdf.Document, page_no: int) -> str:
    tables = doc[page_no].find_tables()
    if not tables.tables:
        return "  (表なし)"
    parts = []
    for ti, t in enumerate(tables.tables):
        data = t.extract()
        parts.append(f"  table{ti} ({t.row_count}行x{t.col_count}列):")
        for ri, row in enumerate(data):
            parts.append(f"    row{ri}: {row}")
    return "\n".join(parts)


def compare_page(orig_doc, trans_doc, orig_pno, trans_pno):
    """1ページ分の比較を出力。"""
    print("=" * 70)
    print(f"元ページ {orig_pno}  →  翻訳ページ {trans_pno}")
    print("=" * 70)

    orig_md = page_to_markdown(orig_doc, orig_pno)
    trans_md = page_to_markdown(trans_doc, trans_pno)

    max_preview = 1200
    print("\n【元 markdown】")
    print(orig_md[:max_preview])
    if len(orig_md) > max_preview:
        print(f"  ... ({len(orig_md)} chars total)")

    print("\n【翻訳後 markdown】")
    print(trans_md[:max_preview])
    if len(trans_md) > max_preview:
        print(f"  ... ({len(trans_md)} chars total)")

    print("\n【表の比較】")
    print("  元:")
    print(_table_summary(orig_doc, orig_pno))
    print("  翻訳後:")
    print(_table_summary(trans_doc, trans_pno))
    print()


def run_compare(pages: list[int] | None = None):
    """指定ページを翻訳して比較する。"""

    input_pdf = Path("embedded_design_guide.pdf")
    output_pdf = Path("test_compare_output.pdf")

    if not input_pdf.exists():
        print(f"エラー: {input_pdf} が見つかりません")
        sys.exit(1)

    orig_doc = pymupdf.open(str(input_pdf))
    total = len(orig_doc)

    if pages is None:
        pages = list(range(total))
    pages = [p for p in pages if 0 <= p < total]

    print(f"=== 翻訳実行: {len(pages)} ページ ===\n")

    config = TranslationConfig(
        input_pdf=input_pdf,
        output_pdf=output_pdf,
        pages=pages,
    )
    run_translation(config)

    trans_doc = pymupdf.open(str(output_pdf))

    print(f"\n=== 比較結果 ===\n")
    for orig_pno in pages:
        if orig_pno >= len(trans_doc):
            print(f"p{orig_pno}: 翻訳PDFに対応ページなし")
            continue
        compare_page(orig_doc, trans_doc, orig_pno, orig_pno)

    orig_doc.close()
    trans_doc.close()
    print(f"翻訳PDF: {output_pdf}")


if __name__ == "__main__":
    page_args = [int(p) for p in sys.argv[1:]] if len(sys.argv) > 1 else None
    run_compare(page_args)
