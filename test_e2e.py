"""
E2E テスト — 翻訳パイプラインの前後比較

元PDFと翻訳後PDFを markdown 化し、以下を自動検証する:
  1. ページ数が一致すること
  2. 各ページにテキストが存在すること（翻訳抜けがないこと）
  3. 表の行列数が一致すること
  4. 画像・図形が維持されていること
  5. 翻訳テキストが日本語を含むこと

使い方:
  python test_e2e.py                # 全ページ
  python test_e2e.py 0 1 3          # 指定ページのみ

戻り値: 全チェック合格で exit 0、失敗ありで exit 1
"""
from __future__ import annotations
import sys
from pathlib import Path
from dataclasses import dataclass, field

import pymupdf
import utils  # noqa: F401 — SafePopenパッチを適用する
from utils import page_to_markdown, has_japanese, safe_tables


# ================================================================== #
#  検証結果
# ================================================================== #

@dataclass
class CheckResult:
    page: int
    name: str
    passed: bool
    detail: str = ""

@dataclass
class E2EReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, page: int, name: str, passed: bool, detail: str = ""):
        self.results.append(CheckResult(page, name, passed, detail))

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    def print_summary(self):
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)
        print("=" * 60)
        print(f"E2E結果: {passed} passed, {failed} failed")
        print("=" * 60)
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            detail = f" — {r.detail}" if r.detail else ""
            print(f"  [{mark}] p{r.page} {r.name}{detail}")
        print()



# ================================================================== #
#  ページ単位の検証
# ================================================================== #

def check_page(
    report: E2EReport,
    orig_doc: pymupdf.Document,
    trans_doc: pymupdf.Document,
    orig_pno: int,
    trans_pno: int,
) -> None:
    """１ページ分のチェックを実行し report に追加する。"""

    orig_page = orig_doc[orig_pno]
    trans_page = trans_doc[trans_pno]

    # --- 1. テキスト存在チェック ---
    orig_text = orig_page.get_text()
    trans_text = trans_page.get_text()
    has_text = len(trans_text.strip()) > 0
    report.add(orig_pno, "テキスト存在",
               has_text,
               f"元={len(orig_text)}字, 訳={len(trans_text)}字")

    # --- 2. 日本語含有チェック ---
    report.add(orig_pno, "日本語含有", has_japanese(trans_text))

    # --- 3. 表の行列数チェック ---
    orig_tables = safe_tables(orig_page)
    trans_tables = safe_tables(trans_page)
    table_count_ok = len(orig_tables) == len(trans_tables)
    report.add(orig_pno, "表の数",
               table_count_ok,
               f"元={len(orig_tables)}, 訳={len(trans_tables)}")

    for ti in range(min(len(orig_tables), len(trans_tables))):
        ot = orig_tables[ti]
        tt = trans_tables[ti]
        shape_ok = (ot.row_count == tt.row_count and ot.col_count == tt.col_count)
        report.add(orig_pno, f"表{ti}行列数",
                   shape_ok,
                   f"元={ot.row_count}x{ot.col_count}, 訳={tt.row_count}x{tt.col_count}")

    # --- 4. 画像維持チェック ---
    orig_imgs = len(orig_page.get_images())
    trans_imgs = len(trans_page.get_images())
    img_ok = orig_imgs == trans_imgs
    report.add(orig_pno, "画像数",
               img_ok,
               f"元={orig_imgs}, 訳={trans_imgs}")

    # --- 5. 図形維持チェック (表描画で増えるのは許容、減るのはNG) ---
    orig_drw = len(orig_page.get_drawings())
    trans_drw = len(trans_page.get_drawings())
    drw_ok = trans_drw >= orig_drw
    report.add(orig_pno, "図形数",
               drw_ok,
               f"元={orig_drw}, 訳={trans_drw}")

    # --- 6. リンク数チェック (翻訳でリンクが失われないこと) ---
    orig_links = orig_page.get_links()
    trans_links = trans_page.get_links()
    link_ok = len(trans_links) >= len(orig_links)
    report.add(orig_pno, "リンク数",
               link_ok,
               f"元={len(orig_links)}, 訳={len(trans_links)}")

    # --- 7. 箇条書き記号の単独行チェック ---
    # 「• だけの行」が本文なしで存在すると視覚的に「•\n本文」になってしまう
    trans_lines = trans_text.split("\n")
    bullet_only_lines = [
        i for i, ln in enumerate(trans_lines) if ln.strip() in ("•", "・")
    ]
    bullet_ok = len(bullet_only_lines) == 0
    sample = ""
    if bullet_only_lines:
        i = bullet_only_lines[0]
        nxt = trans_lines[i + 1][:30] if i + 1 < len(trans_lines) else ""
        sample = f" 例:{i}行目 -> \"{nxt}\""
    report.add(orig_pno, "箇条書き結合",
               bullet_ok,
               f"単独バレット行数={len(bullet_only_lines)}{sample}")

    # --- 8. テーブルセルはみ出しチェック ---
    # span の始点が表内にあり、終点が表の外にはみ出しているものを検出
    overflow_count = 0
    overflow_sample = ""
    for tt in trans_tables:
        tbl_rect = pymupdf.Rect(tt.bbox)
        td = trans_page.get_text("dict")
        for b in td["blocks"]:
            if b.get("type", 0) != 0:
                continue
            for ln in b["lines"]:
                for sp in ln["spans"]:
                    sp_rect = pymupdf.Rect(sp["bbox"])
                    starts_inside = (
                        tbl_rect.x0 <= sp_rect.x0 < tbl_rect.x1 and
                        tbl_rect.y0 <= sp_rect.y0 < tbl_rect.y1
                    )
                    ends_outside = (
                        sp_rect.x1 > tbl_rect.x1 + 2 or
                        sp_rect.y1 > tbl_rect.y1 + 2
                    )
                    if starts_inside and ends_outside:
                        overflow_count += 1
                        if not overflow_sample:
                            overflow_sample = f' 例:"{sp["text"][:20]}"'
    overflow_ok = overflow_count == 0
    report.add(orig_pno, "セル収容",
               overflow_ok,
               f"はみ出しspan={overflow_count}{overflow_sample}")

    # --- markdown 比較 (情報出力のみ、PASS/FAIL なし) ---
    orig_md = page_to_markdown(orig_doc, orig_pno)
    trans_md = page_to_markdown(trans_doc, trans_pno)

    print(f"--- p{orig_pno} 元 markdown ({len(orig_md)}字) ---")
    print(orig_md[:600])
    if len(orig_md) > 600:
        print(f"  ... (省略)")
    print(f"--- p{orig_pno} 翻訳後 markdown ({len(trans_md)}字) ---")
    print(trans_md[:600])
    if len(trans_md) > 600:
        print(f"  ... (省略)")
    print()


