from __future__ import annotations
import os

import pymupdf

_MIN_FONTSIZE = 3.5
_TABLE_FONTSIZE = 8.0
_TABLE_HEADER_FONTSIZE = 8.0
_CELL_PAD = 3  # pt
_SUB_INDENT = "\u2003"  # em space — サブ項目 (—/–) のインデント
_CONT_INDENT = "\u2003\u2003"  # em space x2 — 継続行のインデント

# --- 日本語フォント設定 ---
_WIN_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\YuGothR.ttc",
    r"C:\Windows\Fonts\msgothic.ttc",
]

_JP_FONTFILE: str | None = None
_JP_FONTNAME = "jpfont"

for _p in _WIN_FONT_CANDIDATES:
    if os.path.exists(_p):
        _JP_FONTFILE = _p
        break


_BULLET_CHARS = ("•", "・", "—", "–")


def _join_wrapped(text: str) -> str:
    """箇条書き記号で始まらない行を前の行に結合する。

    バレット記号のみの行（例: "•\\n本文"）は次行と結合する。
    """
    lines = text.split("\n")
    if len(lines) <= 1:
        return text
    merged = [lines[0]]
    for line in lines[1:]:
        s = line.lstrip()
        prev = merged[-1].rstrip()
        # 前行がバレット記号のみ → 次行を結合
        if prev in _BULLET_CHARS:
            merged[-1] = prev + " " + line.lstrip()
        elif s.startswith(_BULLET_CHARS) or not s:
            merged.append(line)
        else:
            merged[-1] += " " + line
    return "\n".join(merged)


def _nbsp_after_bullet(line: str) -> str:
    """バレット記号直後のスペースをノーブレークスペースに置換し、
    セル幅での折り返しでバレットだけが行末に残るのを防ぐ。"""
    return (
        line.replace("• ", "•\xa0", 1)
            .replace("・ ", "・\xa0", 1)
            .replace("— ", "—\xa0", 1)
            .replace("– ", "–\xa0", 1)
    )


def _indent_bullets(text: str, *, preserve_all_newlines: bool = False) -> str:
    """箇条書きのサブ項目 (—/–) と継続行にインデントを付与する。

    preserve_all_newlines=True の場合、`_join_wrapped` による行結合をスキップする。
    表セルのように改行構造が LLM 側で既に保証されているケース向け。
    """
    if not preserve_all_newlines:
        text = _join_wrapped(text)
    lines = text.split("\n")
    result: list[str] = []
    in_sub = False
    for line in lines:
        s = line.lstrip()
        if s.startswith(("•", "・")):
            in_sub = False
            result.append(_nbsp_after_bullet(line))
        elif s.startswith(("—", "–")):
            in_sub = True
            result.append(_SUB_INDENT + _nbsp_after_bullet(line))
        elif in_sub and s:
            result.append(_CONT_INDENT + line)
        else:
            in_sub = False
            result.append(line)
    return "\n".join(result)


def _font_kwargs() -> dict:
    if _JP_FONTFILE:
        return {"fontname": _JP_FONTNAME, "fontfile": _JP_FONTFILE}
    return {"fontname": "japan"}


# ------------------------------------------------------------------ #
#  フォントサイズ自動縮小
# ------------------------------------------------------------------ #

# 試し書き用 Document をモジュールレベルで再利用 (毎回 pymupdf.open() しない)
_FIT_DOC: pymupdf.Document | None = None


def _get_fit_doc() -> pymupdf.Document:
    global _FIT_DOC
    if _FIT_DOC is None:
        _FIT_DOC = pymupdf.open()
    return _FIT_DOC


