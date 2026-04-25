#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zhconvert_translator.py
繁化姬（zhconvert.org）翻譯包裝器 — 實作與 Translator 相同的介面。

本模組使用了繁化姬的服務（https://zhconvert.org）。

繁化姬以 zh-CN→zh-TW 字詞轉換為核心，字典堆疊包含：
  goo辞書、漢字ペディア、國家教育研究院、教育部重編國語辭典修訂本、萌典、汉典、粵語審音配詞字庫
與 OpenCC 相比，繁化姬幾乎不會誤傷固定詞語（如「的士」→「計程車」）。

服務條款摘要（使用前請閱讀 https://zhconvert.org 上的完整條款）：
  - 繁化姬不保證所有轉換正確，轉換結果僅供參考，正式文件請人工校閱
  - 免費使用 API 時，程式中必須說明使用了繁化姬的服務並附上主網頁網址
"""

import time

import requests


ZHCONVERT_URL = "https://api.zhconvert.org/convert"
SEP = "\n⊕⊕⊕\n"
BATCH_CHARS = 8000   # 繁化姬單次上限約 50k，保守設 8000


class ZhConvertTranslator:
    """與 Translator 相容的繁化姬包裝器。

    epub_handler 只呼叫以下介面：
      translate(text) -> str
      translate_batch(texts, fmt="text") -> list[str]
      _fallback_log  (list)
      _s2t_keys      (frozenset，由外部注入)
      clear_fallback_log()
      append_fallback_log(path)
      close()

    額外屬性：
      skip_nmt_repair = True  → epub_handler 據此跳過 NMT-specific repair pass
    """

    skip_nmt_repair: bool = True

    def __init__(self):
        self._fallback_log: list = []
        self._s2t_keys: frozenset = frozenset()
        self.total_chars: int = 0
        self.total_http: int = 0
        self._session = requests.Session()
        print("✓ 繁化姬模式（zhconvert.org / Taiwan）")

    # ── 單段翻譯 ──────────────────────────────────────────────────────────

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return text
        return self._zhconvert_single(text)

    # ── 批次翻譯 ──────────────────────────────────────────────────────────

    def translate_batch(self, texts: list[str], fmt: str = "text") -> list[str]:
        """
        fmt="text" 或 fmt="html" 均以相同方式處理：
        繁化姬只轉換漢字，HTML 標籤不受影響，無需特殊處理。
        """
        if not texts:
            return []

        results: list[str] = [""] * len(texts)

        # 按 BATCH_CHARS 拆批
        batches: list[list[tuple[int, str]]] = []
        current: list[tuple[int, str]] = []
        current_len = 0
        for idx, text in enumerate(texts):
            if current and current_len + len(text) + len(SEP) > BATCH_CHARS:
                batches.append(current)
                current = []
                current_len = 0
            current.append((idx, text))
            current_len += len(text) + len(SEP)
        if current:
            batches.append(current)

        for batch in batches:
            indices = [i for i, _ in batch]
            batch_texts = [t for _, t in batch]
            joined = SEP.join(batch_texts)
            self.total_chars += len(joined)

            converted_parts = None
            for attempt in range(3):
                try:
                    r = self._session.post(
                        ZHCONVERT_URL,
                        data={"text": joined, "converter": "Taiwan"},
                        timeout=30,
                    )
                    r.raise_for_status()
                    self.total_http += 1
                    converted = r.json()["data"]["text"]
                    parts = converted.split(SEP)
                    if len(parts) != len(batch_texts):
                        # 分隔符被轉換（極少發生）：逐條重送
                        parts = self._zhconvert_one_by_one(batch_texts)
                    converted_parts = parts
                    break
                except Exception as e:
                    if attempt == 2:
                        converted_parts = [f"[ERROR: {e}]"] * len(batch_texts)
                    else:
                        time.sleep(2 ** attempt)

            if converted_parts:
                for i, part in zip(indices, converted_parts):
                    results[i] = part

        return results

    # ── 共用方法（與 Translator 介面對齊）────────────────────────────────

    def clear_fallback_log(self):
        self._fallback_log = []

    def append_fallback_log(self, path: str):
        pass  # 繁化姬不產生 fallback log

    def close(self):
        self._session.close()

    def stats(self) -> str:
        return f"繁化姬 {self.total_http} 次 HTTP 請求，共 {self.total_chars:,} 字"

    # ── 內部工具 ──────────────────────────────────────────────────────────

    def _zhconvert_single(self, text: str) -> str:
        for attempt in range(3):
            try:
                r = self._session.post(
                    ZHCONVERT_URL,
                    data={"text": text, "converter": "Taiwan"},
                    timeout=15,
                )
                r.raise_for_status()
                self.total_http += 1
                self.total_chars += len(text)
                return r.json()["data"]["text"]
            except Exception as e:
                if attempt == 2:
                    return text  # 失敗回傳原文
                time.sleep(2 ** attempt)
        return text

    def _zhconvert_one_by_one(self, texts: list[str]) -> list[str]:
        out = []
        for t in texts:
            out.append(self._zhconvert_single(t))
            time.sleep(0.2)
        return out
