"""
翻訳結果の SQLite キャッシュ

source_text をハッシュ化したキーで翻訳結果を永続化する。
- 途中で強制終了しても保存済みの翻訳は次回即時ロードされる
- 同じ文言が別 PDF に現れた場合も再利用される (PDF 間共有)
- モデル・言語ペアの切替時はキーが変わるので自動的に別エントリになる
"""
from __future__ import annotations
import hashlib
import sqlite3
import threading
import time
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS translation_cache (
    key_hash        TEXT PRIMARY KEY,
    source_text     TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    model           TEXT NOT NULL,
    source_lang     TEXT NOT NULL,
    target_lang     TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model ON translation_cache(model);
"""


def _make_key(
    source_text: str, model: str, source_lang: str, target_lang: str,
) -> str:
    """キャッシュキーを生成する。

    source_text は翻訳入力そのもの (marker 付きの場合はそれも含む)。
    model/lang が違えば別キーになる。
    """
    payload = f"{model}\x00{source_lang}\x00{target_lang}\x00{source_text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TranslationCache:
    """SQLite ベースの翻訳キャッシュ。スレッドセーフ (per-thread connection)。"""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._local = threading.local()
        # スキーマは最初の接続時に作成
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def get(
        self,
        source_text: str,
        model: str,
        source_lang: str,
        target_lang: str,
    ) -> str | None:
        key = _make_key(source_text, model, source_lang, target_lang)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT translated_text FROM translation_cache WHERE key_hash = ?",
            (key,),
        ).fetchone()
        with self._lock:
            if row:
                self._hits += 1
            else:
                self._misses += 1
        return row[0] if row else None

    def put(
        self,
        source_text: str,
        translated_text: str,
        model: str,
        source_lang: str,
        target_lang: str,
    ) -> None:
        key = _make_key(source_text, model, source_lang, target_lang)
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO translation_cache "
            "(key_hash, source_text, translated_text, model, source_lang, target_lang, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, source_text, translated_text, model, source_lang, target_lang, time.time()),
        )
        conn.commit()
        with self._lock:
            self._writes += 1

    def stats(self) -> dict:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "writes": self._writes}

    def entry_count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM translation_cache").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        if hasattr(self._local, "conn"):
            try:
                self._local.conn.close()
            except Exception:
                pass


def default_cache_path(input_pdf: Path) -> Path:
    """入力 PDF に対応するデフォルトのキャッシュ DB パス。"""
    return input_pdf.parent / f".{input_pdf.stem}_cache.sqlite3"
