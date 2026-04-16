"""p1716 の register description 表で改行保持を検証する単発スクリプト。

S32K3XXRM.pdf の p1716 だけを 1 ページ PDF に抽出し、翻訳→出力 PDF を検査する。
全体を翻訳すると最終圧縮が 5394 ページ対象で非常に遅いため、1 ページ単位にする。

使い方:
  .venv/Scripts/python.exe -X utf8 tests/verify_p1716.py
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf

from config import TranslationConfig
from pipeline import run_translation

INPUT = Path("S32K3XXRM.pdf")
EXTRACTED = Path("p1716_only.pdf")
OUTPUT = Path("p1716_translated.pdf")
PAGE_NO = 1716  # 0-indexed


def extract_single_page() -> None:
    src = pymupdf.open(str(INPUT))
    dst = pymupdf.open()
    dst.insert_pdf(src, from_page=PAGE_NO, to_page=PAGE_NO)
    dst.save(str(EXTRACTED), garbage=1, deflate=True)
    dst.close()
    src.close()
    print(f"extracted: {EXTRACTED} ({EXTRACTED.stat().st_size} bytes)")


def translate() -> None:
    cfg = TranslationConfig(
        input_pdf=EXTRACTED,
        output_pdf=OUTPUT,
        pages=[0],
        use_cache=True,
        clear_cache=True,  # 新プロンプトで再翻訳
    )
    run_translation(cfg)


def inspect_cells() -> None:
    """出力 PDF の表セル内テキストを読み取り、改行 (\\n) が保持されているか確認する。"""
    doc = pymupdf.open(str(OUTPUT))
    page = doc[0]

    # 表検出
    tables = page.find_tables().tables
    print(f"\n=== 検証結果 (p1716 翻訳後) ===")
    print(f"検出された表の数: {len(tables)}")

    for ti, t in enumerate(tables):
        data = t.extract()
        print(f"\n--- 表{ti} ({t.row_count}x{t.col_count}) ---")
        multi_line_cells = 0
        for ri, row in enumerate(data):
            for ci, cell in enumerate(row):
                if cell and "\n" in cell:
                    multi_line_cells += 1
                    lines = cell.split("\n")
                    print(f"  セル[{ri}][{ci}] 行数={len(lines)}:")
                    for li, line in enumerate(lines[:5]):
                        print(f"    [{li}] {line[:80]}")
                    if len(lines) > 5:
                        print(f"    ... (残り {len(lines) - 5} 行省略)")
        print(f"  複数行セル数: {multi_line_cells}")

    doc.close()


if __name__ == "__main__":
    extract_single_page()
    translate()
    inspect_cells()
