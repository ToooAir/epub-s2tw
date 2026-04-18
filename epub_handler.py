#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
epub_handler.py
EPUB 讀取、文字抽取、翻譯替換、儲存。

處理策略：
  1. 逐一掃描 spine 中的 XHTML 文件
  2. 找出 block-level 元素（<p>, <h1>~<h6>, <li> 等）
  3. 若元素含有 inline HTML（<em>, <strong> 等），以 HTML 格式送翻譯 API
     否則以純文字批次送出（速度更快、更省費用）
  4. 把翻譯結果寫回對應元素
  5. 更新 OPF metadata（dc:title, dc:description 等）
"""

import re
import zipfile
from collections import Counter, defaultdict

from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub
from tqdm import tqdm

_EMPTY: frozenset = frozenset()  # 空集合常數，避免 defaultdict 每次建新物件

# 要翻譯的 block-level tag
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "td", "th", "blockquote", "figcaption", "dt", "dd",
}

# 這些標籤內的文字不翻譯（程式碼、注音、旁注）
SKIP_ANCESTOR = {"script", "style", "code", "pre", "rt", "rp"}

# 含有這些子標籤代表有 inline 格式，以 HTML 模式翻譯
INLINE_TAGS = {"em", "strong", "b", "i", "u", "s", "span", "a", "mark"}

# OPF metadata 欄位
META_FIELDS = ["title", "description", "publisher", "subject"]


# ── HTML 工具 ──────────────────────────────────────────────────────────

def _has_inline(tag) -> bool:
    return bool(tag.find(INLINE_TAGS))


def _replace_inner_html(tag, new_html: str):
    """把 tag 的內容替換為 new_html（保留 tag 本身）。"""
    tag.clear()
    wrapper = BeautifulSoup(f"<div>{new_html}</div>", "html.parser")
    if wrapper.div:
        for child in list(wrapper.div.children):
            tag.append(child)


# ── 段落搜尋法：source n-gram 在 target 段落中找合法翻譯形式 ──────────

def _plausible(s_ng: str, t_ng: str, s2t: dict, is_all_fixed: bool = False) -> bool:
    """判斷 t_ng 是否為 s_ng 的位置對齊合法翻譯。
    """
    if is_all_fixed:
        diffs = 0
        for sc, tc in zip(s_ng, t_ng):
            if sc != tc:
                diffs += 1
                if diffs > 1:
                    return False
        # 放寬到長度 3（允許高頻專有名詞進入候選名單），嚴格過濾交由 _is_valid_replacement
        if len(s_ng) < 3 and diffs > 0:
            return False
        return True

    for sc, tc in zip(s_ng, t_ng):
        if sc == tc:
            continue
        if sc not in s2t:
            # 固定字：必須完全相同
            return False
        else:
            # 轉換字：必須是該字的已知合法繁體
            if tc not in s2t[sc]:
                return False
    return True


def _aligned_regions(src: str, tgt: str, s2t: dict, t2s: dict) -> list[tuple[int, int]]:
    """回傳 (start, end) 區間，代表 src/tgt 位置對齊合法的連續段落。

    合法條件（三者之一）：
      - src[i] == tgt[i]：字元相同，無需轉換
      - tgt[i] 是 src[i] 的已知繁體形式（s2t）
      - tgt[i] 不是任何簡體字的已知繁體形式（t2s reverse map 查不到）→ 音譯/轉寫字，允許通過

    斷點：tgt[i] 屬於「其他」簡體字的繁體（Google 在此換詞）。
    """
    regions: list[tuple[int, int]] = []
    start: int | None = None
    for i, (sc, tc) in enumerate(zip(src, tgt)):
        valid = sc == tc or tc in s2t.get(sc, ()) or tc not in t2s
        if valid:
            if start is None:
                start = i
        else:
            if start is not None:
                regions.append((start, i))
                start = None
    if start is not None:
        regions.append((start, len(src)))
    return regions


# ── 主要處理函式 ───────────────────────────────────────────────────────

def process_xhtml(content: bytes, translator, postprocessor=None, pairs_collector: list | None = None) -> bytes:
    """翻譯一個 XHTML 文件的內容並回傳修改後的 bytes。"""
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        decoded = content.decode("utf-8", errors="replace")

    # 嘗試用 lxml-xml 解析（EPUB 標準），回退 html.parser
    try:
        soup = BeautifulSoup(decoded, "lxml-xml")
    except Exception:
        soup = BeautifulSoup(decoded, "html.parser")

    plain_segs = []   # (tag, text)        → 純文字批次翻譯
    html_segs  = []   # (tag, inner_html)  → HTML 格式翻譯

    for tag in soup.find_all(BLOCK_TAGS):
        # 跳過在 script / code / rt 等標籤內的元素
        if any(p.name in SKIP_ANCESTOR for p in tag.parents):
            continue

        if _has_inline(tag):
            inner = tag.decode_contents()
            if inner.strip():
                html_segs.append((tag, inner))
        else:
            text = tag.get_text()
            if text.strip():
                plain_segs.append((tag, text))

    # 抽取 <head><title> 供後續翻譯（<title> 不在 BLOCK_TAGS，需單獨處理）
    title_tag  = soup.find('title')
    title_text = title_tag.get_text().strip() if title_tag else None

    # 沒有任何可翻譯內容，直接回傳原始 bytes，避免不必要的重新序列化
    if not plain_segs and not html_segs and not title_text:
        return content

    pp = postprocessor

    # 純文字批次翻譯
    if plain_segs:
        texts      = [t for _, t in plain_segs]
        translated = translator.translate_batch(texts, fmt="text")
        for (tag, orig), new_text in zip(plain_segs, translated):
            final = pp.apply(new_text) if pp else new_text
            tag.string = final
            if pairs_collector is not None:
                pairs_collector.append((orig, final))

    # HTML 格式翻譯（含 inline 標籤）
    if html_segs:
        html_texts      = [h for _, h in html_segs]
        translated_html = translator.translate_batch(html_texts, fmt="html")
        for (tag, orig_html), new_html in zip(html_segs, translated_html):
            final_html = pp.apply(new_html) if pp else new_html
            _replace_inner_html(tag, final_html)
            if pairs_collector is not None:
                orig_text = BeautifulSoup(orig_html, "html.parser").get_text()
                tgt_text  = BeautifulSoup(final_html, "html.parser").get_text()
                if orig_text.strip():
                    pairs_collector.append((orig_text, tgt_text))

    result = str(soup)
    # lxml-xml 會把 SVG 的 viewBox 小寫化，需還原（SVG 屬性大小寫敏感）
    result = re.sub(r'\bviewbox\b', 'viewBox', result)
    # lxml-xml 序列化時會丟掉 <head> 內的 <link>/<meta>，還原成原始 head
    # 注意：lxml-xml 輸出空 head 為 <head/>（self-closing），需同時匹配兩種形式
    HEAD_PAT = re.compile(r'<head(?:\s[^>]*)?(?:/>|>.*?</head>)', re.DOTALL | re.IGNORECASE)
    orig_head = HEAD_PAT.search(decoded)
    if orig_head:
        result = HEAD_PAT.sub(orig_head.group(0), result, count=1)

    # HEAD_PAT 還原了原始 head（含未翻譯 title），此處將 title 替換為翻譯版本
    if title_text:
        translated_title = translator.translate(title_text)
        if pp:
            translated_title = pp.apply(translated_title)
        TITLE_PAT = re.compile(r'(<title[^>]*>)\s*.*?\s*(</title>)', re.DOTALL | re.IGNORECASE)
        result = TITLE_PAT.sub(rf'\g<1>{translated_title}\g<2>', result, count=1)

    return result.encode("utf-8")


# ── EpubProcessor ──────────────────────────────────────────────────────

class EpubProcessor:

    def __init__(self, path: str):
        self.path = path
        self.book = epub.read_epub(path, {"ignore_ncx": False})
        self._text_pairs: list[tuple[str, str]] = []  # (原文, 翻譯) 純文字對
        # 保留原始 ZIP，讓 process_xhtml 能拿到未經 ebooklib 解析的 raw bytes
        self._zipfile = zipfile.ZipFile(path, "r")
        # 推算 EPUB content 根目錄（OPF 所在資料夾，通常是 OEBPS/ 或 EPUB/）
        opf_candidates = [n for n in self._zipfile.namelist() if n.endswith(".opf")]
        opf_dir = opf_candidates[0].rsplit("/", 1)[0] + "/" if opf_candidates else ""
        self._epub_root = opf_dir  # e.g. "OEBPS/"

    def translate(self, translator, postprocessor=None, verbose: bool = False, report_path: str | None = None):
        """翻譯整本 EPUB 的 HTML 文件與 metadata。"""
        docs = [i for i in self.book.get_items()
                if i.get_type() == ebooklib.ITEM_DOCUMENT
                and not isinstance(i, epub.EpubNav)]

        with tqdm(docs, unit="xhtml", dynamic_ncols=True) as bar:
            for item in bar:
                name = item.get_name()
                if verbose:
                    bar.set_description(name)
                try:
                    zip_path = self._epub_root + item.get_name()
                    try:
                        raw = self._zipfile.read(zip_path)
                    except KeyError:
                        raw = item.get_content()
                    new_content = process_xhtml(raw, translator, postprocessor, self._text_pairs)
                    item.set_content(new_content)
                    item.get_content = lambda _bytes=new_content, default=None: _bytes
                except Exception as e:
                    tqdm.write(f"  ⚠️  跳過 {name}: {e}")

        self._translate_ncx(translator, postprocessor)
        self._translate_toc(translator, postprocessor)
        self._translate_metadata(translator, postprocessor)
        s2t = postprocessor.s2t_map if postprocessor else {}
        self._consistency_pass(docs, s2t_map=s2t, report_path=report_path)

    def _translate_ncx(self, translator, postprocessor=None):
        """翻譯 toc.ncx 中的 <navLabel><text> 與 <docTitle><text>。"""
        for item in self.book.get_items():
            if not item.get_name().lower().endswith(".ncx"):
                continue
            try:
                decoded = item.get_content().decode("utf-8", errors="replace")
                soup = BeautifulSoup(decoded, "lxml-xml")
                text_tags = soup.find_all("text")
                if not text_tags:
                    continue
                originals  = [t.get_text() for t in text_tags]
                translated = translator.translate_batch(originals)
                for tag, new_text in zip(text_tags, translated):
                    tag.string = postprocessor.apply(new_text) if postprocessor else new_text
                item.set_content(str(soup).encode("utf-8"))
            except Exception as e:
                tqdm.write(f"  ⚠️  跳過 NCX: {e}")

    def _translate_toc(self, translator, postprocessor=None):
        """翻譯 book.toc（ebooklib 用來生成 nav.xhtml 的來源）。"""
        from ebooklib.epub import Link, Section
        pp = postprocessor

        def _t(text):
            result = translator.translate(text)
            return pp.apply(result) if pp else result

        def translate_entries(entries):
            for entry in entries:
                if isinstance(entry, Link):
                    entry.title = _t(entry.title)
                elif isinstance(entry, Section):
                    entry.title = _t(entry.title)
                    if entry.children:
                        translate_entries(entry.children)
                elif isinstance(entry, tuple) and len(entry) == 2:
                    translate_entries([entry[0]])
                    translate_entries(entry[1])

        try:
            translate_entries(self.book.toc)
        except Exception as e:
            tqdm.write(f"  ⚠️  翻譯 TOC 失敗: {e}")

    def _translate_metadata(self, translator, postprocessor=None):
        pp = postprocessor
        DC = "http://purl.org/dc/elements/1.1/"
        for field in META_FIELDS:
            try:
                items = self.book.get_metadata("DC", field)
                if not items:
                    continue
                self.book.metadata[DC][field] = [
                    (
                        (pp.apply(translator.translate(value)) if pp else translator.translate(value))
                        if value else value,
                        attrs,
                    )
                    for value, attrs in items
                ]
            except Exception as e:
                tqdm.write(f"  ⚠️  metadata {field}: {e}")
        # book.title 是 nav.xhtml <title>/<h2> 的來源，需同步更新
        try:
            titles = self.book.get_metadata("DC", "title")
            if titles:
                self.book.title = self.book.metadata[DC]["title"][0][0]
        except Exception:
            pass
        # 翻譯 OPF meta：belongs-to-collection（系列名）與 file-as（排序標題）
        OPF = "http://www.idpf.org/2007/opf"
        TRANSLATE_PROPS = {"belongs-to-collection", "file-as"}
        try:
            if None in self.book.metadata.get(OPF, {}):
                self.book.metadata[OPF][None] = [
                    (
                        (pp.apply(translator.translate(v)) if pp else translator.translate(v))
                        if attrs.get("property") in TRANSLATE_PROPS and v else v,
                        attrs,
                    )
                    for v, attrs in self.book.metadata[OPF][None]
                ]
        except Exception as e:
            tqdm.write(f"  ⚠️  OPF meta: {e}")

    def _consistency_pass(
        self,
        docs,
        s2t_map: dict | None = None,
        min_total: int = 10,
        min_minority: int = 2,
        max_minor_ratio: float = 0.33,
        report_path: str | None = None,
    ):
        """整本書翻譯完後，統一同一原文被翻成不同繁體寫法的情況。

        演算法：
          1. 利用 _text_pairs（原文, 翻譯）的位置對齊，建立
             { 原文n-gram → Counter(翻譯n-gram) }
          2. 同一原文 n-gram 對應多個翻譯形式 → 少數派統一成多數派
          3. 套用回每個 XHTML item
        """
        if not self._text_pairs:
            return

        # 動態計算 min_total：段落越少門檻越低，避免樣本不足時無法觸發
        pair_count = len(self._text_pairs)
        min_total = max(4, pair_count // 200)
        # 專有名詞高頻豁免門檻：某個詞出現次數大於此值，將豁免嚴格的全固定詞安全鎖
        entity_thresh = max(15, pair_count // 50)

        cjk = re.compile(r'^[\u4e00-\u9fff\u3400-\u4dbf]+$')
        s2t = s2t_map or {}
        # t2s：繁體字 → 來源簡體字（用於偵測 Google 字形混淆，如 莉→麗 來自 丽→麗）
        t2s: dict[str, str] = {
            trad: simp
            for simp, trads in s2t.items()
            for trad in trads
            if trad != simp
        }

        # ── 1. 建立原文 n-gram → 翻譯 n-gram 計數（段落搜尋法）─────────
        # 不依賴字元位置對齊；改為在 target 段落中搜尋 source n-gram 的合法翻譯形式：
        # - 固定字（不在 s2t_keys）：target 位置必須是同一字
        # - 轉換字（在 s2t_keys，如 丽→麗/莉）：target 位置允許任何非簡體 CJK
        # 每個 (src, tgt) 段落對，每個 (s_ng, t_ng) 配對只計一次。
        s2t_keys = frozenset(s2t.keys())
        src_to_tgt: dict[str, Counter] = defaultdict(Counter)

        for src, tgt in self._text_pairs:
            # 建立 target n-gram 索引：(長度, 首字) → set(t_ng)
            # 用首字做第一層過濾，把候選從 O(全 n-gram) 降到 O(幾個)
            tgt_idx: dict[tuple[int, str], set[str]] = defaultdict(set)
            for n in range(3, 5):
                for j in range(len(tgt) - n + 1):
                    t_ng = tgt[j:j+n]
                    if cjk.match(t_ng):
                        tgt_idx[(n, t_ng[0])].add(t_ng)

            # 每個 (s_ng, t_ng) 對在本段落只計一次
            local_pairs: set[tuple[str, str]] = set()
            for n in range(3, 5):
                for i in range(len(src) - n + 1):
                    s_ng = src[i:i+n]
                    if not cjk.match(s_ng):
                        continue
                    sc0 = s_ng[0]
                    is_all_fixed = all(sc not in s2t_keys for sc in s_ng)
                    
                    if sc0 not in s2t_keys:
                        # 固定首字：target 以同字開頭
                        candidates = tgt_idx.get((n, sc0), _EMPTY)
                    else:
                        # 轉換首字：target 以各 s2t 對應形式開頭
                        candidates = set()
                        for tc0 in s2t.get(sc0, ()):
                            candidates |= tgt_idx.get((n, tc0), _EMPTY)
                    for t_ng in candidates:
                        if (s_ng, t_ng) not in local_pairs and _plausible(s_ng, t_ng, s2t, is_all_fixed):
                            # 反向碰撞檢查 (Competitive Alignment)：
                            # 若 t_ng (如 艾莉亞) 與本段落中另一個原文 s_alt (如 艾莉娅) 的字元差異數，
                            # 小於或等於它與 s_ng (如 艾莉莎) 的差異數，表示該翻譯有歧義，放棄歸屬！
                            if is_all_fixed and s_ng != t_ng:
                                my_diff = sum(1 for a, b in zip(s_ng, t_ng) if a != b)
                                ambiguous = False
                                for i in range(len(src) - n + 1):
                                    s_alt = src[i:i+n]
                                    if s_alt != s_ng:
                                        alt_diff = sum(1 for a, b in zip(s_alt, t_ng) if a != b)
                                        if alt_diff <= my_diff:
                                            ambiguous = True
                                            break
                                if ambiguous:
                                    continue
                            local_pairs.add((s_ng, t_ng))

            for s_ng, t_ng in local_pairs:
                src_to_tgt[s_ng][t_ng] += 1

        # ── 2. 找出翻譯不一致的原文 n-gram ───────────────────────────
        def _is_valid_replacement(s_ng: str, wrong: str, right: str, s2t: dict, is_all_fixed: bool, total_count: int, entity_thresh: int) -> bool:
            """驗證把 wrong 換成 right 在語意上是合法的繁體字形修正。"""
            if is_all_fixed:
                # 全固定字情況下，只允許至多一個字的差異
                diffs = sum(1 for wc, rc in zip(wrong, right) if wc != rc)
                if len(s_ng) < 4 and diffs > 0:
                    # 豁免條款：長度為 3 的高頻全固定詞，若出現頻率極高，視為重要核心名詞
                    if len(s_ng) == 3 and total_count >= entity_thresh:
                        return diffs <= 1
                    return False
                return diffs <= 1

            for sc, wc, rc in zip(s_ng, wrong, right):
                if wc == rc:
                    continue
                if rc not in s2t.get(sc, set()):
                    return False
            return True

        normalization: dict[str, str] = {}  # {少數翻譯: 多數翻譯}
        wrong_to_source: dict[str, str] = {}  # 用於 debug 追蹤 wrong 是從哪個原文配對來的

        # ── Pass 1: 建立全域合法標靶 (Global Legitimate Targets) ──────────
        # 避免把「雙胞胎兄弟的合法名字」當成偶然的錯字庫給全域抹除掉
        global_legitimate_targets: set[str] = set()
        s_ng_to_majority: dict[str, str] = {}
        for s_ng, tgt_counts in src_to_tgt.items():
            if sum(tgt_counts.values()) < min_total:
                continue
            is_all_fixed = all(sc not in s2t_keys for sc in s_ng)
            if is_all_fixed and s_ng in tgt_counts:
                # 絕對真理法則 (Absolute Truth Defense):
                # 若原文全為固定字，其完美的繁體映射必為自身。
                # 防止 Google 幻覺翻譯發生 51% 攻擊（如 15 次「政近一步」贏過 5 次「政近一行」），
                # 我們強行剝奪 AI 的多數決權力，擁戴絕對真理！
                majority_tgt = s_ng
            else:
                majority_tgt = tgt_counts.most_common(1)[0][0]
            s_ng_to_majority[s_ng] = majority_tgt
            global_legitimate_targets.add(majority_tgt)

        # ── Pass 2: 建立一致性修正對應表 ──────────
        for s_ng, tgt_counts in src_to_tgt.items():
            if len(tgt_counts) < 2:
                continue
            total = sum(tgt_counts.values())
            if total < min_total:
                continue
                
            is_all_fixed = all(sc not in s2t_keys for sc in s_ng)
            majority_tgt = s_ng_to_majority[s_ng]
            
            # Fix 2：多數派與原文相同（未轉換）→ 若為非全固定字才跳過，避免把合法繁體改回簡體
            if majority_tgt == s_ng:
                if not is_all_fixed:
                    continue
                    
            entity_thresh = max(15, total // 50)
            
            for t_ng, count in tgt_counts.items():
                if t_ng == majority_tgt:
                    continue
                    
                # 【全域防撞機制】：若此候選本身就是本書其他合法名詞的正解（例如雙胞胎），絕不能抹除它！
                if t_ng in global_legitimate_targets:
                    continue
                    
                # 如果是真理模式，無視少數派比例強制清除 AI 幻覺；否則受限於容錯閾值
                if not (is_all_fixed and majority_tgt == s_ng):
                    if count < min_minority or count / total >= max_minor_ratio:
                        continue
                    if not _is_valid_replacement(s_ng, t_ng, majority_tgt, s2t, is_all_fixed, total, entity_thresh):
                        continue
                    # Fix 3：wrong 和 right 都是原文 n-gram 的合法 s2t 繁體 →
                    # 這是歧義字（如 发→發/髮），語意取決於上下文，交給 postprocessor 負責
                    if all(
                        wc == rc
                        or (wc in s2t.get(sc, ()) and rc in s2t.get(sc, ()))
                        for sc, wc, rc in zip(s_ng, t_ng, majority_tgt)
                    ):
                        continue
                    normalization[t_ng] = majority_tgt
                    wrong_to_source[t_ng] = s_ng

        if not normalization:
            return

        # ── 2b. 核心差異精簡：提煉最短差異前綴 ──────────
        # 將長度較長但不影響差異字的後綴去除，提升修正覆蓋率（例如 艾麗莎不 → 艾麗）
        optimized_norm: dict[str, str] = {}
        optimized_source: dict[str, str] = {}
        conflict_cores: set[str] = set()

        for wrong, right in list(normalization.items()):
            diff_indices = [i for i, (w, r) in enumerate(zip(wrong, right)) if w != r]
            if not diff_indices:
                continue
            last_diff = max(diff_indices)
            core_wrong = wrong[:last_diff + 1]
            core_right = right[:last_diff + 1]
            
            # 【高危單字元或雙字元全固定字防呆機制】
            is_core_all_fixed = all(sc not in s2t_keys for sc in wrong_to_source[wrong][:last_diff + 1])
            if is_core_all_fixed and len(core_wrong) < 4:
                # 核心長度不足 4 的全固定字，擴散替換風險過高（例如 雪底 → 雪之）
                # 退回使用穩定的完整 n-gram 規則，不作精簡！
                core_wrong = wrong
                core_right = right

            if core_wrong in conflict_cores:
                continue
            
            if core_wrong in optimized_norm:
                if optimized_norm[core_wrong] != core_right:
                    del optimized_norm[core_wrong]
                    del optimized_source[core_wrong]
                    conflict_cores.add(core_wrong)
            else:
                optimized_norm[core_wrong] = core_right
                optimized_source[core_wrong] = wrong_to_source[wrong]

        normalization = optimized_norm
        wrong_to_source = optimized_source

        if not normalization:
            return

        # ── 2c. 冗餘片段過濾：移除被較短規則完全涵蓋的冗餘長規則 ──────────
        # 若 wa (短) 是 wb (長) 的子字串，且對應位置的替換結果也同步，
        # 則 wb 是多餘的（wa 已經能完美涵蓋 wb 的替換行為），保留 wa 並刪除 wb。
        keys = list(normalization.keys())
        subfrags: set[str] = set()
        for wa in keys:
            ra = normalization[wa]
            for wb in keys:
                if wa == wb or len(wb) <= len(wa):
                    continue
                rb = normalization[wb]
                # 在 wb/rb 中搜尋 wa/ra 出現的同一位置
                p = 0
                while True:
                    p = wb.find(wa, p)
                    if p == -1:
                        break
                    if rb[p:p + len(ra)] == ra:
                        subfrags.add(wb)  # 刪除長的（冗餘），保留短的（通用）
                        break
                    p += 1
                    
        # ── 2d. 矛盾對立過濾：解決互逆的翻譯循環 ──────────
        # 若 shorter_rule 的正確翻譯(ra) 剛好是 longer_rule 企圖消滅的錯字(wb)
        # 代表 longer_rule 在局部出現了相反的多數決，這會造成取代循環或反向破壞。
        # 由於 shorter_rule 擁有更廣的泛用性與絕對多數支撐，必定以短規則為準，刪除長規則。
        for wa in keys:
            ra = normalization[wa]
            for wb in keys:
                if wa == wb or wb in subfrags:
                    continue
                rb = normalization[wb]
                if ra in wb or wa in rb:
                    if len(wa) < len(wb):
                        subfrags.add(wb)  # 刪除長的（局部謬誤），保留短的（全域共識）
                        
        for k in subfrags:
            del normalization[k]

        if not normalization:
            return

        tqdm.write(f"  📝 一致性修正：{len(normalization)} 組")
        for wrong, right in list(normalization.items())[:5]:
            tqdm.write(f"       {wrong} → {right}")

        # ── 3. 套用回每個 item，同時收集 debug 資訊 ──────────────────
        tag_re   = re.compile(r"<[^>]+>")
        sorted_norm = sorted(normalization.items(), key=lambda x: -len(x[0]))

        # wrong → [(before, after), ...]  每組最多保留 5 個例句
        debug_hits: dict[str, list[tuple[str, str]]] = {w: [] for w, _ in sorted_norm}

        for item in docs:
            try:
                content_bytes = item.get_content()
                content = content_bytes.decode("utf-8", errors="ignore")
                plain   = tag_re.sub("", content)
                for wrong, right in sorted_norm:
                    if wrong not in content:
                        continue
                    # 收集例句（在 plain text 中搜尋，最多 5 句）
                    hits = debug_hits[wrong]
                    if len(hits) < 5:
                        idx = 0
                        while len(hits) < 5:
                            pos = plain.find(wrong, idx)
                            if pos == -1:
                                break
                            ctx_s = max(0, pos - 35)
                            ctx_e = min(len(plain), pos + len(wrong) + 35)
                            before = plain[ctx_s:ctx_e].replace("\n", " ").strip()
                            after  = before.replace(wrong, right, 1)
                            hits.append((before, after))
                            idx = pos + 1
                            
                # Fix C: 在 BeautifulSoup 的文字節點上進行替換，防止破壞 HTML 標籤屬性
                try:
                    soup = BeautifulSoup(content_bytes, "lxml-xml")
                except Exception:
                    soup = BeautifulSoup(content_bytes, "html.parser")

                changed = False
                for text_node in soup.find_all(string=True):
                    if text_node.parent and text_node.parent.name in SKIP_ANCESTOR:
                        continue
                    new_text = str(text_node)
                    original_text = new_text
                    for wrong, right in sorted_norm:
                        if wrong in new_text:
                            new_text = new_text.replace(wrong, right)
                    if new_text != original_text:
                        text_node.replace_with(new_text)
                        changed = True

                if changed:
                    result = str(soup)
                    result = re.sub(r'\bviewbox\b', 'viewBox', result)
                    HEAD_PAT = re.compile(r'<head(?:\s[^>]*)?(?:/>|>.*?</head>)', re.DOTALL | re.IGNORECASE)
                    orig_head = HEAD_PAT.search(content)
                    if orig_head:
                        result = HEAD_PAT.sub(orig_head.group(0), result, count=1)
                    patched = result.encode("utf-8")
                    item.set_content(patched)
                    item.get_content = lambda b=patched, d=None: b
            except Exception:
                continue

        # ── 4. 寫出報告檔案 ─────────────────────────────────────────
        if report_path:
            import os
            try:
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(f"=== 一致性修正報告 ({len(normalization)} 組) ===\n\n")
                    for idx, (wrong, right) in enumerate(sorted_norm, 1):
                        hits = debug_hits.get(wrong, [])
                        source_str = wrong_to_source.get(wrong, "unknown")
                        f.write(f"[{idx:02d}] {wrong} → {right}  (source: {source_str}, {len(hits)} 個例句)\n")
                        for before, after in hits:
                            f.write(f"  修正前：…{before}…\n")
                            f.write(f"  修正後：…{after}…\n")
                        f.write("\n")
                tqdm.write(f"  📄 一致性報告已輸出：{os.path.basename(report_path)}")
            except Exception as e:
                tqdm.write(f"  ⚠️  報告寫入失敗：{e}")

    def save(self, output_path: str):
        epub.write_epub(output_path, self.book, {})