def _find_fitting_fontsize(
    rect: pymupdf.Rect,
    text: str,
    max_fontsize: float,
) -> float:
    """rect 内に text が収まる最大フォントサイズを返す (二分探索)。"""
    if rect.width < 2 or rect.height < 2:
        return _MIN_FONTSIZE

    test_rect = pymupdf.Rect(0, 0, rect.width, rect.height)
    fkw = _font_kwargs()
    td = _get_fit_doc()

    def _fits(fs: float) -> bool:
        tp = td.new_page(width=rect.width + 10, height=rect.height + 10)
        try:
            rc = tp.insert_textbox(test_rect, text, fontsize=fs, **fkw)
        finally:
            td.delete_page(-1)
        return rc >= 0

    # まず max_fontsize で OK なら即返す (最頻ケース)
    if _fits(max_fontsize):
        return max_fontsize
    if not _fits(_MIN_FONTSIZE):
        return _MIN_FONTSIZE

    # 二分探索: step=0.5 単位で最大 fit サイズを見つける
    # max_fontsize=9.5, min=3.5 なら (9.5-3.5)/0.5 = 12 段階 → log2(12)=~4 回
    lo, hi = _MIN_FONTSIZE, max_fontsize
    steps = int((hi - lo) / 0.5)
    while steps > 1:
        mid_steps = steps // 2
        mid = lo + mid_steps * 0.5
        if _fits(mid):
            lo = mid
            steps -= mid_steps
        else:
            hi = mid
            steps = mid_steps
    return lo


# ------------------------------------------------------------------ #
#  表の描画
# ------------------------------------------------------------------ #

def _render_table(
    page: pymupdf.Page,
    table,
    translated_rows: list[list[str]],
) -> None:
    """元の表位置に新しい表を描画する。"""
    rows = table.row_count
    cols = table.col_count
    table_rows = table.rows
    fkw = _font_kwargs()

    # --- Phase 1: セル背景と罫線 ---
    shape = page.new_shape()

    # ヘッダ行 (灰色背景)
    if table_rows:
        header_cells = table_rows[0].cells
        for c in range(min(cols, len(header_cells))):
            cell = header_cells[c]
            if cell is None:
                continue
            shape.draw_rect(pymupdf.Rect(cell))
        shape.finish(fill=(0.91, 0.91, 0.91), color=(0.4, 0.4, 0.4), width=0.5)

    # データ行 (白背景)
    for r in range(1, rows):
        if r >= len(table_rows):
            continue
        row_cells = table_rows[r].cells
        for c in range(min(cols, len(row_cells))):
            cell = row_cells[c]
            if cell is None:
                continue
            shape.draw_rect(pymupdf.Rect(cell))
        shape.finish(fill=(1, 1, 1), color=(0.4, 0.4, 0.4), width=0.5)

    shape.commit()

    # --- Phase 2: セルテキスト ---
    for r in range(rows):
        if r >= len(table_rows):
            continue
        row_cells = table_rows[r].cells
        for c in range(min(cols, len(row_cells))):
            cell = row_cells[c]
            if cell is None:
                continue
            text = translated_rows[r][c] if r < len(translated_rows) and c < len(translated_rows[r]) else ""
            if not text or not text.strip():
                continue

            text = _indent_bullets(text, preserve_all_newlines=True)

            cell_rect = pymupdf.Rect(cell)
            text_rect = cell_rect + (_CELL_PAD, _CELL_PAD, -_CELL_PAD, -_CELL_PAD)
            if text_rect.width < 2 or text_rect.height < 2:
                continue

            max_fs = _TABLE_HEADER_FONTSIZE if r == 0 else _TABLE_FONTSIZE
            fontsize = _find_fitting_fontsize(text_rect, text, max_fs)

            page.insert_textbox(
                text_rect, text,
                fontsize=fontsize,
                align=pymupdf.TEXT_ALIGN_LEFT,
                **fkw,
            )


# ------------------------------------------------------------------ #
#  メイン: テキスト差し替え + 表再描画
# ------------------------------------------------------------------ #

def _rect_key(rect) -> tuple:
    """Rect 座標を丸めてタプル化（浮動小数点比較の安定化）。"""
    return tuple(round(c, 2) for c in rect)


def _find_nearest(
    rects: list[pymupdf.Rect],
    origin: pymupdf.Rect,
) -> pymupdf.Rect | None:
    """origin に最も近い rect を返す。"""
    if not rects:
        return None
    ox, oy = origin.x0, origin.y0
    best = min(rects, key=lambda r: (r.x0 - ox) ** 2 + (r.y0 - oy) ** 2)
    return best


