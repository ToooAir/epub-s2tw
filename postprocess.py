#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
postprocess.py
三層後處理，修正 Google 翻譯的繁體錯誤：
  Layer 1 — corrections.json：實測已知的系統性錯誤
  Layer 2 — STCharacters × MOE字典：以教育部詞頭為基礎生成字形修正規則
  Layer 3 — TWVariants：台灣異體字標準化
"""

import json
import lzma
from pathlib import Path


class PostProcessor:

    def __init__(
        self,
        corrections_path: str = "corrections.json",
        stcharacters_path: str = "STCharacters.txt",
        twvariants_path: str = "TWVariants.txt",
        moedict_path: str = "dict-revised.json.xz",
    ):
        moe_headwords = self._load_moe_headwords(moedict_path)
        self._s2t, self._corrections = self._build_corrections(
            corrections_path, stcharacters_path, twvariants_path, moe_headwords
        )
        # 按首字分組，長詞優先：加速 single-pass 掃描
        self._by_first: dict[str, list] = {}
        for wrong, right in sorted(self._corrections.items(), key=lambda x: -len(x[0])):
            if wrong != right:  # 跳過 identity 規則（TWVariants 清洗後產生）
                self._by_first.setdefault(wrong[0], []).append((wrong, right))
        # Bigram 邊界字典：從 MOE 詞頭提取相鄰字對
        # 用於 lookbehind / lookahead 越界檢查，防止短規則命中更長詞的片段
        self._bigrams: frozenset[str] = self._build_bigrams(moe_headwords)
        # 後處理觸發記錄：(wrong, right) → [上下文片段, ...]（最多 3 筆）
        self._applied: dict[tuple[str, str], list[str]] = {}

    # ── 建立 ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_moe_headwords(moedict_path: str) -> frozenset[str]:
        """載入教育部國語辭典詞頭集合。檔案不存在時回傳空集合（功能降級）。"""
        p = Path(moedict_path)
        if not p.exists():
            return frozenset()
        with lzma.open(p, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return frozenset(e["title"] for e in data if "title" in e)

    def _build_corrections(
        self, corrections_path: str, stcharacters_path: str,
        twvariants_path: str, moe_headwords: frozenset
    ) -> tuple[dict[str, set], dict]:
        # STCharacters：建立 primary ↔ alt 對應，同時建立 s2t_map（簡→繁合法集合）
        primary_to_alts: dict[str, list] = {}
        alt_to_primary: dict[str, str] = {}
        s2t: dict[str, set] = {}
        with open(stcharacters_path, encoding="utf-8") as f:
            for line in f:
                if "\t" not in line:
                    continue
                s, t = line.rstrip("\n").split("\t", 1)
                opts = t.strip().split()
                if not opts:
                    continue
                s2t[s.strip()] = set(opts)
                if len(opts) < 2:
                    continue
                primary = opts[0]
                for alt in opts[1:]:
                    if alt != primary:
                        alt_to_primary[alt] = primary
                        primary_to_alts.setdefault(primary, [])
                        if alt not in primary_to_alts[primary]:
                            primary_to_alts[primary].append(alt)

        # MOE 詞頭生成層：以教育部字典為基礎，生成字形修正規則
        # 對每個 MOE 詞頭（right）替換異體字，若替換結果（wrong）∉ MOE → 生成 wrong→right
        # 保證：right 永遠是 MOE 認可的台灣標準詞；wrong∉MOE 由構造保證
        generated: dict[str, str] = {}
        for headword in moe_headwords:
            if len(headword) < 2:
                continue
            for i, char in enumerate(headword):
                alts: list[str] = []
                if char in primary_to_alts:
                    alts = primary_to_alts[char]
                elif char in alt_to_primary:
                    alts = [alt_to_primary[char]]
                for alt in alts:
                    wrong = headword[:i] + alt + headword[i + 1:]
                    if wrong != headword and wrong not in moe_headwords:
                        generated[wrong] = headword

        # 載入既有測試型 corrections（優先），過濾過於激進的單字條目
        tested: dict[str, str] = {}
        p = Path(corrections_path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            raw = data.get("corrections", data)
            tested = {k: v for k, v in raw.items() if len(k) >= 2}

        # tested 優先覆蓋 generated
        final_corrections = {**generated, **tested}
        
        # 載入 TWVariants (異體字標準化)，覆蓋所有先前的設定，同時用來「清洗」字典裡的 value
        v_p = Path(twvariants_path)
        if v_p.exists():
            tw_map = {}
            with open(v_p, encoding="utf-8") as f:
                for line in f:
                    if "\t" not in line or line.startswith("#"):
                        continue
                    k, v = line.rstrip("\n").split("\t", 1)
                    v = v.split()[0]
                    if k and v and k != v:
                        tw_map[k] = v
                        final_corrections[k] = v
            
            # 使用 tw_map 清洗所有已有規則的 Value (把裡面的 纔會 變成 才會)
            for k in list(final_corrections.keys()):
                val = final_corrections[k]
                new_val = "".join(tw_map.get(ch, ch) for ch in val)
                if new_val != val:
                    final_corrections[k] = new_val

        return s2t, final_corrections

    @property
    def s2t_map(self) -> dict[str, set]:
        """簡體字 → 合法繁體選項集合，供外部位置驗證使用。"""
        return self._s2t

    _PUNCT = frozenset('，。！？；：「」『』【】〔〕（）…—～、·\u3000')

    @staticmethod
    def _build_bigrams(moe_headwords: frozenset) -> frozenset[str]:
        """從 MOE 詞頭提取 bigram 集合（相鄰字對），用於邊界越界檢查。
        排除含標點的 bigram（成語字典收錄帶逗號的諺語，會誤觸邊界判斷）。
        """
        punct = PostProcessor._PUNCT
        bigrams: set[str] = set()
        for headword in moe_headwords:
            for j in range(len(headword) - 1):
                bg = headword[j:j+2]
                if not (bg[0] in punct or bg[1] in punct):
                    bigrams.add(bg)
        return frozenset(bigrams)

    # ── 套用 ────────────────────────────────────────────────────────────

    def apply(self, text: str) -> str:
        """Single-pass 最長匹配替換：每個位置只處理一次，避免連鎖修改。"""
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            candidates = self._by_first.get(text[i])
            matched = False
            if candidates:
                for wrong, right in candidates:  # 已按長度降序排列
                    end = i + len(wrong)
                    if text[i:end] != wrong:
                        continue
                    # Lookbehind：前字＋規則首字 構成已知 bigram → 規則首字屬於前面的詞
                    if i > 0 and (text[i-1] + wrong[0]) in self._bigrams:
                        continue
                    # Lookahead：規則尾字＋後字 構成已知 bigram → 規則尾字屬於後面的詞
                    if end < n and (wrong[-1] + text[end]) in self._bigrams:
                        continue
                    out.append(right)
                    key = (wrong, right)
                    snippets = self._applied.setdefault(key, [])
                    if len(snippets) < 3:
                        s = max(0, i - 12)
                        e = min(n, end + 12)
                        snippets.append(text[s:e])
                    i = end
                    matched = True
                    break
            if not matched:
                out.append(text[i])
                i += 1
        return "".join(out)

    def clear_applied_log(self):
        self._applied.clear()

    def write_applied_log(self, path: str, mode: str = "a"):
        if not self._applied:
            return
        lines = [f"\n\n=== 後處理修正紀錄 ({len(self._applied)} 條規則觸發) ===\n"]
        for idx, ((wrong, right), snippets) in enumerate(
            sorted(self._applied.items(), key=lambda x: -len(x[1])), 1
        ):
            count = len(snippets)
            lines.append(f"\n[{idx:03d}] {wrong} → {right}  ({count}+ 次)\n")
            for snip in snippets:
                lines.append(f"  …{snip}…\n")
        with open(path, mode, encoding="utf-8") as f:
            f.write("".join(lines))

    # ── 統計 ────────────────────────────────────────────────────────────

    def stats(self) -> str:
        return f"corrections: {len(self._corrections):,} 條"
