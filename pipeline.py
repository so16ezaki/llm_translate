from __future__ import annotations
import json
import re
import time
import concurrent.futures
from dataclasses import dataclass

import pymupdf

from config import TranslationConfig
from client import make_client
from renderer import replace_page_content

_GROUP_X_TOL = 8.0        # 同一グループとみなす x0 の誤差 (pt)
_GROUP_Y_GAP = 25.0       # ブロック間の最大 y ギャップ (pt)
_GROUP_MAX_BLOCKS = 5     # 1 グループあたりの最大ブロック数
_GROUP_MAX_CHARS = 400    # 1 グループあたりの最大文字数
_WIDE_TABLE_COLS = 8      # これ以上の列数の表はセル単位翻訳に切り替え


def _is_figure_page(page: pymupdf.Page, min_chars: int) -> bool:
    """テキスト量が min_chars 未満なら図ページと判定する。"""
    return len(page.get_text().strip()) < min_chars


def _extract_block_text(block: dict) -> str:
    """テキストブロックから文字列を組み立てる。"""
    lines: list[str] = []
    for line in block["lines"]:
        line_text = "".join(span["text"] for span in line["spans"])
        lines.append(line_text)
    return "\n".join(lines)


def _block_in_table(block: dict, table_rects: list[pymupdf.Rect]) -> bool:
    br = pymupdf.Rect(block["bbox"])
    return any(tr.intersects(br) for tr in table_rects)


# ------------------------------------------------------------------ #
#  リンク ↔ ブロック マッピング & マーカー処理
# ------------------------------------------------------------------ #
_MARKER_RE = re.compile(r"⟪/?(\d+)⟫")

# LLM が「第18章 18 クロスバー...」のように章番号を直後に重複出力することがある。
# その「第N章 N」の N を削除して「第N章」に正規化する。
_CHAPTER_DUP_RE = re.compile(r"(第(\d+)章)[\s\u00a0\n]+\2(?=[\s\u00a0\n]|\b)")

# LLM が「章 18 タイトル」「章\n18\nタイトル」のように「第」を省略・改行分断することがある。
# これを「第18章 タイトル」に正規化する。
_CHAPTER_PREFIX_RE = re.compile(r"(?<![\d\u4e00-\u9fff])章[\s\u00a0\n]+(\d+)[\s\u00a0\n]+")


def _normalize_chapter_prefix(text: str) -> str:
    """「章 N ...」「章\\nN\\n...」→「第N章 ...」に正規化。"""
    if not text or "章" not in text:
        return text
    return _CHAPTER_PREFIX_RE.sub(r"第\1章 ", text)


def _dedup_chapter_number(text: str) -> str:
    """「第N章 N ...」→「第N章 ...」に正規化 (LLM の章番号重複出力対策)。"""
    if not text:
        return text
    text = _normalize_chapter_prefix(text)
    return _CHAPTER_DUP_RE.sub(r"\1", text)


def _map_links_to_blocks(
    page: pymupdf.Page,
    non_table_blocks: list[dict],
) -> list[tuple[dict, str, int | None, int]]:
    """各リンクについて (link_dict, 元テキスト, block_index, char_offset) を返す。

    char_offset: リンクテキストの、ブロックテキスト内での開始文字位置。
    同じ文字列が複数出現する場合でも正しい位置を特定する。
    """
    links = page.get_links()
    text_dict = page.get_text("dict")
    result: list[tuple[dict, str, int | None, int]] = []
    for link in links:
        link_rect = pymupdf.Rect(link["from"])
        # リンク矩形内のテキストを収集
        parts: list[str] = []
        for block in text_dict["blocks"]:
            if block.get("type", 0) != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if link_rect.intersects(pymupdf.Rect(span["bbox"])):
                        parts.append(span["text"])
        text = "".join(parts).strip()

        # どのブロックに属するか & span 走査で文字オフセットを計算
        block_idx = None
        char_offset = -1
        for bi, b in enumerate(non_table_blocks):
            if not link_rect.intersects(pymupdf.Rect(b["bbox"])):
                continue
            block_idx = bi
            offset = 0
            found = False
            for li, line in enumerate(b["lines"]):
                if li > 0:
                    offset += 1  # "\n"
                for span in line["spans"]:
                    span_rect = pymupdf.Rect(span["bbox"])
                    if link_rect.intersects(span_rect) and not found:
                        char_offset = offset
                        found = True
                    offset += len(span["text"])
                if found:
                    break
            break
        result.append((link, text, block_idx, char_offset))
    return result