_LINK_COLOR = (0, 0, 0.8)  # リンク文字の色 (濃い青)


def _colorize_link_text(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
) -> None:
    """リンクテキストを青色で上書き描画する。

    既存テキストの span からフォントサイズとベースラインを取得し、
    insert_text で描画することで周囲と同じ見た目を維持する。
    """
    if not text or rect.is_empty or rect.width < 1:
        return

    # 既存テキストの span からフォントサイズ・ベースラインを取得
    fontsize = None
    baseline_y = None
    text_dict = page.get_text("dict")
    for block in text_dict["blocks"]:
        if block.get("type", 0) != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                span_rect = pymupdf.Rect(span["bbox"])
                if rect.intersects(span_rect):
                    fontsize = span["size"]
                    baseline_y = span["origin"][1]
                    break
            if fontsize:
                break
        if fontsize:
            break

    if fontsize is None:
        return  # 対応する span が見つからない場合はスキップ

    fkw = _font_kwargs()
    # 白背景で元の黒テキストを隠す
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(fill=(1, 1, 1), color=(1, 1, 1), width=0)
    shape.commit()
    # rect が複数行相当 (高さが fontsize の 1.8 倍超) なら insert_textbox で折り返し描画。
    # それ以外は insert_text でベースラインを維持。
    if rect.height > fontsize * 1.8:
        page.insert_textbox(
            rect, text,
            fontsize=fontsize,
            color=_LINK_COLOR,
            align=pymupdf.TEXT_ALIGN_LEFT,
            **fkw,
        )
    else:
        page.insert_text(
            pymupdf.Point(rect.x0, baseline_y),
            text,
            fontsize=fontsize,
            color=_LINK_COLOR,
            **fkw,
        )


def _restore_links(
    page: pymupdf.Page,
    saved_links: list[dict],
    link_restore_info: list[tuple[dict, str, str, pymupdf.Rect | None]],
    *,
    linked_block_indices: set[int] | None = None,
) -> None:
    """redaction で消えたリンクを復元し、リンクテキストを青色で描画する。

    link_restore_info: [(link_dict, original_text, translated_text, block_rect), ...]
      pipeline 側でマーカーから抽出した翻訳済みリンクテキストと、
      リンク元ブロックの矩形(フォールバック用)を含む。

    linked_block_indices が与えられた場合、block_rect フォールバック経路では
    Phase 2 で既に青色描画済みなので colorize をスキップする (二重描画防止)。
    """
    if not saved_links:
        return

    _ = linked_block_indices  # 直接は参照しないが、呼び出し側の意図を明示するため受ける

    # pipeline 側からのリンク情報を rect_key でインデックス
    info_by_key: dict[tuple, tuple[str, str, pymupdf.Rect | None]] = {}
    for link, orig, trans, brect in link_restore_info:
        info_by_key[_rect_key(link["from"])] = (orig, trans, brect)

    # 既存リンクを全削除（座標更新のため再挿入する）
    for existing in page.get_links():
        try:
            page.delete_link(existing)
        except Exception:
            pass

    for link in saved_links:
        key = _rect_key(link["from"])
        new_rect = None
        display_text = ""
        used_block_rect = False  # 青色再描画の要否判定用
        orig_text, trans_text, block_rect = info_by_key.get(key, ("", "", None))

        origin = pymupdf.Rect(link["from"])

        # TOC のような「行全体を覆うリンク」かを判定。
        # word search が 1 単語だけヒットしてその矩形だけが青塗りされるのを防ぐため、
        # 原リンクが block の大半を覆っているなら word search をスキップし block_rect を使う。
        is_wide_link = (
            block_rect is not None
            and block_rect.width > 1
            and origin.width >= block_rect.width * 0.5
        )

        # 1) 翻訳後テキストで検索（マーカーで特定済み）
        if trans_text:
            hits = page.search_for(trans_text)
            if hits:
                new_rect = _find_nearest(hits, origin)
                display_text = trans_text
        # 2) 元テキスト全体で検索
        if new_rect is None and orig_text:
            hits = page.search_for(orig_text)
            if hits:
                new_rect = _find_nearest(hits, origin)
                display_text = orig_text
        # 3a) 行全体リンクは block_rect を直接使う
        #     (TOC のように リンク矩形 ≒ ブロック矩形 で word search だと
        #      主要単語 1 つ分だけの狭い矩形になってしまうため)
        if new_rect is None and is_wide_link and block_rect is not None:
            new_rect = block_rect
            display_text = trans_text or orig_text
            used_block_rect = True
        # 3b) 狭いインラインリンクは原文の主要単語で個別検索
        if new_rect is None and orig_text:
            import re as _re
            words = _re.findall(r"[A-Za-z0-9]{2,}", orig_text)
            for word in words:
                hits = page.search_for(word)
                if hits:
                    new_rect = _find_nearest(hits, origin)
                    display_text = word
                    break
        # 4) 最終フォールバック → リンク元ブロックの矩形を使う
        if new_rect is None and block_rect is not None:
            new_rect = block_rect
            display_text = trans_text or orig_text
            used_block_rect = True

        restored = dict(link)
        if new_rect is not None:
            restored["from"] = new_rect
        try:
            page.insert_link(restored)
        except Exception:
            pass

        # リンクテキストを青色で描画。
        # ただし block_rect フォールバックの場合は Phase 2 で既に青色描画済みなのでスキップ。
        if new_rect is not None and display_text and not used_block_rect:
            _colorize_link_text(page, new_rect, display_text)


