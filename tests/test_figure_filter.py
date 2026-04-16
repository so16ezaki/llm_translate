"""
図フィルタリング E2E テスト — p26 (ドキュメント p27) で検証

ブロックダイアグラム (Figure 11) とテキスト (2.5 Feature comparison) が
混在するページで以下を確認する:

  1. 図領域が正しく検出される（密度ベース + キャプション拡張）
  2. 図内テキストブロックが翻訳対象から除外される
  3. 図外テキストのみが翻訳される（日本語が含まれる）
  4. 翻訳後も画像・描画が維持される
  5. 図内テキスト（回路ラベル等）が元のまま残る

使い方:
  python -X utf8 tests/test_figure_filter.py
  python -X utf8 tests/test_figure_filter.py --dry-run   # 翻訳なしで検出のみ確認
"""
from __future__ import annotations
import sys
import os
import argparse
from pathlib import Path
from dataclasses import dataclass, field

# プロジェクトルートを path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf
from pipeline import _get_figure_rects, _block_in_figure, _block_in_table, _extract_block_text
from utils import has_japanese, safe_tables


INPUT_PDF = Path("S32K3XXRM.pdf")
# テスト対象ページ (0始まり) — ドキュメント p25,26,27
TEST_PAGES = [20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]


# ================================================================== #
#  レポート
# ================================================================== #

@dataclass
class CheckResult:
    page: int
    name: str
    passed: bool
    detail: str = ""

@dataclass
class TestReport:
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
        print(f"図フィルタテスト: {passed} passed, {failed} failed")
        print("=" * 60)
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            detail = f" — {r.detail}" if r.detail else ""
            print(f"  [{mark}] p{r.page} {r.name}{detail}")
        print()


# ================================================================== #
#  dry-run テスト (翻訳なし — 図検出ロジックのみ)
# ================================================================== #

def test_detection(report: TestReport) -> None:
    """図領域検出が正しく動作するか確認する。"""
    doc = pymupdf.open(str(INPUT_PDF))

    for pno in TEST_PAGES:
        page = doc[pno]
        text_dict = page.get_text("dict")
        blocks = text_dict["blocks"]
        text_blocks = [b for b in blocks if b["type"] == 0]

        figure_rects = _get_figure_rects(page, blocks)

        in_fig = [b for b in text_blocks if _block_in_figure(b, figure_rects)]

        # 図が存在するページのみ図検出チェック
        if figure_rects:
            # 1. 図領域が検出されること
            report.add(pno, "図領域検出",
                       len(figure_rects) >= 1,
                       f"{len(figure_rects)}領域")

            # 2. 図領域が十分な大きさであること (最低 100x100)
            for i, r in enumerate(figure_rects):
                big = r.width >= 100 and r.height >= 100
                report.add(pno, f"図{i}サイズ",
                           big,
                           f"{r.width:.0f}x{r.height:.0f}")

            # 3. 図内テキストが除外されること
            report.add(pno, "図内テキスト除外",
                       len(in_fig) > 0,
                       f"{len(in_fig)}ブロック除外")

        # 4. 表領域と図領域が区別できること
        tables = safe_tables(page)
        table_rects = [pymupdf.Rect(t.bbox) for t in tables]
        out_fig = [b for b in text_blocks
                   if not _block_in_table(b, table_rects)
                   and not _block_in_figure(b, figure_rects)]
        # ヘッダ/フッタを除いた図外テキストを確認
        content_blocks = [b for b in out_fig
                         if b["bbox"][1] > 60 and b["bbox"][1] < 740]

        if pno == 26:
            # p26: 図の下に "2.5 Feature comparison" テキストがある
            report.add(pno, "図外テキスト存在",
                       len(content_blocks) > 0,
                       f"{len(content_blocks)}ブロック")
            out_texts = " ".join(_extract_block_text(b) for b in content_blocks)
            has_feature = "Feature" in out_texts or "feature" in out_texts
            report.add(pno, "Feature comparison が図外",
                       has_feature)
        elif figure_rects:
            # 図ページ: 図外の本文テキストは少ないはず
            report.add(pno, "図外テキスト少量",
                       len(content_blocks) <= 5,
                       f"{len(content_blocks)}ブロック")

        # 5. 図内テキストに回路ラベルが含まれること (図ページのみ)
        if in_fig:
            fig_text = " ".join(_extract_block_text(b) for b in in_fig)
            has_label = any(kw in fig_text for kw in ["CM7", "Flash", "TCM", "DMA", "AHB"])
            report.add(pno, "図内に回路ラベル",
                       has_label,
                       f"{'検出' if has_label else '未検出'}")

    doc.close()


# ================================================================== #
#  E2E テスト (実際に翻訳して検証)
# ================================================================== #

