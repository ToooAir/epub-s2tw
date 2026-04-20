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
        self._s2t, self._corrections, self._manual_keys = self._build_corrections(
            corrections_path, stcharacters_path, twvariants_path, moe_headwords
        )
        # 按首字分組，長詞優先：加速 single-pass 掃描
        # Layer 1（corrections.json）不受 bigram 保護；Layer 2/3 受保護
        self._by_first_manual: dict[str, list] = {}  # Layer 1：不做 bigram 檢查
        self._by_first: dict[str, list] = {}          # Layer 2/3：做 bigram 檢查
        for wrong, right in sorted(self._corrections.items(), key=lambda x: -len(x[0])):
            if wrong != right:
                if wrong in self._manual_keys:
                    self._by_first_manual.setdefault(wrong[0], []).append((wrong, right))
                else:
                    self._by_first.setdefault(wrong[0], []).append((wrong, right))
        # Bigram 邊界字典：從 MOE 詞頭提取相鄰字對
        # 用於 lookbehind / lookahead 越界檢查，防止短規則命中更長詞的片段
        self._bigrams: frozenset[str] = self._build_bigrams(moe_headwords)
        # 後處理觸發記錄：(wrong, right) → [上下文片段, ...]（最多 3 筆）
        self._applied: dict[tuple[str, str], list[str]] = {}
        # Bigram 擋住記錄：規則匹配成功但被 bigram 邊界保護跳過（潛在漏修正）
        self._blocked: dict[tuple[str, str], list[str]] = {}
        # CKIP 斷詞器（可選）：lazy init，呼叫 enable_ckip() 後才載入
        self._ckip_ws = None
        self._ckip_boundaries: set[int] = set()

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
    ) -> tuple[dict[str, set], dict, frozenset]:
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

        # tested 優先覆蓋 generated；記錄 manual keys（Layer 1，不做 bigram 保護）
        manual_keys: frozenset = frozenset(tested.keys())
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

        return s2t, final_corrections, manual_keys

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

    # ── CKIP 斷詞 ────────────────────────────────────────────────────────

    def enable_ckip(self, device: int = -1) -> None:
        """載入 ckip-transformers albert-tiny 斷詞器。
        首次呼叫會從 HuggingFace 下載模型並快取至 ~/.cache/huggingface/。
        device=-1 強制 CPU（macOS 無 CUDA，避免 fallback 延遲）。
        注意：tokenizer 由 ckip-transformers 內部從 bert-base-chinese 載入，
              不可用 AutoTokenizer.from_pretrained('ckiplab/albert-tiny-chinese-ws')。
        """
        import contextlib
        import io
        import os
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # 壓制 Loading weights 進度條
        import transformers  # type: ignore
        transformers.logging.set_verbosity_error()                    # 壓制 transformers 詳細輸出
        from ckip_transformers.nlp import CkipWordSegmenter           # type: ignore
        # redirect_stdout 壓制 ckip-transformers 的 LOAD REPORT print()
        with contextlib.redirect_stdout(io.StringIO()):
            self._ckip_ws = CkipWordSegmenter(model="albert-tiny", device=device)

    def _compute_ckip_boundaries(self, text: str) -> set[int]:
        """對整段文字斷詞，回傳所有詞起始 index 的集合。失敗時回傳空集合（退化）。
        輸入為 List[str]，輸出為 List[List[str]]，取第 0 筆。
        """
        try:
            words: list[str] = self._ckip_ws([text], show_progress=False)[0]
            starts: set[int] = set()
            pos = 0
            for w in words:
                starts.add(pos)
                pos += len(w)
            return starts
        except Exception:
            return set()

    def _ckip_cross_boundary(self, start: int, end: int) -> bool:
        """若 [start, end) 內部存在詞界（詞的起始位置），回傳 True（跨詞）。"""
        return any(b in self._ckip_boundaries for b in range(start + 1, end))

    # ── 套用 ────────────────────────────────────────────────────────────

    def apply(self, text: str) -> str:
        """Single-pass 最長匹配替換：每個位置只處理一次，避免連鎖修改。
        Layer 1（corrections.json）不做 bigram 邊界檢查（手動驗證規則，優先且無條件套用）。
        Layer 2/3（MOE 生成 + TWVariants）做 bigram 邊界保護；
          啟用 CKIP 時採雙重確認：bigram AND CKIP 皆認為跨界才跳過，任一放行則套用。
        """
        if self._ckip_ws is not None:
            self._ckip_boundaries = self._compute_ckip_boundaries(text)
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            matched = False
            # Layer 1：手動規則，不做 bigram 檢查
            for wrong, right in self._by_first_manual.get(text[i], []):
                end = i + len(wrong)
                if text[i:end] != wrong:
                    continue
                out.append(right)
                key = (wrong, right)
                snippets = self._applied.setdefault(key, [])
                if len(snippets) < 3:
                    snippets.append(text[max(0, i-12):min(n, end+12)])
                i = end
                matched = True
                break
            if not matched:
                # Layer 2/3：自動規則，做 bigram 邊界保護
                for wrong, right in self._by_first.get(text[i], []):
                    end = i + len(wrong)
                    if text[i:end] != wrong:
                        continue
                    snippet = text[max(0, i-12):min(n, end+12)]
                    blocked_by = None
                    if i > 0 and (text[i-1] + wrong[0]) in self._bigrams:
                        blocked_by = f"lookbehind:{text[i-1]+wrong[0]}"
                    elif end < n and (wrong[-1] + text[end]) in self._bigrams:
                        blocked_by = f"lookahead:{wrong[-1]+text[end]}"
                    if blocked_by:
                        # 雙重確認：CKIP 未啟用 OR CKIP 也說跨界 → 才真的跳過
                        ckip_confirms = (
                            self._ckip_ws is None or
                            self._ckip_cross_boundary(i, end)
                        )
                        if ckip_confirms:
                            key = (wrong, right)
                            bl = self._blocked.setdefault(key, [])
                            if len(bl) < 3:
                                bl.append(f"{snippet}  [{blocked_by}]")
                            continue
                        # CKIP 判定不跨詞界 → 覆蓋 bigram 保護，套用修正
                        out.append(right)
                        key = (wrong, right)
                        snippets = self._applied.setdefault(key, [])
                        if len(snippets) < 3:
                            snippets.append(f"{snippet}  [ckip↑{blocked_by}]")
                        i = end
                        matched = True
                        break
                    out.append(right)
                    key = (wrong, right)
                    snippets = self._applied.setdefault(key, [])
                    if len(snippets) < 3:
                        snippets.append(snippet)
                    i = end
                    matched = True
                    break
            if not matched:
                out.append(text[i])
                i += 1
        return "".join(out)

    def clear_applied_log(self):
        self._applied.clear()
        self._blocked.clear()

    def write_blocked_log(self, path: str, mode: str = "a"):
        """輸出被 bigram 邊界保護擋住的規則（潛在漏修正），供 review 用。"""
        if not self._blocked:
            return
        lines = [f"\n\n=== Bigram 攔截紀錄 ({len(self._blocked)} 條規則被擋) ===\n"]
        lines.append("  ↑ 這些規則匹配成功但被 bigram 邊界保護跳過，可能是漏修正，請人工確認\n")
        for idx, ((wrong, right), snippets) in enumerate(
            sorted(self._blocked.items(), key=lambda x: -len(x[1])), 1
        ):
            lines.append(f"\n[{idx:03d}] {wrong} → {right}  ({len(snippets)}+ 次)\n")
            for snip in snippets:
                lines.append(f"  …{snip}…\n")
        with open(path, mode, encoding="utf-8") as f:
            f.write("".join(lines))

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