# ================================================================== #
#  メイン
# ================================================================== #

def run_e2e(
    pages: list[int] | None = None,
    input_file: str | None = None,
    *,
    use_cache: bool = True,
    clear_cache: bool = False,
) -> bool:
    """E2E テストを実行し、全合格なら True を返す。"""
    from config import TranslationConfig
    from pipeline import run_translation

    input_pdf = Path(input_file) if input_file else Path("embedded_design_guide.pdf")
    output_pdf = Path(f"test_e2e_{input_pdf.stem}_output.pdf")

    if not input_pdf.exists():
        print(f"エラー: {input_pdf} が見つかりません")
        return False

    orig_doc = pymupdf.open(str(input_pdf))
    total = len(orig_doc)

    if pages is None:
        pages = list(range(total))
    pages = [p for p in pages if 0 <= p < total]

    # --- 翻訳実行 ---
    print(f"=== E2Eテスト: {len(pages)} ページ ===\n")

    config = TranslationConfig(
        input_pdf=input_pdf,
        output_pdf=output_pdf,
        pages=pages,
        use_cache=use_cache,
        clear_cache=clear_cache,
    )
    run_translation(config)

    trans_doc = pymupdf.open(str(output_pdf))

    # --- ページ数チェック ---
    report = E2EReport()
    page_count_ok = len(trans_doc) == len(orig_doc)
    report.add(-1, "ページ数",
               page_count_ok,
               f"期待={len(orig_doc)}, 実際={len(trans_doc)}")

    # --- ページごとのチェック ---
    print(f"\n=== markdown 比較 ===\n")
    for orig_pno in pages:
        if orig_pno >= len(trans_doc):
            report.add(orig_pno, "ページ存在", False, "翻訳PDFに対応ページなし")
            continue
        check_page(report, orig_doc, trans_doc, orig_pno, orig_pno)

    orig_doc.close()
    trans_doc.close()

    # --- 結果 ---
    report.print_summary()
    print(f"翻訳PDF: {output_pdf}")
    return report.all_passed


if __name__ == "__main__":
    # 使い方:
    #   python test_e2e.py [入力PDF] [ページ番号...] [--no-cache] [--clear-cache]
    # 例:
    #   python test_e2e.py S32K3XXRM.pdf 0 1 2 3
    #   python test_e2e.py S32K3XXRM.pdf 0 1 2 3 --no-cache
    args = sys.argv[1:]
    use_cache = True
    clear_cache = False

    # フラグを先に処理
    filtered: list[str] = []
    for a in args:
        if a == "--no-cache":
            use_cache = False
        elif a == "--clear-cache":
            clear_cache = True
        else:
            filtered.append(a)
    args = filtered

    input_file = None
    page_args = None
    if args and not args[0].isdigit():
        input_file = args[0]
        args = args[1:]
    if args:
        page_args = [int(p) for p in args]

    ok = run_e2e(page_args, input_file, use_cache=use_cache, clear_cache=clear_cache)
    sys.exit(0 if ok else 1)