def test_e2e(report: TestReport) -> None:
    """翻訳を実行し、図の保全とテキスト翻訳を検証する。"""
    from config import TranslationConfig
    from pipeline import run_translation

    output_pdf = Path("test_figure_filter_output.pdf")

    config = TranslationConfig(
        input_pdf=INPUT_PDF,
        output_pdf=output_pdf,
        pages=TEST_PAGES,
    )

    print(f"=== 翻訳実行: p{TEST_PAGES} ===\n")
    run_translation(config)

    orig_doc = pymupdf.open(str(INPUT_PDF))
    trans_doc = pymupdf.open(str(output_pdf))

    for i, pno in enumerate(TEST_PAGES):
        if pno >= len(trans_doc):
            report.add(pno, "ページ存在", False, "翻訳PDFに対応ページなし")
            continue

        orig_page = orig_doc[pno]
        trans_page = trans_doc[pno]

        # 1. 画像数が維持されること
        orig_imgs = len(orig_page.get_images())
        trans_imgs = len(trans_page.get_images())
        report.add(pno, "画像数維持",
                   orig_imgs == trans_imgs,
                   f"元={orig_imgs}, 訳={trans_imgs}")

        # 図領域を先に計算
        orig_text_dict = orig_page.get_text("dict")
        orig_blocks = orig_text_dict["blocks"]
        figure_rects = _get_figure_rects(orig_page, orig_blocks)

        # 2. 描画数チェック
        orig_drw = len(orig_page.get_drawings())
        trans_drw = len(trans_page.get_drawings())
        orig_tables_e2e = safe_tables(orig_page)
        if orig_tables_e2e and not figure_rects:
            # 表ページ: 表の再描画で罫線数が変わるのは想定内
            report.add(pno, "描画数(表ページ)",
                       trans_drw > 0,
                       f"元={orig_drw}, 訳={trans_drw}")
        else:
            # 図ページ: 描画が大幅に減っていないこと
            report.add(pno, "描画数維持",
                       trans_drw >= orig_drw * 0.8,
                       f"元={orig_drw}, 訳={trans_drw}")

        # 3. 図内テキスト（回路ラベル）が残っていること — 図ページのみ

        trans_text = trans_page.get_text()
        if figure_rects:
            labels_to_check = ["CM7", "TCM"]
            for label in labels_to_check:
                report.add(pno, f"図内ラベル'{label}'残存",
                           label in trans_text,
                           f"{'検出' if label in trans_text else '消失'}")

        # 4. 表の行列数が維持されること
        orig_tables = safe_tables(orig_page)
        trans_tables = safe_tables(trans_page)
        if orig_tables:
            report.add(pno, "表の数",
                       len(orig_tables) == len(trans_tables),
                       f"元={len(orig_tables)}, 訳={len(trans_tables)}")
            for ti in range(min(len(orig_tables), len(trans_tables))):
                ot = orig_tables[ti]
                tt = trans_tables[ti]
                shape_ok = (ot.row_count == tt.row_count
                            and ot.col_count == tt.col_count)
                report.add(pno, f"表{ti}行列数",
                           shape_ok,
                           f"元={ot.row_count}x{ot.col_count},"
                           f" 訳={tt.row_count}x{tt.col_count}")

            # 表のデータセルが消失していないこと
            for ti in range(min(len(orig_tables), len(trans_tables))):
                ot_data = orig_tables[ti].extract()
                tt_data = trans_tables[ti].extract()
                orig_filled = sum(
                    1 for row in ot_data for c in row
                    if (c or "").strip()
                )
                trans_filled = sum(
                    1 for row in tt_data for c in row
                    if (c or "").strip()
                )
                report.add(pno, f"表{ti}データ保持",
                           trans_filled >= orig_filled * 0.8,
                           f"元={orig_filled}セル, 訳={trans_filled}セル")

        # 5. p26: 図外テキストが日本語に翻訳されていること
        if pno == 26:
            # 図領域より下のテキストを取得
            fig_bottom = max(r.y1 for r in figure_rects) if figure_rects else 0
            below_fig_text = ""
            trans_text_dict = trans_page.get_text("dict")
            for b in trans_text_dict["blocks"]:
                if b["type"] == 0 and b["bbox"][1] > fig_bottom:
                    below_fig_text += "".join(
                        s["text"] for l in b["lines"] for s in l["spans"]
                    )
            report.add(pno, "図外テキスト日本語化",
                       has_japanese(below_fig_text),
                       f"{len(below_fig_text)}字")

    orig_doc.close()
    trans_doc.close()


# ================================================================== #
#  メイン
# ================================================================== #

def main() -> None:
    parser = argparse.ArgumentParser(description="図フィルタリング E2E テスト")
    parser.add_argument("--dry-run", action="store_true",
                        help="翻訳を実行せず図検出ロジックのみテスト")
    args = parser.parse_args()

    if not INPUT_PDF.exists():
        print(f"エラー: {INPUT_PDF} が見つかりません")
        sys.exit(1)

    report = TestReport()

    # 常に検出テストを実行
    print("=== 図検出テスト ===\n")
    test_detection(report)
    report.print_summary()

    if not args.dry_run:
        # E2E テストも実行
        e2e_report = TestReport()
        test_e2e(e2e_report)
        e2e_report.print_summary()

        all_ok = report.all_passed and e2e_report.all_passed
    else:
        all_ok = report.all_passed

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
