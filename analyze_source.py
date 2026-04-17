"""元PDFの構造を徹底調査する。

調べる項目:
  1. Catalog レベル: StructTree, Outline, PageLabels, Lang, OCProperties, Names, AcroForm
  2. Page 属性: MediaBox, CropBox, Rotate, UserUnit, Tabs, PieceInfo
  3. StructTree: role タグの分布、深さ、MCID 参照率
  4. Outline: 見出し階層、ページマッピング
  5. Named destinations: 個数・命名規則
  6. PageLabels: 章別ページ番号方式
  7. Fonts: 埋め込み状況、エンコーディング
  8. Images: xref 分布、再利用率
  9. Form XObjects: 再利用パターン
  10. Metadata XMP
"""
from __future__ import annotations
import re
import sys
from collections import Counter
from pathlib import Path

import pymupdf

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "S32K3XXRM.pdf")

doc = pymupdf.open(str(SRC))
print(f"=== {SRC} ===")
print(f"pages: {len(doc)}, xref: {doc.xref_length():,}")
print()

# ----- 1. Catalog keys -----
print("## 1. Catalog レベルのキー")
catalog = doc.pdf_catalog()
cat_obj = doc.xref_object(catalog, compressed=False)
for key in ("StructTreeRoot", "MarkInfo", "Lang", "PageLabels", "Outlines",
            "Names", "AcroForm", "OCProperties", "Metadata", "ViewerPreferences",
            "PageMode", "PageLayout", "OpenAction", "PieceInfo", "Threads"):
    m = re.search(rf"/{key}\s+([^/\n]+)", cat_obj)
    print(f"  {key}: {'あり' if m else '-'}")

# ----- 2. Metadata -----
print("\n## 2. Metadata")
md = doc.metadata or {}
for k, v in md.items():
    if v:
        s = str(v)[:80]
        print(f"  {k}: {s}")

# ----- 3. StructTree role distribution -----
print("\n## 3. StructTree role 分布 (top 20)")
role_counter = Counter()
k_counter = Counter()  # /K 配列長 (子要素数)
depths = []

# xref 全体を舐めて StructElem を拾う
N = doc.xref_length()
for xref in range(1, N):
    try:
        obj = doc.xref_object(xref, compressed=False)
    except Exception:
        continue
    if "/Type /StructElem" not in obj and "/Type/StructElem" not in obj:
        continue
    m = re.search(r"/S\s*/(\w+)", obj)
    role = m.group(1) if m else "?"
    role_counter[role] += 1
    # K のサイズ推定: /K [ ... ] の要素数。単純に /MCID の出現数カウント
    k_counter[role] += obj.count("/MCID")

for role, count in role_counter.most_common(20):
    mcids = k_counter[role]
    print(f"  {role}: {count:,} 要素, MCID 参照 {mcids:,}")

# ----- 4. Outline (bookmarks) -----
print("\n## 4. Outline (目次)")
toc = doc.get_toc(simple=False)
print(f"  総エントリ数: {len(toc)}")
level_counter = Counter(entry[0] for entry in toc)
print(f"  レベル分布: {dict(sorted(level_counter.items()))}")
if toc:
    print("  最初の10件:")
    for lvl, title, page, *_ in toc[:10]:
        print(f"    L{lvl} p{page}: {title[:60]}")

# ----- 5. Named destinations -----
print("\n## 5. Named destinations")
try:
    names = doc.resolve_names()  # pymupdf >= 1.23
    print(f"  個数: {len(names)}")
    if names:
        sample = list(names.keys())[:5]
        print(f"  例: {sample}")
except Exception as e:
    print(f"  取得不可: {e}")

# ----- 6. Page labels -----
print("\n## 6. Page labels (ページ番号方式)")
labels = []
try:
    for i in range(min(len(doc), 20)):
        labels.append(doc[i].get_label() if hasattr(doc[i], 'get_label') else None)
except Exception:
    pass
if any(labels):
    print(f"  先頭20ページのラベル: {labels}")
else:
    print("  -")

# ----- 7. Fonts -----
print("\n## 7. Fonts")
font_names = Counter()
font_embedded = Counter()
# サンプリング: 最初100ページから収集
for i in range(min(len(doc), 100)):
    for f in doc[i].get_fonts(full=True):
        # (xref, ext, type, basefont, name, encoding)
        xref, ext, ftype, basefont, fname, enc = f[:6]
        font_names[basefont] += 1
        font_embedded[ext or "none"] += 1
print(f"  (先頭100ページサンプリング)")
print(f"  ユニーク basefont: {len(font_names)}")
for n, c in font_names.most_common(10):
    print(f"    {n}: {c}ページで出現")
print(f"  ext 分布: {dict(font_embedded)}")

# ----- 8. Images -----
print("\n## 8. Images")
unique_imgs = set()
img_refs = 0
for i, page in enumerate(doc):
    for img in page.get_images(full=True):
        img_refs += 1
        unique_imgs.add(img[0])
print(f"  ユニーク画像 xref: {len(unique_imgs)}")
print(f"  ページからの参照合計: {img_refs}")
print(f"  再利用率: {(1 - len(unique_imgs)/max(img_refs,1))*100:.1f}%")

# ----- 9. XObjects (Form / Image) -----
print("\n## 9. XObject 分布")
xobj_types = Counter()
for xref in range(1, N):
    try:
        obj = doc.xref_object(xref, compressed=False)
    except Exception:
        continue
    if "/Type /XObject" not in obj and "/Type/XObject" not in obj:
        continue
    m = re.search(r"/Subtype\s*/(\w+)", obj)
    xobj_types[m.group(1) if m else "?"] += 1
print(f"  {dict(xobj_types)}")

# ----- 10. Actions / Links -----
print("\n## 10. Link/Action 分布")
link_kinds = Counter()
for page in doc:
    for link in page.get_links():
        link_kinds[link.get("kind", "?")] += 1
print(f"  {dict(link_kinds)}")
# pymupdf LINK kind: 1=GoTo, 2=GoToR, 3=Launch, 4=URI, 5=Named, ...
kind_names = {1: "GoTo(内部)", 2: "GoToR(外部PDF)", 3: "Launch", 4: "URI", 5: "Named"}
for k, v in link_kinds.items():
    print(f"    {kind_names.get(k, k)}: {v}")

# ----- 11. Page geometry -----
print("\n## 11. Page geometry")
sizes = Counter()
rotates = Counter()
for page in doc:
    w, h = round(page.rect.width, 1), round(page.rect.height, 1)
    sizes[(w, h)] += 1
    rotates[page.rotation] += 1
print(f"  page size 分布: {dict(sizes)}")
print(f"  rotate 分布: {dict(rotates)}")

doc.close()
print("\n=== 完了 ===")