def _inject_link_markers(
    src_per_block: list[str],
    link_info: list[tuple[dict, str, int | None, int]],
) -> tuple[list[str], dict[int, tuple[dict, str]]]:
    """ソーステキストにリンクマーカー ⟪n⟫...⟪/n⟫ を埋め込む。

    char_offset (link_info[3]) を使い、同一文字列の複数出現でも
    正しい位置にマーカーを挿入する。

    Returns:
        (marked_src_per_block, marker_map)
        marker_map: {marker_id: (link_dict, original_text)}
    """
    # ブロック別にリンクを集約
    block_links: dict[int, list[tuple[int, dict, str, int]]] = {}
    marker_map: dict[int, tuple[dict, str]] = {}
    mid = 0
    for link, text, block_idx, char_offset in link_info:
        if block_idx is None or not text:
            continue
        block_links.setdefault(block_idx, []).append((mid, link, text, char_offset))
        marker_map[mid] = (link, text)
        mid += 1

    if not marker_map:
        return src_per_block, marker_map

    marked = list(src_per_block)
    for block_idx, entries in block_links.items():
        src = marked[block_idx]
        # char_offset を使って正確な位置にマーカーを挿入
        positioned: list[tuple[int, int, int, str]] = []
        for m_id, _link, text, char_offset in entries:
            if char_offset >= 0:
                # span 走査で得た正確なオフセットを使用
                pos = char_offset
            else:
                # フォールバック: str.find()
                pos = src.find(text)
            if 0 <= pos <= len(src) - len(text):
                positioned.append((pos, len(text), m_id, text))
        # 末尾から挿入して位置ずれを防ぐ
        positioned.sort(key=lambda x: x[0], reverse=True)
        for pos, length, m_id, text in positioned:
            src = src[:pos] + f"⟪{m_id}⟫" + text + f"⟪/{m_id}⟫" + src[pos + length:]
        marked[block_idx] = src
    return marked, marker_map


def _parse_link_markers(
    translated_texts: list[str],
    marker_map: dict[int, tuple[dict, str]],
) -> tuple[list[str], dict[int, str]]:
    """翻訳結果からマーカーを解析し、翻訳済みリンクテキストを抽出。

    LLM が marker 内に改行を挿入したケース (例: "⟪0⟫第18章\n18\nクロスバー...⟪/0⟫")
    にも対応するため DOTALL でマッチし、marker 内の改行は半角スペースに正規化する。

    Returns:
        (clean_texts, translated_link_texts)
        translated_link_texts: {marker_id: 翻訳後のリンクテキスト}
    """
    translated_link_texts: dict[int, str] = {}
    clean: list[str] = []

    def _normalize_ws(s: str) -> str:
        return re.sub(r"\s*\n\s*", " ", s).strip()

    for text in translated_texts:
        for mid in marker_map:
            pattern = rf"⟪{mid}⟫(.*?)⟪/{mid}⟫"
            m = re.search(pattern, text, flags=re.DOTALL)
            if m:
                translated_link_texts[mid] = _normalize_ws(m.group(1))

        # marker 内側の改行を半角スペースに畳み込んでから marker 記号を除去する。
        # そうしないと insert_textbox で「第18章\n18\nクロスバー...」と 3 行描画されてしまう。
        def _collapse(m):
            return _normalize_ws(m.group(1))

        cleaned = re.sub(
            r"⟪\d+⟫(.*?)⟪/\d+⟫",
            _collapse,
            text,
            flags=re.DOTALL,
        )
        # 孤立マーカー (対応閉じタグなし等) を除去
        cleaned = _MARKER_RE.sub("", cleaned)
        clean.append(cleaned)
    return clean, translated_link_texts


_GRID_CELL = 40.0       # 密度検出のグリッドセルサイズ (pt)
_DENSITY_THRESH = 5     # セル内の描画数がこれ以上で「図」セルと判定
_MIN_FIGURE_DIM = 100.0 # マージ後の矩形がこれ以上で図領域として採用

# ヘッダ/フッタを除いたコンテンツ領域の左右マージン
_CONTENT_X0 = 40.0
_CONTENT_X1 = 560.0


