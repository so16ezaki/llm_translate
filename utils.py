"""
テスト共通ユーティリティ

test_e2e.py / test_compare.py で共有するヘルパーをまとめたモジュール。
"""
from __future__ import annotations
import subprocess

import pymupdf
import pymupdf4llm


# ------------------------------------------------------------------ #
#  Windows の cp932 エラー抑制（pymupdf4llm が内部でサブプロセスを使うため）
# ------------------------------------------------------------------ #

_orig_popen = subprocess.Popen


class _SafePopen(_orig_popen):
    def __init__(self, *args, **kwargs):
        if kwargs.get("encoding") or kwargs.get("text") or kwargs.get("universal_newlines"):
            kwargs.setdefault("errors", "replace")
        super().__init__(*args, **kwargs)


subprocess.Popen = _SafePopen  # type: ignore[misc]


# ------------------------------------------------------------------ #
#  Markdown 変換
# ------------------------------------------------------------------ #

def page_to_markdown(doc: pymupdf.Document, page_no: int) -> str:
    """指定ページを Markdown 文字列に変換する。失敗時は get_text() にフォールバック。"""
    try:
        return pymupdf4llm.to_markdown(
            doc, pages=[page_no], show_progress=False,
            ignore_images=True, ignore_graphics=True,
        )
    except Exception as e:
        return f"[markdown変換エラー: {e}]\n{doc[page_no].get_text()}"


# ------------------------------------------------------------------ #
#  日本語検出
# ------------------------------------------------------------------ #

def has_japanese(text: str) -> bool:
    """ひらがな・カタカナ・CJK統合漢字のいずれかを含むなら True。"""
    for ch in text:
        cp = ord(ch)
        if (
            0x3040 <= cp <= 0x309F  # ひらがな
            or 0x30A0 <= cp <= 0x30FF  # カタカナ
            or 0x4E00 <= cp <= 0x9FFF  # CJK統合漢字
        ):
            return True
    return False


# ------------------------------------------------------------------ #
#  表ユーティリティ
# ------------------------------------------------------------------ #

def safe_tables(page: pymupdf.Page) -> list:
    """bbox / row_count / col_count が正常に取得できる表のみ返す。"""
    out = []
    for t in page.find_tables().tables:
        try:
            _ = t.bbox
            _ = t.row_count
            _ = t.col_count
            out.append(t)
        except (ValueError, IndexError):
            pass
    return out
