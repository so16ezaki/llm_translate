from __future__ import annotations
import re
import time
import requests
from config import TranslationConfig


# 翻訳成否の追加検証 (LLM が原文をそのまま返すケース検出用)
_JP_CHAR_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def _looks_translated(source: str, translated: str, target_lang: str) -> bool:
    """翻訳結果が本当に翻訳されているかを簡易判定する。

    target_lang=ja の場合のポリシー:
      - 短い固有名詞 / ブランド名 / 型番は原文そのままで OK (例: "NXP Semiconductors")
      - 原文と完全一致 = LLM が「翻訳不要」と判断した = 正当 (章タイトル/モジュール名等)
      - 3 語以上の英文センテンスで内容に変化があるのに日本語が含まれない = 本当の未翻訳
    """
    if target_lang == "ja":
        src = source.strip()
        trans = translated.strip()
        # 短いソースは固有名詞扱いで常に pass (例: "NXP Semiconductors", "CAUTION")
        if len(src) < 30:
            return True
        # 原文そのまま返却 = LLM が翻訳不要と判断 (章タイトル・モジュール名等で正当)
        if src == trans:
            return True
        # 3 語以上の英文センテンスで日本語ゼロ かつ 何か変化はある
        # → LLM が部分改変しただけで訳していない可能性 = 失敗
        has_latin_sentence = bool(re.search(r"[A-Za-z]+\s+[A-Za-z]+\s+[A-Za-z]+", src))
        if has_latin_sentence and not _JP_CHAR_RE.search(trans):
            return False
    return True

_THINK_RE = re.compile(r"<think(?:ing)?[^>]*>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)

_LANG_NAMES = {
    "en": "English", "ja": "Japanese", "zh-hans": "Simplified Chinese",
    "zh-hant": "Traditional Chinese", "ko": "Korean", "fr": "French",
    "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "ru": "Russian", "ar": "Arabic",
}

_KEEP_RULES = """\
Keep the following unchanged:
- Acronyms and technical terms (PWM, CAN, SPI, I2C, MCU, GPIO, DMA, UART, ADC, CRC, etc.)
- Register names, pin names, signal names, hex literals (0x1F, etc.)
- Numbers, units, version strings
- English words commonly kept as-is in the target language
"""


# TOC 向けルールは marker 付き (=本文ブロック) 翻訳のときだけ適用する。
# markdown 表翻訳など長文のプロンプトには含めないことで、LLM が混乱して
# 原文をそのまま返す退行を防ぐ。
_TOC_STYLE_RULE = """\
Translation style:
- "Chapter N" -> "第N章" (include "第", no space between N and 章)
- Do NOT duplicate numbers: 'Chapter 18' -> '第18章', not '第18章 18'.
"""


_MARKER_RULE = """\
The text may contain markers like ⟪0⟫...⟪/0⟫.
Keep these markers in the output, wrapping the corresponding translated text.
Do NOT insert any line breaks between ⟪N⟫ and ⟪/N⟫ — the translated text for a
marker must stay on a single line.
"""


def _build_prompt(text: str, src: str, tgt: str, hint: str | None = None) -> str:
    """翻訳用プロンプトを組み立てる。

    hint:
      - None / "text": 通常の本文ブロック翻訳 (marker 付きの場合 TOC ルールも適用)
      - "table":       markdown 表翻訳。表構造を壊さないための注意書きを強化
    """
    src_name = _LANG_NAMES.get(src, src)
    tgt_name = _LANG_NAMES.get(tgt, tgt)

    if hint == "table":
        # 表セル内の複数行は JSON 配列として渡される。構造保持を最重要に指示。
        return (
            f"Translate the following {src_name} content to {tgt_name}.\n"
            f"Output ONLY the translated content. No explanations, no original text.\n"
            f"Rules:\n"
            f"- If the input is a markdown table: preserve the structure (same number of rows,\n"
            f"  same number of '|' per row, keep the '| --- | --- |' separator unchanged).\n"
            f"- If the input is a numbered list: keep the numbering.\n"
            f"- Translate EVERY text content. Do not leave anything in {src_name}.\n"
            f"- Keep acronyms (PWM, SPI, CAN, ADC, MCU, GPIO, DMA, UART, CRC, etc.),\n"
            f"  register/pin/signal names, hex literals and numbers unchanged.\n"
            f"- CRITICAL: If a cell or item is a JSON array like [\"line1\", \"line2\", \"line3\"],\n"
            f"  the array represents a multi-line cell. Translate each STRING element. Keep:\n"
            f"    * the array brackets [ and ]\n"
            f"    * the SAME NUMBER of elements (never merge, split, add, or remove elements)\n"
            f"    * the element order\n"
            f"    * double-quoted strings (escape \" as \\\" and \\ as \\\\)\n"
            f"    * leading whitespace inside strings (indentation is meaningful — preserve it)\n"
            f"  Do NOT collapse the array into a single string. Do NOT use any other separator.\n"
            f"\n"
            f"Example:\n"
            f"  Input cell:  [\"Mode A\", \"00b - select bank 0\", \"01b - select bank 1\"]\n"
            f"  Output cell: [\"モード A\", \"00b - バンク 0 選択\", \"01b - バンク 1 選択\"]\n"
            f"\n"
            f"{text}"
        )

    # 本文翻訳 (従来動作)
    has_markers = "⟪" in text
    marker_rule = _MARKER_RULE if has_markers else ""
    toc_rule = _TOC_STYLE_RULE if has_markers else ""
    return (
        f"Translate the following {src_name} text to {tgt_name}.\n"
        f"Output ONLY the translated text. No explanations, no original text.\n"
        f"Keep line breaks in the same structure.\n"
        f"{marker_rule}"
        f"{toc_rule}"
        f"{_KEEP_RULES}\n{text}"
    )


class OllamaClient:
    def __init__(self, config: TranslationConfig) -> None:
        self._config = config
        self._url = f"{config.ollama_url}/api/chat"
        # Connection pool を再利用して TCP ハンドシェイクを回避。
        # 並列度 max_workers に合わせて pool_maxsize を確保する。
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=config.max_workers,
            pool_maxsize=config.max_workers * 2,
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def translate_markdown(self, markdown: str, hint: str | None = None) -> tuple[str, bool]:
        if not markdown.strip():
            return markdown, True
        prompt = _build_prompt(markdown, self._config.source_lang, self._config.target_lang, hint=hint)
        payload = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "keep_alive": "10m",  # モデルをメモリに保持して再ロード回避
            "options": {"temperature": self._config.temperature, "num_predict": 16384},
        }
        last_result = markdown
        for attempt in range(3):
            try:
                resp = self._session.post(
                    self._url, json=payload,
                    timeout=self._config.timeout_sec,
                )
                resp.raise_for_status()
                content: str = resp.json()["message"]["content"]
                result = _THINK_RE.sub("", content).strip()
                if not result:
                    continue
                last_result = result
                # LLM が原文をそのまま返すケースを検出。検出したらリトライ。
                if not _looks_translated(markdown, result, self._config.target_lang):
                    if attempt < 2:
                        continue
                    # 最終試行でも未翻訳 → 失敗として返却 (ok=False でキャッシュされない)
                    return result, False
                return result, True
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                print(f"    [翻訳エラー] {e}")
                return f"[翻訳エラー] {e}\n---原文---\n{markdown}", False
        return last_result, False