def _get_figure_rects(page: pymupdf.Page, blocks: list[dict]) -> list[pymupdf.Rect]:
    """ページ内の図（画像ブロック + 描画密集領域）を検出する。

    検出戦略:
      1. 画像ブロック・密度グリッドから図の「種」(seed) を作る
      2. "Figure N." キャプションをページ内から探す
      3. キャプションの直上にある seed を見つけ、
         seed 上端 〜 キャプション下端 の全幅を図領域とする
      4. キャプションが見つからない seed は従来通りマージ結果を使う
    """
    import re
    from collections import defaultdict

    seeds: list[pymupdf.Rect] = []

    # 1) 画像ブロック (type == 1)
    for b in blocks:
        if b["type"] == 1:
            seeds.append(pymupdf.Rect(b["bbox"]))

    # 2) 描画密度ベースの検出
    drawings = page.get_drawings()
    if drawings:
        grid: dict[tuple[int, int], int] = defaultdict(int)
        for d in drawings:
            r = pymupdf.Rect(d["rect"])
            cx = int(r.x0 // _GRID_CELL)
            cy = int(r.y0 // _GRID_CELL)
            grid[(cx, cy)] += 1
        for (cx, cy), cnt in grid.items():
            if cnt >= _DENSITY_THRESH:
                seeds.append(pymupdf.Rect(
                    cx * _GRID_CELL, cy * _GRID_CELL,
                    (cx + 1) * _GRID_CELL, (cy + 1) * _GRID_CELL,
                ))

    # 3) seed をマージし、小さすぎるものを除外
    merged = _merge_rects(seeds, margin=5)
    clusters = [r for r in merged if r.width >= _MIN_FIGURE_DIM
                and r.height >= _MIN_FIGURE_DIM]

    # 4) "Figure N." キャプションを収集
    caption_pattern = re.compile(r"^Figure\s+\d+")
    captions: list[pymupdf.Rect] = []
    for b in blocks:
        if b["type"] != 0:
            continue
        text = "".join(
            span["text"] for line in b["lines"] for span in line["spans"]
        )
        if caption_pattern.match(text.strip()):
            captions.append(pymupdf.Rect(b["bbox"]))

    # 5) 各キャプションに最も近い直上クラスタを探し、全幅の図領域を生成
    used_clusters: set[int] = set()
    figures: list[pymupdf.Rect] = []

    for cap in captions:
        best_idx = -1
        best_dist = 300.0  # キャプションとクラスタ下端の最大許容距離
        for ci, cl in enumerate(clusters):
            if ci in used_clusters:
                continue
            # クラスタの下端がキャプション上端より上にある
            dist = cap.y0 - cl.y1
            if -20 <= dist < best_dist:
                best_dist = dist
                best_idx = ci
        if best_idx >= 0:
            used_clusters.add(best_idx)
            cl = clusters[best_idx]
            # クラスタ上端 〜 キャプション下端、全幅
            figures.append(pymupdf.Rect(
                _CONTENT_X0, cl.y0,
                _CONTENT_X1, cap.y1,
            ))

    # キャプションと紐付かなかったクラスタはそのまま追加
    for ci, cl in enumerate(clusters):
        if ci not in used_clusters:
            figures.append(cl)

    # 6) "Diagram" / "Block diagram" / "Register layout" のような見出しラベル直下を
    #    強制的に図領域として登録する (ビットフィールド図のように drawings が疎で
    #    seed として検出されず、かつ pymupdf が表として誤検出してしまうケースの救済)。
    diagram_label_pattern = re.compile(
        r"^(?:Diagram|Block\s+diagram|Register\s+layout)\b[\s:.]*$",
        re.IGNORECASE,
    )
    for b in blocks:
        if b.get("type", 0) != 0:
            continue
        text = "".join(
            span["text"] for line in b["lines"] for span in line["spans"]
        ).strip()
        # 独立した短い見出し行のみ対象 (本文中の同文字を誤検出しないため)
        if not diagram_label_pattern.match(text) or len(text) > 30:
            continue
        cap = pymupdf.Rect(b["bbox"])
        # キャプション下から「次の短い独立見出し」または 200pt までを図領域とする。
        # (典型ビットフィールド図は 100-150pt。しかし次セクションの見出し=
        #  "Fields" などが出たらそこで打ち切って本物の表を巻き込まない)
        end_y = cap.y1 + 200
        for ob in blocks:
            if ob.get("type", 0) != 0 or ob is b:
                continue
            ob_rect = pymupdf.Rect(ob["bbox"])
            # キャプション直下から end_y までの領域内で検索
            if ob_rect.y0 <= cap.y1 + 20 or ob_rect.y0 >= end_y:
                continue
            ob_text = "".join(s["text"] for l in ob["lines"] for s in l["spans"]).strip()
            # 左端寄り (x < 100) で短く (<= 20 chars)、英字のみ独立単語
            # → セクション見出しと判定してそこで打ち切り
            if (
                0 < len(ob_text) <= 20
                and ob_rect.x0 < 100
                and re.match(r"^[A-Z][A-Za-z]+$", ob_text)
            ):
                end_y = min(end_y, ob_rect.y0)
                break
        figures.append(pymupdf.Rect(
            _CONTENT_X0, cap.y1, _CONTENT_X1, end_y,
        ))

    return figures


def _merge_rects(
    rects: list[pymupdf.Rect], margin: float = 10,
) -> list[pymupdf.Rect]:
    """重なり合う or 近接する矩形をマージする。"""
    if not rects:
        return []
    merged = [pymupdf.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        new: list[pymupdf.Rect] = []
        used = [False] * len(merged)
        for i in range(len(merged)):
            if used[i]:
                continue
            current = pymupdf.Rect(merged[i])
            expanded = current + (-margin, -margin, margin, margin)
            for j in range(i + 1, len(merged)):
                if used[j]:
                    continue
                if expanded.intersects(merged[j]):
                    current |= merged[j]
                    expanded = current + (-margin, -margin, margin, margin)
                    used[j] = True
                    changed = True
            new.append(current)
        merged = new
    return merged


def _block_in_figure(block: dict, figure_rects: list[pymupdf.Rect]) -> bool:
    br = pymupdf.Rect(block["bbox"])
    return any(fr.intersects(br) for fr in figure_rects)


def _join_wrapped_lines(text: str) -> str:
    """PDF の折り返し行を結合する。

    bullet (•) や sub-item (—/–) で始まらない行は
    前の行の続きとみなしてスペースで結合する。
    """
    lines = text.split("\n")
    if len(lines) <= 1:
        return text
    merged = [lines[0]]
    for line in lines[1:]:
        s = line.lstrip()
        if s.startswith(("•", "—", "–")) or not s:
            merged.append(line)
        else:
            merged[-1] += " " + line
    return "\n".join(merged)


# セル内改行を表す壊れにくいマーカー。
# 旧形式の "<br>" も復元時に受け付ける (後方互換・キャッシュ互換)。
_CELL_NL = "⟦NL⟧"


def _encode_cell_for_prompt(cell: str) -> str:
    """複数行セルを JSON 配列文字列にエンコードする。単行セルは素のまま。

    `table.extract()` が返す `\n` をそのまま配列境界として保存することで、
    LLM は各行を独立した翻訳単位として扱える。
    """
    if cell is None:
        return ""
    lines = cell.split("\n")
    if len(lines) <= 1:
        return cell.strip()
    # ensure_ascii=False で日本語/漢字もそのまま出力
    return json.dumps([l.rstrip() for l in lines], ensure_ascii=False)


def _decode_translated_cell(translated: str, expected_lines: int | None = None) -> str:
    """LLM が返したセル文字列を実改行テキストに復元する。

    優先順:
      1. JSON 配列 (`[".."]`) → json.loads で復元
      2. 旧マーカー (`⟦NL⟧` / `<br>`) → `_restore_cell_newlines`
      3. その他 → そのまま返す
    """
    if not translated:
        return ""
    s = translated.strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list) and all(isinstance(x, str) for x in arr):
                if expected_lines is not None and len(arr) != expected_lines:
                    print(
                        f"    [warn] cell array length mismatch: "
                        f"expected={expected_lines} got={len(arr)}"
                    )
                return "\n".join(arr)
        except json.JSONDecodeError:
            pass
    return _restore_cell_newlines(s)


def _table_to_markdown(data: list[list[str]]) -> str:
    if not data:
        return ""
    clean = [[_encode_cell_for_prompt(c or "") for c in row] for row in data]
    lines = []
    lines.append("| " + " | ".join(clean[0]) + " |")
    lines.append("| " + " | ".join("---" for _ in clean[0]) + " |")
    for row in clean[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _restore_cell_newlines(text: str) -> str:
    """セル内改行マーカーを実改行に戻す (旧形式用・キャッシュ互換)。"""
    return text.replace(_CELL_NL, "\n").replace("<br>", "\n")


def _needs_translation(text: str) -> bool:
    """セルのテキストが翻訳を必要とするか判定する。

    数値のみ、空、ダッシュのみ、型番（S32K…）などはスキップ。
    """
    import re
    s = text.replace("\n", " ").strip()
    if not s:
        return False
    # 数値・ダッシュ・スペースのみ
    if re.fullmatch(r"[\d\s.,\-—–/()]+", s):
        return False
    # 型番パターン (S32K…, CM7…)
    if re.fullmatch(r"[A-Z0-9][A-Z0-9_\-./\s]+", s) and len(s) < 30:
        return False
    return True


def _translate_table_cells(
    client,
    data: list[list[str]],
) -> tuple[list[list[str]], int]:
    """広い表を構造保持しつつコンテキスト付きで翻訳する。

    1. 翻訳対象セルを番号付きリストで送信。複数行セルは JSON 配列化。
    2. LLM 応答をパースして元のセルにマッピング。
    3. 複数行セルは `_decode_translated_cell` で JSON → 実改行に復元。
    """
    import re

    # table.extract() が返す生データをそのまま保持 (merge しない)
    # 各セルは末尾ホワイトスペースのみ除去
    clean_data = [
        [(c or "").rstrip() for c in row]
        for row in data
    ]

    # 翻訳対象セルを収集 (row, col, text)
    targets: list[tuple[int, int, str]] = []
    for ri, row in enumerate(clean_data):
        for ci, cell in enumerate(row):
            if cell and _needs_translation(cell):
                targets.append((ri, ci, cell))

    if not targets:
        return clean_data, 0

    # 番号付きリストで翻訳依頼。複数行セルは JSON 配列で送る。
    header_cells = [c for c in clean_data[0] if c] if clean_data else []
    col_hint = ", ".join(h.replace("\n", " ") for h in header_cells[:5])
    items = "\n".join(
        f"{i+1}. {_encode_cell_for_prompt(t[2])}" for i, t in enumerate(targets)
    )
    prompt = (
        f"Context: technical table (columns: {col_hint}, ...).\n"
        f"Keep the numbering. Items marked as JSON arrays must stay as arrays "
        f"with the same element count; translate each string element.\n"
        f"Translate each item:\n\n"
        f"{items}"
    )
    translated, ok = client.translate_markdown(prompt, hint="table")

    if not ok:
        return clean_data, 1

    # 番号付きリストをパース。JSON 配列が複数行にまたがる可能性があるため、
    # "\nN." を次項目の境界として分割する。
    mapping: dict[int, str] = {}
    item_re = re.compile(r"(?:^|\n)(\d+)\.\s*", re.DOTALL)
    matches = list(item_re.finditer(translated))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(translated)
        body = translated[start:end].strip()
        if body:
            mapping[num] = body

    fail = 0
    for i, (ri, ci, orig) in enumerate(targets):
        num = i + 1
        if num in mapping and mapping[num]:
            orig_line_count = len(orig.split("\n")) if "\n" in orig else 1
            expected = orig_line_count if orig_line_count > 1 else None
            clean_data[ri][ci] = _decode_translated_cell(mapping[num], expected)
        else:
            # パース失敗 → 元テキストのまま
            fail += 1

    return clean_data, fail


def _parse_markdown_table(md: str, expected_cols: int) -> list[list[str]] | None:
    lines = [l.strip() for l in md.strip().split("\n") if l.strip()]
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.replace("|", "").replace("-", "").replace(":", "").strip()
        if not stripped:
            continue
        if "|" not in line:
            continue
        cells = [_decode_translated_cell(c.strip()) for c in line.strip().strip("|").split("|")]
        if len(cells) == expected_cols:
            rows.append(cells)
    return rows if rows else None


# ------------------------------------------------------------------ #
#  近接ブロックのグループ化
# ------------------------------------------------------------------ #

def _group_blocks(blocks: list[dict]) -> list[list[int]]:
    """近接するテキストブロックをグループ化し、インデックスのリストを返す。

    同じ左端 (x0) かつ縦に近い連続ブロックを1グループにまとめる。
    箇条書きのように1行1ブロックになるケースで、
    コンテキストを保ったまま一括翻訳するため。
    """
    if not blocks:
        return []

    # y0 でソート（元の順序を保つためインデックスも持つ）
    indexed = sorted(enumerate(blocks), key=lambda ib: ib[1]["bbox"][1])

    def _blk_chars(b: dict) -> int:
        return len(_extract_block_text(b))

    groups: list[list[int]] = []
    current_group: list[int] = [indexed[0][0]]
    current_chars: int = _blk_chars(indexed[0][1])
    prev_block = indexed[0][1]

    for idx, block in indexed[1:]:
        prev_rect = pymupdf.Rect(prev_block["bbox"])
        cur_rect = pymupdf.Rect(block["bbox"])

        same_x = abs(cur_rect.x0 - prev_rect.x0) < _GROUP_X_TOL
        close_y = (cur_rect.y0 - prev_rect.y1) < _GROUP_Y_GAP
        blk_chars = _blk_chars(block)
        size_ok = len(current_group) < _GROUP_MAX_BLOCKS
        chars_ok = (current_chars + blk_chars) <= _GROUP_MAX_CHARS

        if same_x and close_y and size_ok and chars_ok:
            current_group.append(idx)
            current_chars += blk_chars
        else:
            groups.append(current_group)
            current_group = [idx]
            current_chars = blk_chars
        prev_block = block

    groups.append(current_group)
    return groups


# ------------------------------------------------------------------ #
#  グループ/一括翻訳の共通処理
# ------------------------------------------------------------------ #

def _translate_batch(
    client,
    src_per_block: list[str],
    translated_texts: list[str],
    indices: list[int],
) -> int:
    """indices で指定されたブロックを1回のLLM呼び出しで翻訳する。

    translated_texts を直接更新する。戻り値は失敗数。
    """
    parts = [src_per_block[gi] for gi in indices]
    merged = "\n".join(parts)

    if not merged.strip():
        for j, gi in enumerate(indices):
            translated_texts[gi] = parts[j]
        return 0

    if len(indices) == 1:
        t, ok = client.translate_markdown(merged)
        translated_texts[indices[0]] = t
        return 0 if ok else 1

    # 複数ブロック → 一括翻訳
    t, ok = client.translate_markdown(merged)
    if not ok:
        for j, gi in enumerate(indices):
            translated_texts[gi] = parts[j]
        return 1

    # 各 part に含まれる marker ID を抽出し、marker ベースで分配を試みる。
    # (LLM が marker 内外に不要な改行を入れても、marker 境界でブロック単位に
    #  正しく分けられるため、行数ベース分配の取りこぼしを防ぐ)
    per_block_markers: list[list[int]] = []
    for p in parts:
        per_block_markers.append([int(mid) for mid in _MARKER_RE.findall(p)])

    can_marker_split = all(mids for mids in per_block_markers) and len(
        {mid for mids in per_block_markers for mid in mids}
    ) == sum(len(mids) for mids in per_block_markers)

    if can_marker_split:
        # 各ブロックの末尾 marker (⟪/N⟫) の位置で翻訳結果を切る
        end_ids = [mids[-1] for mids in per_block_markers]
        cuts: list[int] = []
        search_from = 0
        for end_id in end_ids:
            close = f"⟪/{end_id}⟫"
            idx = t.find(close, search_from)
            if idx < 0:
                cuts = []
                break
            cuts.append(idx + len(close))
            search_from = cuts[-1]
        if cuts:
            prev = 0
            for j, gi in enumerate(indices):
                chunk_text = t[prev:cuts[j]].strip()
                translated_texts[gi] = chunk_text
                prev = cuts[j]
            return 0

    # marker 分配ができない場合 → 行ベースフォールバック
    translated_lines = t.split("\n")
    n = len(indices)

    if len(translated_lines) >= n:
        # 行数が足りる → 均等に分配
        chunk = len(translated_lines) // n
        rem = len(translated_lines) % n
        pos = 0
        for j, gi in enumerate(indices):
            take = chunk + (1 if j < rem else 0)
            translated_texts[gi] = "\n".join(translated_lines[pos:pos + take])
            pos += take
        return 0

    # 行数不足 → 個別翻訳にフォールバック
    # (先頭に全部入れ残りを空にするとブロックがページから消えるため)
    fail = 0
    for j, gi in enumerate(indices):
        t_i, ok_i = client.translate_markdown(parts[j])
        translated_texts[gi] = t_i if ok_i else parts[j]
        if not ok_i:
            fail += 1
    return fail


# ------------------------------------------------------------------ #
#  メイン
# ------------------------------------------------------------------ #

@dataclass
class PageData:
    page_no: int
    non_table_blocks: list[dict]
    marked_src: list[str]
    marker_map: dict[int, tuple[dict, str]]
    link_info: list[tuple[dict, str, int | None, int]]
    
    valid_tables: list[pymupdf.Table]
    table_data_list: list[list[list[str]]]
    table_col_counts: list[int]
    
    groups: list[list[int]]

    translated_texts: list[str]
    translated_tables_rows: list[list[list[str]]]
    translated_link_texts: dict[int, str] = None
    
    block_fail: int = 0


def _extract_chunk(
    doc: pymupdf.Document,
    chunk_pages: list[int],
    config: TranslationConfig,
    start: float,
) -> list[PageData]:
    """Phase 1: 指定チャンクのページからテキスト・表を抽出する。"""
    page_data_list: list[PageData] = []
    n = len(chunk_pages)
    progress_step = max(1, n // 10) if n >= 30 else max(1, n)
    errors = 0

    for i, page_no in enumerate(chunk_pages):
        if i > 0 and (i % progress_step == 0 or i == n - 1):
            elapsed = time.monotonic() - start
            print(f"    抽出: {i}/{n} ({i*100//n}%) 経過 {elapsed:.0f}s")

        try:
            page = doc[page_no]

            if _is_figure_page(page, config.min_text_chars):
                continue

            text_dict = page.get_text("dict")
            all_blocks = text_dict["blocks"]
            figure_rects = _get_figure_rects(page, all_blocks)

            try:
                tables_raw = page.find_tables()
            except Exception as e:
                print(f"  [p{page_no}] find_tables 失敗 ({type(e).__name__}) → 表なしで続行")
                tables_raw = type("X", (), {"tables": []})()
            valid_tables = []
            for t in tables_raw.tables:
                try:
                    tr = pymupdf.Rect(t.bbox)
                    if any(fr.intersects(tr) for fr in figure_rects):
                        continue
                    valid_tables.append(t)
                except (ValueError, IndexError):
                    pass
            table_rects = [pymupdf.Rect(t.bbox) for t in valid_tables]

            all_text_blocks = [b for b in all_blocks if b["type"] == 0]
            non_table_blocks = [b for b in all_text_blocks
                               if not _block_in_table(b, table_rects)
                               and not _block_in_figure(b, figure_rects)]

            src_per_block = [_extract_block_text(b) for b in non_table_blocks]
            link_info = _map_links_to_blocks(page, non_table_blocks)
            marked_src, marker_map = _inject_link_markers(src_per_block, link_info)
            groups = _group_blocks(non_table_blocks)

            table_data_list = []
            table_col_counts = []
            for t in valid_tables:
                table_data_list.append(t.extract())
                table_col_counts.append(t.col_count)

            pd = PageData(
                page_no=page_no,
                non_table_blocks=non_table_blocks,
                marked_src=marked_src,
                marker_map=marker_map,
                link_info=link_info,
                valid_tables=valid_tables,
                table_data_list=table_data_list,
                table_col_counts=table_col_counts,
                groups=groups,
                translated_texts=[""] * len(non_table_blocks),
                translated_tables_rows=[None] * len(valid_tables),  # type: ignore
            )
            page_data_list.append(pd)
        except Exception as e:
            errors += 1
            print(f"  [p{page_no}] 抽出エラー ({type(e).__name__}: {e}) → スキップ")

    if errors:
        print(f"    抽出エラー {errors} ページ")
    return page_data_list


def _translate_chunk(
    client,
    page_data_list: list[PageData],
    config: TranslationConfig,
) -> None:
    """Phase 2: チャンク内の全ページをまとめて並列翻訳する。"""
    total_tasks = sum(len(pd.groups) + len(pd.table_data_list) for pd in page_data_list)
    if total_tasks == 0:
        return
    completed = 0

    def _task_group(pd: PageData, indices: list[int]):
        fail = _translate_batch(client, pd.marked_src, pd.translated_texts, indices)
        if fail:
            pd.block_fail += fail

    def _task_table(pd: PageData, ti: int):
        data = pd.table_data_list[ti]
        col_count = pd.table_col_counts[ti]
        if col_count >= _WIDE_TABLE_COLS:
            rows, cell_fail = _translate_table_cells(client, data)
            pd.block_fail += cell_fail
        else:
            md, ok = client.translate_markdown(_table_to_markdown(data), hint="table")
            parsed = _parse_markdown_table(md, col_count) if ok else None
            if ok and parsed:
                rows = parsed
            else:
                rows, cell_fail = _translate_table_cells(client, data)
                pd.block_fail += cell_fail
        pd.translated_tables_rows[ti] = rows

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        futures = []
        for pd in page_data_list:
            for gi in pd.groups:
                futures.append(executor.submit(_task_group, pd, gi))
            for ti in range(len(pd.table_data_list)):
                futures.append(executor.submit(_task_table, pd, ti))

        for fut in concurrent.futures.as_completed(futures):
            fut.result()
            completed += 1
            if completed % 20 == 0 or completed == total_tasks:
                print(f"    翻訳タスク進捗: {completed} / {total_tasks}")

    # マーカー解析・章番号正規化
    for pd in page_data_list:
        if pd.marker_map:
            pd.translated_texts, pd.translated_link_texts = _parse_link_markers(
                pd.translated_texts, pd.marker_map,
            )
        else:
            pd.translated_link_texts = {}
        pd.translated_texts = [_dedup_chapter_number(t) for t in pd.translated_texts]
        pd.translated_link_texts = {
            mid: _dedup_chapter_number(t) for mid, t in pd.translated_link_texts.items()
        }


def _render_chunk(
    doc: pymupdf.Document,
    page_data_list: list[PageData],
) -> int:
    """Phase 3: チャンク内の全ページを PDF へ描画する。失敗ページ数を返す。"""
    failed_pages = 0
    for pd in page_data_list:
        page = doc[pd.page_no]

        link_restore_info = []
        linked_block_indices: set[int] = set()
        for li_link, li_text, bi, _off in pd.link_info:
            translated = ""
            for mid, (m_link, m_text) in pd.marker_map.items():
                if m_link is li_link:
                    translated = pd.translated_link_texts.get(mid, "")
                    break
            block_rect = (
                pymupdf.Rect(pd.non_table_blocks[bi]["bbox"])
                if bi is not None and 0 <= bi < len(pd.non_table_blocks)
                else None
            )
            link_restore_info.append((li_link, li_text, translated, block_rect))
            if bi is not None and block_rect is not None and block_rect.width > 1:
                link_rect = pymupdf.Rect(li_link["from"])
                if link_rect.width >= block_rect.width * 0.5:
                    linked_block_indices.add(bi)

        translated_tables = [(t, rows) for t, rows in zip(pd.valid_tables, pd.translated_tables_rows)]

        replace_page_content(
            page, pd.non_table_blocks, pd.translated_texts, translated_tables,
            link_restore_info=link_restore_info,
            linked_block_indices=linked_block_indices,
        )

        n_tables = len(pd.valid_tables)
        n_blocks = len(pd.non_table_blocks)
        tbl_info = f", 表{n_tables}" if n_tables else ""
        if pd.block_fail == 0:
            print(f"  [p{pd.page_no}] 翻訳OK ({n_blocks}段落/{len(pd.groups)}グループ{tbl_info})")
        else:
            failed_pages += 1
            print(f"  [p{pd.page_no}] {pd.block_fail}件失敗 ({n_blocks}段落/{len(pd.groups)}グループ{tbl_info})")
    return failed_pages


def _save_doc(doc: pymupdf.Document, out_path) -> float:
    """doc を保存し、所要時間を返す。

    方針: リンク・外観を維持しつつ最速で書き出す。
      - garbage=1: 未参照オブジェクトのみ削除 (annot 参照は壊さない)
      - deflate=True: コンテンツストリーム (テキスト) を flate 圧縮
      - 画像/フォントの再圧縮や重複マージ (garbage=3) は行わない
      - clean=False / linear=False: 全ページ走査を要する処理はスキップ

    5394 ページ級の doc でも O(変更ページ) のコストで完了する。
    """
    t0 = time.monotonic()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(
        str(out_path),
        garbage=1,
        deflate=True,
        clean=False,
        linear=False,
    )
    return time.monotonic() - t0


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def run_translation(config: TranslationConfig) -> None:
    doc = pymupdf.open(str(config.input_pdf))
    total = len(doc)

    target_pages = (
        [p for p in config.pages if 0 <= p < total]
        if config.pages is not None else list(range(total))
    )
    n_target = len(target_pages)

    # チャンクサイズ (0 以下 → 全体を 1 チャンクで処理 = 従来動作)
    chunk_size = config.chunk_size if config.chunk_size and config.chunk_size > 0 else n_target
    chunk_size = max(1, chunk_size)

    print(f"PDF翻訳開始: {config.input_pdf.name} ({n_target}ページ, chunk_size={chunk_size})")
    start = time.monotonic()

    client = make_client(config)
    total_failed = 0

    try:
        for chunk_idx, chunk_start in enumerate(range(0, n_target, chunk_size)):
            chunk_pages = target_pages[chunk_start:chunk_start + chunk_size]
            first, last = chunk_pages[0], chunk_pages[-1]
            n_chunks = (n_target + chunk_size - 1) // chunk_size
            print(
                f"\n==== チャンク {chunk_idx + 1}/{n_chunks} "
                f"(p{first}-p{last}, {len(chunk_pages)}ページ) "
                f"[経過 {time.monotonic()-start:.0f}s] ===="
            )

            # Phase 1: 抽出
            print(f"  [{time.monotonic()-start:.0f}s] 抽出フェーズ...")
            page_data_list = _extract_chunk(doc, chunk_pages, config, start)

            # Phase 2: 翻訳
            print(
                f"  [{time.monotonic()-start:.0f}s] LLM翻訳フェーズ "
                f"(max_workers={config.max_workers})..."
            )
            _translate_chunk(client, page_data_list, config)

            # Phase 3: 描画
            print(f"  [{time.monotonic()-start:.0f}s] PDF描画フェーズ...")
            chunk_failed = _render_chunk(doc, page_data_list)
            total_failed += chunk_failed

            # save チェックポイント: 毎チャンク後に output.pdf を上書き (軽量保存)
            save_time = _save_doc(doc, config.output_pdf)
            print(
                f"  [{time.monotonic()-start:.0f}s] チャンク保存完了 "
                f"({save_time:.1f}s) → {config.output_pdf}"
            )

        # 全チャンク完了 — 各チャンクの末尾で `_save_doc` 済みなので
        # 追加の圧縮保存は行わない (画像/フォント再圧縮を避け常に高速)。
        size = config.output_pdf.stat().st_size if config.output_pdf.exists() else 0
        print(
            f"\n[{time.monotonic()-start:.0f}s] 保存済み → {config.output_pdf}  "
            f"({_format_size(size)})"
        )
    except KeyboardInterrupt:
        # 中断時: チャンク保存と同等の軽量保存で即座に抜ける。
        # チャンク保存済みデータは既に output にあるので in-progress 分だけ追加で書く。
        print("\n!! 中断検出。軽量保存を試みます...")
        try:
            save_time = _save_doc(doc, config.output_pdf)
            size = config.output_pdf.stat().st_size if config.output_pdf.exists() else 0
            print(
                f"   軽量保存完了 ({save_time:.1f}s) → {config.output_pdf}  "
                f"({_format_size(size)})"
            )
        except KeyboardInterrupt:
            print("   二度目の中断検出 — 最終保存を打ち切り")
        except Exception as e:
            print(f"   最終保存失敗 ({type(e).__name__}: {e}) — 直前のチャンク保存を維持")
        print("   再実行するとキャッシュ経由で翻訳済み分が即時復元されます。")
        raise
    finally:
        # キャッシュ統計表示
        cache = getattr(client, "cache", None)
        if cache is not None:
            s = cache.stats()
            total_calls = s["hits"] + s["misses"]
            hit_rate = (s["hits"] / total_calls * 100) if total_calls else 0.0
            print(
                f"キャッシュ: hit={s['hits']}, miss={s['misses']}, "
                f"write={s['writes']}, ヒット率={hit_rate:.0f}%, "
                f"累計エントリ={cache.entry_count()}"
            )
        client.close()
        doc.close()

    elapsed = time.monotonic() - start
    status = "完了!" if total_failed == 0 else f"完了 ({total_failed}ページに一部失敗あり)"
    print(f"\n{status} 出力: {config.output_pdf}  合計: {elapsed:.1f}秒")