def replace_page_content(
    page: pymupdf.Page,
    non_table_blocks: list[dict],
    translated_texts: list[str],
    translated_tables: list[tuple],
    *,
    link_restore_info: list[tuple[dict, str, str, pymupdf.Rect | None]] | None = None,
    linked_block_indices: set[int] | None = None,
) -> None:
    """テキスト差し替え + 表を新規描画。画像・図形は維持。

    Args:
        link_restore_info: [(link_dict, original_text, translated_text, block_rect), ...]
            pipeline 側で抽出したリンク情報。None の場合は元座標で復元。
            block_rect は search 全滅時のフォールバック用。
        linked_block_indices: 行全体リンク(TOC)対象のブロック index 集合。
            Phase 2 でこれらのブロックを青色で描画し、Phase 4 の colorize 二重描画を防ぐ。
    """

    linked_block_indices = linked_block_indices or set()

    # --- リンク保存（redaction で消えるため） ---
    saved_links = page.get_links()

    # Phase 1-A: 表外テキストを除去 (画像・罫線は保持)
    for block in non_table_blocks:
        page.add_redact_annot(pymupdf.Rect(block["bbox"]), fill=(1, 1, 1))
    if non_table_blocks:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )

    # Phase 1-B: 表エリアを丸ごと除去 (罫線ごと消す → 新規描画するため)
    for table, _ in translated_tables:
        page.add_redact_annot(pymupdf.Rect(table.bbox), fill=(1, 1, 1))
    if translated_tables:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
        )

    fkw = _font_kwargs()

    # Phase 2: 表外テキストを挿入
    for bi, (block, translated) in enumerate(zip(non_table_blocks, translated_texts)):
        if not translated.strip():
            continue
        translated = _indent_bullets(translated)
        rect = pymupdf.Rect(block["bbox"])
        original_fs = block["lines"][0]["spans"][0]["size"]
        fontsize = _find_fitting_fontsize(rect, translated, original_fs)
        # 行全体リンク対象ブロックは青色で描画 (Phase 4 で再描画しないため)
        text_color = _LINK_COLOR if bi in linked_block_indices else None
        kwargs = dict(fkw)
        if text_color is not None:
            kwargs["color"] = text_color
        page.insert_textbox(
            rect, translated,
            fontsize=fontsize,
            align=pymupdf.TEXT_ALIGN_LEFT,
            **kwargs,
        )

    # Phase 3: 表を新規描画
    for table, translated_rows in translated_tables:
        _render_table(page, table, translated_rows)

    # Phase 4: リンク復元（翻訳後テキスト位置に再配置）
    _restore_links(page, saved_links, link_restore_info or [],
                   linked_block_indices=linked_block_indices)

    page.clean_contents()
