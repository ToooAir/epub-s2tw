#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
postprocess.py
兩層後處理，修正 Google 翻譯的繁體錯誤：
  Layer 1 — corrections.json：修正孤立詞組測試已知的系統性錯誤
  Layer 2 — STCharacters + STPhrases whitelist：修正 Google 斷詞錯誤造成的歧義字
"""

import json
from pathlib import Path


class PostProcessor:

    def __init__(
        self,
        corrections_path: str = "corrections.json",
        stphrases_path: str = "STPhrases.txt",
        stcharacters_path: str = "STCharacters.txt",
        twvariants_path: str = "TWVariants.txt",
    ):
        self._s2t, self._corrections = self._build_corrections(
            corrections_path, stphrases_path, stcharacters_path, twvariants_path
        )
        # 按首字分組，長詞優先：加速 single-pass 掃描
        self._by_first: dict[str, list] = {}
        for wrong, right in sorted(self._corrections.items(), key=lambda x: -len(x[0])):
            self._by_first.setdefault(wrong[0], []).append((wrong, right))

    # ── 建立 ────────────────────────────────────────────────────────────

    def _build_corrections(
        self, corrections_path: str, stphrases_path: str, stcharacters_path: str, twvariants_path: str
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

        # STPhrases：對每個詞組，生成可能的錯誤形式 → 正確形式
        generated: dict[str, str] = {}
        with open(stphrases_path, encoding="utf-8") as f:
            for line in f:
                if "\t" not in line:
                    continue
                _, t = line.rstrip("\n").split("\t", 1)
                t = t.strip().split()[0]
                for i, char in enumerate(t):
                    alts: list[str] = []
                    if char in primary_to_alts:
                        alts = primary_to_alts[char]
                    elif char in alt_to_primary:
                        alts = [alt_to_primary[char]]
                    for alt in alts:
                        wrong = t[:i] + alt + t[i + 1:]
                        if wrong != t:
                            generated[wrong] = t

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
                    if text[i:end] == wrong:
                        out.append(right)
                        i = end
                        matched = True
                        break
            if not matched:
                out.append(text[i])
                i += 1
        return "".join(out)

    # ── 統計 ────────────────────────────────────────────────────────────

    def stats(self) -> str:
        return f"corrections: {len(self._corrections):,} 條"
