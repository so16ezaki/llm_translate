"""StructTreeRoot (タグ付きPDF構造ツリー) を削除して圧縮する。

翻訳後PDFでは元の英語テキストの構造ツリーが残留しており、実質二重データ。
アクセシビリティメタデータなのでレイアウト・リンクには影響しない。
"""
import sys, time
from pathlib import Path
import pymupdf

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
print(f"入力: {src} ({src.stat().st_size/1024/1024:.1f} MB)")

t0 = time.monotonic()
doc = pymupdf.open(str(src))
print(f"  ページ数: {len(doc)}")
print(f"  xref 数(削除前): {doc.xref_length():,}")

# Catalog から構造ツリー関連キーを削除
catalog = doc.pdf_catalog()
for key in ("StructTreeRoot", "MarkInfo", "Lang"):
    try:
        doc.xref_set_key(catalog, key, "null")
    except Exception as e:
        print(f"  [warn] remove {key}: {e}")
print(f"  StructTreeRoot/MarkInfo 削除完了")

# Page から StructParents 参照も削除 (任意、残しても garbage で削除される)
# Subset fonts
t1 = time.monotonic()
try:
    doc.subset_fonts(verbose=False)
    print(f"  subset_fonts: {time.monotonic()-t1:.1f}s")
except Exception as e:
    print(f"  [warn] subset_fonts: {e}")

t2 = time.monotonic()
doc.save(
    str(dst),
    garbage=2,
    deflate=True,
    deflate_images=True,
    deflate_fonts=True,
    clean=False,
    linear=False,
)
print(f"  save: {time.monotonic()-t2:.1f}s")
doc.close()

print(f"出力: {dst} ({dst.stat().st_size/1024/1024:.1f} MB)")
print(f"  比率: {dst.stat().st_size / src.stat().st_size * 100:.1f}%")
print(f"合計: {time.monotonic()-t0:.1f}s")