class DifyClient:
    def __init__(self, config: TranslationConfig) -> None:
        self._config = config
        base = config.dify_url.rstrip("/")
        self._url = f"{base}/chat-messages"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {config.dify_api_key}",
            "Content-Type": "application/json",
        })

    def close(self) -> None:
        self._session.close()

    def translate_markdown(self, markdown: str, hint: str | None = None) -> tuple[str, bool]:
        if not markdown.strip():
            return markdown, True
        prompt = _build_prompt(markdown, self._config.source_lang, self._config.target_lang, hint=hint)
        payload = {
            "inputs": {}, "query": prompt,
            "response_mode": "blocking", "user": "pdf-translator",
        }
        try:
            resp = self._session.post(self._url, json=payload, timeout=self._config.timeout_sec)
            resp.raise_for_status()
            content: str = resp.json()["answer"]
            result = _THINK_RE.sub("", content).strip()
            return (result, True) if result else (markdown, False)
        except Exception as e:
            print(f"    [翻訳エラー] {e}")
            return f"[翻訳エラー] {e}\n---原文---\n{markdown}", False


class CachedClient:
    """SQLite キャッシュで翻訳結果を永続化するラッパー。

    途中で強制終了しても、保存済みの翻訳結果は次回実行時に即時再利用される。
    temperature=0 を前提とした決定的な翻訳結果のキャッシュ。
    """

    def __init__(self, inner, config: TranslationConfig, cache) -> None:
        self._inner = inner
        self._config = config
        self._cache = cache

    def close(self) -> None:
        self._inner.close()
        try:
            self._cache.close()
        except Exception:
            pass

    @property
    def cache(self):
        return self._cache

    def translate_markdown(self, markdown: str, hint: str | None = None) -> tuple[str, bool]:
        if not markdown.strip():
            return markdown, True
        # キャッシュキーには hint も含める (表用プロンプトと本文用プロンプトで
        # 翻訳結果が異なる可能性があるため)
        cache_key_prefix = f"[{hint}]" if hint else ""
        cache_source = cache_key_prefix + markdown
        cached = self._cache.get(
            cache_source,
            self._config.model,
            self._config.source_lang,
            self._config.target_lang,
        )
        if cached is not None:
            return cached, True
        result, ok = self._inner.translate_markdown(markdown, hint=hint)
        # 翻訳失敗 (ok=False) の結果はキャッシュしない。リトライできるように。
        if ok and not result.startswith("[翻訳エラー]"):
            try:
                self._cache.put(
                    cache_source, result,
                    self._config.model,
                    self._config.source_lang,
                    self._config.target_lang,
                )
            except Exception as e:
                print(f"    [キャッシュ書込失敗] {e}")
        return result, ok


def make_client(config: TranslationConfig) -> OllamaClient | DifyClient | CachedClient:
    inner = DifyClient(config) if config.backend == "dify" else OllamaClient(config)

    # use_cache=False なら CachedClient でラップしない (毎回 LLM に問い合わせ)
    if not getattr(config, "use_cache", True):
        print("  [キャッシュ無効化] use_cache=False で指定。毎回 LLM に問い合わせます。")
        return inner

    from pathlib import Path as _Path
    from checkpoint import TranslationCache, default_cache_path
    try:
        cache_path = _Path(config.cache_path) if config.cache_path else default_cache_path(config.input_pdf)
        if getattr(config, "clear_cache", False) and cache_path.exists():
            cache_path.unlink()
            print(f"  [キャッシュ削除] {cache_path} を削除しました。")
        cache = TranslationCache(cache_path)
        return CachedClient(inner, config, cache)
    except Exception as e:
        print(f"    [キャッシュ初期化失敗 → 無効化] {e}")
        return inner
