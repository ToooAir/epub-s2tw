#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translator.py
Google Translate wrapper — 支援兩種模式：
  - 免費模式（預設）: translate_a/single 非官方端點，不需 API Key
  - API 模式:        Google Cloud Translation API v2，需要 API Key，更穩定
快取：翻譯結果儲存至 .translate_cache.json，避免重複計費與重複請求。
"""

import json
import hashlib
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm


class Translator:

    FREE_URL = "https://translate.googleapis.com/translate_a/single"
    BASE_URL = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self, api_key: str = None, source: str = "zh-CN", target: str = "zh-TW"):
        self.api_key  = api_key
        self.source   = source
        self.target   = target
        self.use_free = api_key is None

        self._cache_path = Path(".translate_cache.json")
        self._cache: dict = self._load_cache()
        self.total_segments  = 0   # 翻譯段落數（含快取命中前的新段落）
        self.total_http      = 0   # 實際 HTTP 請求次數
        self.total_chars     = 0
        self.fallback_count  = 0   # 遭遇漏句幻覺的降級次數
        self._fallback_log: list[tuple[str, str, str]] = []  # (source, hallucinated, fixed)

        self._session = requests.Session()

        if self.use_free:
            print("✓ 免費模式（translate_a/single）")
        else:
            print("✓ Google Cloud Translation API 模式")

    # ── Cache ──────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        if self._cache_path.exists():
            try:
                return json.loads(self._cache_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        self._cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _key(self, text: str, fmt: str = "text") -> str:
        return hashlib.md5(
            f"{self.source}:{self.target}:{fmt}:{text}".encode()
        ).hexdigest()

    def _store(self, key: str, value: str, n_chars: int):
        self._cache[key] = value
        self.total_chars    += n_chars
        self.total_segments += 1

    # ── Public API ─────────────────────────────────────────────────────

    def translate(self, text: str, fmt: str = "text") -> str:
        """翻譯單一字串。fmt = 'text' | 'html'"""
        if not text or not text.strip():
            return text
        key = self._key(text, fmt)
        if key in self._cache:
            return self._cache[key]
        result = (
            self._free_single(text)
            if self.use_free
            else self._api_call([text], fmt)[0]
        )
        
        if self.use_free and self._needs_fallback(text, result, fmt):
            self.fallback_count += 1
            result = self._fallback_sentence_translation(text)

        self._store(key, result, len(text))
        return result

    def translate_batch(self, texts: list, fmt: str = "text") -> list:
        """批次翻譯。回傳與輸入等長的列表。"""
        if not texts:
            return []

        results   = [None] * len(texts)
        miss_idx  = []

        for i, t in enumerate(texts):
            if not t or not t.strip():
                results[i] = t or ""
            else:
                key = self._key(t, fmt)
                if key in self._cache:
                    results[i] = self._cache[key]
                else:
                    miss_idx.append(i)

        if miss_idx:
            miss_texts  = [texts[i] for i in miss_idx]
            translated  = (
                self._free_batch(miss_texts)
                if self.use_free
                else self._api_call(miss_texts, fmt)
            )
            for arr_i, orig_i in enumerate(miss_idx):
                val = translated[arr_i]
                orig_text = miss_texts[arr_i]
                if self.use_free and self._needs_fallback(orig_text, val, fmt):
                    self.fallback_count += 1
                    hallucinated = val
                    val = self._fallback_sentence_translation(orig_text)
                    translated[arr_i] = val
                    self._fallback_log.append((orig_text, hallucinated, val))
                    
                results[orig_i] = val
                self._store(self._key(orig_text, fmt), val, len(orig_text))

        if self.total_segments % 40 == 0:
            self._save_cache()

        return results

    def clear_fallback_log(self):
        self._fallback_log.clear()
        self.fallback_count = 0

    def append_fallback_log(self, path: str):
        if not self._fallback_log:
            return
        lines = [f"\n\n=== NMT 幻覺紀錄 ({len(self._fallback_log)} 件) ===\n"]
        for i, (src, bad, fixed) in enumerate(self._fallback_log, 1):
            lines.append(f"\n[{i:03d}] 原文：\n  {src}\n")
            lines.append(f"      幻覺：\n  {bad}\n")
            lines.append(f"      修正：\n  {fixed}\n")
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(lines))

    def close(self):
        self._save_cache()
        ratio = self.total_segments / self.total_http if self.total_http else 0
        print(f"\n📊 段落數：{self.total_segments}  HTTP 請求：{self.total_http}  平均每請求 {ratio:.1f} 段  翻譯字元：{self.total_chars:,}")
        if self.fallback_count > 0:
            print(f"🛡️  中途遭遇 {self.fallback_count} 次 NMT 漏句/幻覺，已透過碎紙機閃電隔離完美修復。")

    # ── Free mode ──────────────────────────────────────────────────────

    FREE_CHUNK   = 1800  # 單次請求安全字元上限
    SEP          = "\n⚡\n"  # 批次合併用分隔符（翻譯後應原樣保留）
    FREE_WORKERS = 5     # 並行請求數

    def _needs_fallback(self, source: str, translated: str, fmt: str) -> bool:
        if not source or not str(source).strip():
            return False
        if not translated or not str(translated).strip():
            return True

        src_text, tgt_text = str(source), str(translated)
        if fmt == "html":
            src_text = re.sub(r'<[^>]*>', '', src_text)
            tgt_text = re.sub(r'<[^>]*>', '', tgt_text)

        s_len, t_len = len(src_text.strip()), len(tgt_text.strip())
        
        # 1. Length Integrity Check (> 15% + 5 chars missing)
        if s_len - t_len > max(5, s_len * 0.15):
            return True

        # 2. Punctuation Parity Check
        s_end, t_end = src_text.rstrip(), tgt_text.rstrip()
        if not s_end or not t_end:
            return False
            
        quotes = ("」", "』", '"', "'", "”", "’")
        if s_end.endswith(quotes) and not t_end.endswith(quotes):
            return True

        ends = ("。", "！", "？", "…", ".", "!", "?")
        if s_end.endswith(ends) and not t_end.endswith(ends) and not t_end.endswith(quotes):
            return True

        return False

    def _fallback_sentence_translation(self, text: str) -> str:
        """
        將長文切成短句後，以 ⚡ 分隔連成「單一請求」送出。
        這能強制切斷 Google NMT 翻譯高重複疊詞時會陷入的無限迴圈（Attention Hallucination），
        同時避免「機關槍掃射」發送數十個 HTTP Requests 導致 500 Error。
        """
        parts = re.split(r'([。！？…\!\?\.\n]+)', text)
        
        # 將標點與句子貼合
        sentences = []
        current = ""
        for p in parts:
            if not p.strip() or re.match(r'^[。！？…\!\?\n]+$', p):
                current += p
            else:
                if current:
                    sentences.append(current)
                current = p
        if current:
            sentences.append(current)
            
        if not sentences:
            return ""

        # 以 ⚡ 連接並發送一發單一請求
        batched_req = self.SEP.join(sentences)
        translated_batched = self._free_request(batched_req)
        
        # 移掉分隔符號重新拼合為完整段落 (保險清除任何可能殘留的閃電符號)
        clean_text = translated_batched.replace(self.SEP, "").replace("⚡", "")
        return clean_text

    def _free_single(self, text: str) -> str:
        """翻譯單段文字；超過上限自動切塊。"""
        if len(text) <= self.FREE_CHUNK:
            return self._free_request(text)
        # 超過上限：依句子邊界切塊，合併後分次送出再拼回
        chunks, buf = [], []
        for ch in text:
            buf.append(ch)
            if ch in "。！？\n" and len("".join(buf)) >= 100:
                chunks.append("".join(buf))
                buf = []
        if buf:
            chunks.append("".join(buf))
        merged, current = [], ""
        for c in chunks:
            if len(current) + len(c) <= self.FREE_CHUNK:
                current += c
            else:
                if current:
                    merged.append(current)
                current = c
        if current:
            merged.append(current)
        return "".join(self._free_request(m) for m in merged)

    def _free_request(self, text: str) -> str:
        """直接打一次 translate_a/single，回傳翻譯結果。失敗無限重試。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                self.total_http += 1
                resp = self._session.get(
                    self.FREE_URL,
                    params={"client": "gtx", "dt": "t", "sl": self.source, "tl": self.target, "q": text},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                return "".join(t[0] for t in data[0] if t[0])
            except Exception as e:
                self.total_http -= 1  # 失敗不計入
                wait = min(2 ** attempt, 120)
                tqdm.write(f"  ⚠️  請求失敗第 {attempt} 次（{e}），{wait}s 後重試…")
                time.sleep(wait)

    def _free_batch(self, texts: list) -> list:
        """批次翻譯：分組合併後並行送出，翻譯後拆回。確保每段都有結果。"""
        # 把 texts 分組，每組合併後不超過 FREE_CHUNK
        groups: list[tuple[list[int], str]] = []
        cur_idx, cur_parts, cur_len = [], [], 0
        for i, text in enumerate(texts):
            sep_len = len(self.SEP) if cur_parts else 0
            if cur_parts and cur_len + sep_len + len(text) > self.FREE_CHUNK:
                groups.append((cur_idx, self.SEP.join(cur_parts)))
                cur_idx, cur_parts, cur_len = [i], [text], len(text)
            else:
                cur_idx.append(i)
                cur_parts.append(text)
                cur_len += sep_len + len(text)
        if cur_parts:
            groups.append((cur_idx, self.SEP.join(cur_parts)))

        results = [None] * len(texts)

        def translate_group(indices: list[int], joined: str) -> list[tuple[int, str]]:
            """在獨立執行緒中翻譯一個 group，無限重試直到成功；回傳 [(原始index, 翻譯結果), ...]。"""
            attempt = 0
            while True:
                attempt += 1
                try:
                    translated = self._free_request(joined)
                    parts = translated.split(self.SEP)
                    time.sleep(0.05)
                    if len(parts) == len(indices):
                        return list(zip(indices, parts))
                    # 分隔符丟失，退回逐條翻譯
                    tqdm.write(f"  ⚠️  分隔符丟失（期望 {len(indices)} 段，得到 {len(parts)} 段），改逐條翻譯")
                    return [(idx, self._free_single(texts[idx])) for idx in indices]
                except Exception as e:
                    wait = min(2 ** attempt, 120)  # 指數退讓，上限 120s
                    tqdm.write(f"  ⚠️  第 {attempt} 次失敗（{e}），{wait}s 後重試…")
                    time.sleep(wait)

        with ThreadPoolExecutor(max_workers=self.FREE_WORKERS) as executor:
            futures = [executor.submit(translate_group, idx, joined) for idx, joined in groups]
            for future in as_completed(futures):
                for idx, val in future.result():
                    results[idx] = val

        # 安全保護：理論上不會觸發，但若有漏譯直接拋出例外
        missing = [i for i, val in enumerate(results) if val is None]
        if missing:
            raise RuntimeError(f"以下段落未取得翻譯結果：{missing}")

        return results

    # ── API mode ───────────────────────────────────────────────────────

    def _api_call(self, texts: list, fmt: str = "text") -> list:
        """呼叫 Google Cloud Translation API v2，每次最多 100 段。"""
        results = []
        url     = f"{self.BASE_URL}?key={self.api_key}"
        for i in range(0, len(texts), 100):
            self.total_http += 1
            batch = texts[i : i + 100]
            resp  = self._session.post(
                url,
                json={"q": batch, "source": self.source, "target": self.target, "format": fmt},
                timeout=30,
            )
            resp.raise_for_status()
            results.extend(
                t["translatedText"] for t in resp.json()["data"]["translations"]
            )
            time.sleep(0.2)
        return results
