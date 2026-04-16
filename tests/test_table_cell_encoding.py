"""表セルの JSON エンコード/デコード単体テスト。

S32K3XXRM.pdf の register description 表で改行情報が潰れる問題の対応。
複数行セルは JSON 配列で LLM に渡し、単行セルは素のままにする。
"""
from __future__ import annotations
import sys
from pathlib import Path

# プロジェクトルートを path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import _encode_cell_for_prompt, _decode_translated_cell


# ================================================================== #
#  エンコード
# ================================================================== #

def test_encode_single_line():
    assert _encode_cell_for_prompt("Hello") == "Hello"


def test_encode_empty():
    assert _encode_cell_for_prompt("") == ""
    assert _encode_cell_for_prompt(None) == ""


def test_encode_multi_line_basic():
    s = "Title\nDesc\n0b - a\n1b - b"
    encoded = _encode_cell_for_prompt(s)
    assert encoded == '["Title", "Desc", "0b - a", "1b - b"]'


def test_encode_preserves_indentation():
    """列挙値の先頭スペース (インデント) を保持する。"""
    s = "Description:\n  0b - Disabled\n  1b - Enabled"
    encoded = _encode_cell_for_prompt(s)
    # json.dumps はインデント空白をそのまま保持する
    assert '"  0b - Disabled"' in encoded
    assert '"  1b - Enabled"' in encoded


def test_encode_escapes_quotes():
    s = 'He said "hello"\nand left'
    encoded = _encode_cell_for_prompt(s)
    # JSON で " がエスケープされていること
    assert '\\"hello\\"' in encoded


# ================================================================== #
#  デコード
# ================================================================== #

def test_decode_json_array_basic():
    out = _decode_translated_cell(
        '["タイトル", "説明", "0b - A", "1b - B"]', expected_lines=4
    )
    assert out == "タイトル\n説明\n0b - A\n1b - B"


def test_decode_plain_string():
    """配列でない単純文字列はそのまま返す。"""
    assert _decode_translated_cell("こんにちは") == "こんにちは"


def test_decode_legacy_cell_nl_marker():
    """旧 ⟦NL⟧ マーカー形式も復元できる (キャッシュ互換)。"""
    assert _decode_translated_cell("Hello⟦NL⟧World") == "Hello\nWorld"


def test_decode_legacy_br_marker():
    """旧 <br> マーカー形式も復元できる (後方互換)。"""
    assert _decode_translated_cell("Hello<br>World") == "Hello\nWorld"


def test_decode_invalid_json_falls_back():
    """閉じ括弧欠落 → 生文字列として返す (panic しない)。"""
    out = _decode_translated_cell('["a", "b"')
    assert "a" in out


def test_decode_length_mismatch_tolerated(capsys):
    """要素数不一致 → warning 出力してそのまま結合する。"""
    out = _decode_translated_cell('["x", "y"]', expected_lines=3)
    assert out == "x\ny"
    captured = capsys.readouterr()
    assert "mismatch" in captured.out


def test_decode_empty_string():
    assert _decode_translated_cell("") == ""


def test_decode_preserves_indentation():
    """JSON 配列内の先頭スペース (インデント) が復元後も保持される。"""
    out = _decode_translated_cell(
        '["Description:", "  0b - Disabled", "  1b - Enabled"]',
        expected_lines=3,
    )
    assert out == "Description:\n  0b - Disabled\n  1b - Enabled"


# ================================================================== #
#  エンコード→デコード ラウンドトリップ
# ================================================================== #

def test_roundtrip_multi_line():
    original = "VLAN Tag Enable\nEnables or disables the VLAN tag.\n  0b - Disabled\n  1b - Enabled"
    encoded = _encode_cell_for_prompt(original)
    decoded = _decode_translated_cell(encoded, expected_lines=4)
    assert decoded == original


def test_roundtrip_single_line():
    original = "16/VEN"
    encoded = _encode_cell_for_prompt(original)
    decoded = _decode_translated_cell(encoded)
    assert decoded == original


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
