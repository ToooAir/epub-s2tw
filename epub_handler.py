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


# ── Opaque-inline sentinel substitution ──────────────────────────────
# 翻譯前將無文字內容的 inline 元素（如 <a><img/></a> 腳注錨點）替換為
# 私用區 Unicode sentinel，翻譯後還原，避免 NMT 看到雜訊產生幻覺。
#
# 擴充：使用啟發式規則判斷「語意 opaque」元素（如人名、書名、章首數字等），
# 即使有文字內容也整體 sentinel 化，防止 NMT 產生幻覺（如 户冢彩加 → 戶口吃…）。

_SENTINEL_L = "\uE000"   # Unicode Private Use Area，NMT 幾乎不翻譯
_SENTINEL_R = "\uE001"
_SENTINEL_RE = re.compile(r"\uE000(\d+)\uE001")

# 日文假名（會出現在人名旁）
_HAS_KANA = re.compile(r'[\u3040-\u30FF]')
# 全大寫 / 片假名長串（英文人名、日文外來語人名）
_KATAKANA_NAME = re.compile(r'^[\u30A0-\u30FF]{3,}$')
# 西里爾字母（俄文）特徵（允許包含標點與空白）
_RUSSIAN_TEXT = re.compile(r'^[\u0400-\u04FF\s\,\.\!\?\'\"「」]+$')

# 語意關鍵字，子串包含即視為 opaque
_OPAQUE_CLASS_KEYWORDS: frozenset[str] = frozenset({
    "name",      # char-name, person-name, illus-name, ruby-name...
    "title",     # book-title, vol-title...
    "ruby",      # ruby-base, ruby-text...
    "con-box",   # con-box, con-box2（章節編號）
    "chara",     # chara, character
    "author",
    "illus",     # illustrator name
    "label",     # section label
    "num",       # chapter number
    "index",     # index markers
})


def _is_structurally_opaque(el) -> bool:
    """
    結構上屬於 opaque 的 inline 元素：
    - 無任何文字節點（只有圖片、span 等空殼）
    - 是 <ruby> 標籤（底字 + 注音，整體應 sentinel 化）
    """
    if not el.get_text().strip():
        return True
    if el.name == "ruby":
        return True
    return False


def _is_opaque_class(el) -> bool:
    """Class 名稱語意比對（模糊匹配，跨書通用）"""
    classes = el.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return any(
        any(keyword in cls.lower() for keyword in _OPAQUE_CLASS_KEYWORDS)
        for cls in classes
    )


def _is_content_opaque(el) -> bool:
    """根據文字內容特徵判斷是否為人名/書名等專有名詞。"""
    text = el.get_text().strip()
    if not text:
        return False
    if _KATAKANA_NAME.match(text):
        return True
    if _RUSSIAN_TEXT.match(text):
        return True
    # 短文字 + 有假名 → 可能是帶注音的人名
    if len(text) <= 8 and _HAS_KANA.search(text):
        return True
    return False


def _is_opaque_by_context(el) -> bool:
    """父元素是 opaque class → 子 inline 元素也應 sentinel 化。"""
    parent = el.parent
    if parent and hasattr(parent, "get"):
        return _is_opaque_class(parent)
    return False


def _is_opaque(el) -> bool:
    """
    通用 opaque 偵測，取代硬編碼的 OPAQUE_CLASSES。
    優先序：結構 > class 關鍵字 > 內容啟發式 > 父層上下文
    """
    return (
        _is_structurally_opaque(el)
        or _is_opaque_class(el)
        or _is_content_opaque(el)
        or _is_opaque_by_context(el)
    )


def _collect_opaque_roots(tag) -> list:
    """DFS 收集最外層的 opaque inline 元素。"""
    results = []
    def _walk(el):
        for child in list(el.children):
            if not hasattr(child, "name") or not child.name:
                continue
            if child.name in BLOCK_TAGS:
                _walk(child)
                continue
            if _is_opaque(child):
                results.append(child)
            else:
                _walk(child)
    _walk(tag)
    return results


def _apply_opaque_sentinels(tag) -> list[str]:
    """將 opaque inline 元素替換為 sentinel（原地修改 tag）。
    回傳原始元素 HTML 字串的有序清單，供還原使用。
    """
    mapping: list[str] = []
    for el in _collect_opaque_roots(tag):
        mapping.append(str(el))
        el.replace_with(f"{_SENTINEL_L}{len(mapping) - 1}{_SENTINEL_R}")
    return mapping


def _restore_opaque_sentinels(html: str, mapping: list[str]) -> str:
    """將 sentinel token 置換回原始元素 HTML。"""
    if not mapping:
        return html
    def _sub(m: re.Match) -> str:
        idx = int(m.group(1))
        return mapping[idx] if idx < len(mapping) else m.group(0)
    return _SENTINEL_RE.sub(_sub, html)


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


# ── 譯前命名實體保護 ───────────────────────────────────────────────────

def _cliff_threshold(counts: dict[str, int], hard_floor: int) -> int:
    """
    從 N-gram 計數字典中，找出動態門檻以濾除低頻雜訊。
    策略：
    1. 尋找所有相鄰比值 >= 2.0 的「斷層」，採用「最後一個斷層」的上界。
       這能有效包容多個梯隊（如：主角群 600次 -> 配角 175次 -> 雜訊 70次，會停在 175）
    2. 若無明顯斷層，則採用最高頻詞的 15% 作為相對門檻（Zipf's law 經驗法則）。
    保底：回傳值絕不低於 hard_floor。
    """
    if not counts:
        return hard_floor
        
    sorted_counts = sorted(counts.values(), reverse=True)
    if len(sorted_counts) <= 1:
        return hard_floor
        
    best_cut_value = sorted_counts[0]
    found_cliff = False
    
    for i in range(len(sorted_counts) - 1):
        if sorted_counts[i + 1] == 0:
            continue
        ratio = sorted_counts[i] / sorted_counts[i + 1]
        # 只要發生超過兩倍的斷崖式下跌，我們就把防線推進到這裡
        if ratio >= 2.0:
            best_cut_value = sorted_counts[i]
            found_cliff = True
            
    if found_cliff:
        return max(best_cut_value, hard_floor)
        
    # 若分佈太平滑，無顯著斷層，改用最高頻的 15% 作為門檻
    max_val = sorted_counts[0]
    relative_threshold = int(max_val * 0.15)
    return max(relative_threshold, hard_floor)

def scan_protected_entities(
    raw_docs: list[bytes],
    s2t_keys: frozenset[str],
    min_freq: int = 8,
    ng_range: tuple = (2, 4),
    moe_words: frozenset[str] = frozenset()
) -> dict[str, int]:
    """掃描全書，找出高頻且不需繁簡轉換的固定詞（如人名）。"""
    cjk = re.compile(r'^[\u4e00-\u9fff\u3400-\u4dbf]+$')
    counter = Counter()
    all_texts: list[str] = []  # 用於左側語境檢查
    
    # 過濾常見語法助詞與代名詞，避免切碎句子結構
    stop_chars = set("的了着在是不也就和与到以得真啊嘆哎一我你他她它这那哪其个们些么")

    
    for raw in raw_docs:
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("utf-8", errors="replace")
        try:
            soup = BeautifulSoup(decoded, "lxml-xml")
        except Exception:
            soup = BeautifulSoup(decoded, "html.parser")
            
        for tag in soup.find_all(BLOCK_TAGS):
            if any(p.name in SKIP_ANCESTOR for p in tag.parents):
                continue
            text = tag.get_text()
            if not text.strip():
                continue
            all_texts.append(text)  # 收集所有文字內容
            
            for n in range(ng_range[0], ng_range[1] + 1):
                for i in range(len(text) - n + 1):
                    s_ng = text[i:i+n]
                    if not cjk.match(s_ng):
                        continue
                    if any(c in stop_chars for c in s_ng):
                        continue
                    if s_ng in moe_words:
                        continue
                    # 全字元皆需為固定字（無繁簡差異），確保 sentinel 還原後不需要 s2t 轉換
                    if all(sc not in s2t_keys for sc in s_ng):
                        counter[s_ng] += 1
                        
    # 僅對初步達標（>= min_freq）的候選詞套用動態斷層過濾
    candidates = {s: c for s, c in counter.items() if c >= min_freq}
    dynamic_threshold = _cliff_threshold(candidates, hard_floor=min_freq)
    
    entities: dict[str, int] = {}
    for s_ng, count in candidates.items():
        if count >= dynamic_threshold:
            entities[s_ng] = count

    # 左側語境過濾：若候選詞幾乎總是接在特定 s2t 字元後面，代表它是更長名詞的尾段，應移除
    # 例：「藤小姐」幾乎總是接在「后」（s2t 字）後面 → 它是「后藤小姐」的尾段，不應独立保護
    if all_texts and entities:
        combined_text = "\n".join(all_texts)
        to_remove: list[str] = []
        for s_ng in entities:
            ng_len = len(s_ng)
            total_occurrences = 0
            s2t_prefix_counts: Counter = Counter()
            pos = 0
            while True:
                pos = combined_text.find(s_ng, pos)
                if pos == -1:
                    break
                total_occurrences += 1
                if pos > 0:
                    prev_char = combined_text[pos - 1]
                    if prev_char in s2t_keys:
                        s2t_prefix_counts[prev_char] += 1
                pos += ng_len
            if total_occurrences > 0 and s2t_prefix_counts:
                most_common_prefix_count = s2t_prefix_counts.most_common(1)[0][1]
                # 若 ≥80% 的出現都接在同一個 s2t 字元後面 → 尾段詞，移除
                if most_common_prefix_count / total_occurrences >= 0.80:
                    to_remove.append(s_ng)
        for s_ng in to_remove:
            del entities[s_ng]

    return entities


def _inject_entity_guards(tag, entities, pattern: re.Pattern):
    """將 tag 內的保護詞彙用 <span class="notranslate-name"> 包起來。"""
    from bs4 import NavigableString
    import html
    text_nodes = [t for t in tag.find_all(string=True) if isinstance(t, NavigableString)]
    
    for node in text_nodes:
        if any(p.name in SKIP_ANCESTOR for p in node.parents):
            continue
        text = str(node)
        if not text.strip():
            continue
            
        parts = pattern.split(text)
        if len(parts) > 1:
            new_html = ""
            for i, part in enumerate(parts):
                if i % 2 == 1:
                    new_html += f'<span class="notranslate-name">{part}</span>'
                else:
                    new_html += html.escape(part)
            parsed = BeautifulSoup(new_html, "html.parser")
            node.replace_with(parsed)


# ── 主要處理函式 ───────────────────────────────────────────────────────

def process_xhtml(
    content: bytes, translator, postprocessor=None, pairs_collector: list | None = None,
    entities: dict | None = None, entities_pattern: re.Pattern | None = None
) -> bytes:
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

    plain_segs = []   # (tag, text)                      → 純文字批次翻譯
    html_segs  = []   # (tag, inner_html, sentinel_map)  → HTML 格式翻譯

    if entities and entities_pattern:
        for tag in soup.find_all(BLOCK_TAGS):
            if not any(p.name in SKIP_ANCESTOR for p in tag.parents):
                _inject_entity_guards(tag, entities, entities_pattern)

    for tag in soup.find_all(BLOCK_TAGS):
        # 跳過在 script / code / rt 等標籤內的元素
        if any(p.name in SKIP_ANCESTOR for p in tag.parents):
            continue

        if _has_inline(tag):
            if not tag.get_text().strip():
                continue  # 僅含 opaque 元素（如獨立腳注錨點），無需翻譯
            mapping = _apply_opaque_sentinels(tag)
            inner = tag.decode_contents()
            if inner.strip():
                html_segs.append((tag, inner, mapping))
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
        html_texts      = [h for _, h, _ in html_segs]
        translated_html = translator.translate_batch(html_texts, fmt="html")
        for (tag, orig_html, mapping), new_html in zip(html_segs, translated_html):
            new_html   = _restore_opaque_sentinels(new_html, mapping)
            # 逐文字節點套用後處理，避免 pp.apply() 扫描到 HTML tag 字元
            if pp:
                soup_tmp = BeautifulSoup(new_html, "html.parser")
                for node in soup_tmp.find_all(string=True):
                    if node.parent and node.parent.name in SKIP_ANCESTOR:
                        continue
                    corrected = pp.apply(str(node))
                    if corrected != str(node):
                        node.replace_with(corrected)
                final_html = soup_tmp.decode_contents()
            else:
                final_html = new_html
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

    def translate(self, translator, postprocessor=None, verbose: bool = False, report_path: str | None = None, protect_entities: bool = True):
        """翻譯整本 EPUB 的 HTML 文件與 metadata。"""
        docs = [i for i in self.book.get_items()
                if i.get_type() == ebooklib.ITEM_DOCUMENT
                and not isinstance(i, epub.EpubNav)]

        # ── 預掃描：建立全書命名實體保護集合 ───────────────────────────
        entities = {}
        entities_pattern = None
        all_raws = []
        for item in docs:
            try:
                all_raws.append(self._zipfile.read(self._epub_root + item.get_name()))
            except KeyError:
                all_raws.append(item.get_content())
                
        if protect_entities:
            s2t_keys = frozenset(postprocessor.s2t_map.keys()) if postprocessor else frozenset()
            moe_words = postprocessor.moe_words if postprocessor and hasattr(postprocessor, "moe_words") else frozenset()
            min_freq = max(8, len(docs))
            entities = scan_protected_entities(all_raws, s2t_keys, min_freq=min_freq, moe_words=moe_words)
            if entities:
                sorted_entities = sorted(entities.keys(), key=len, reverse=True)
                entities_pattern = re.compile("(" + "|".join(re.escape(e) for e in sorted_entities) + ")")
                tqdm.write(f"  🛡️  命名實體保護：偵測到 {len(entities)} 個固定詞 (min_freq={min_freq})")
                sample = list(entities.keys())[:8]
                tqdm.write(f"       範例：{'、'.join(sample)}")

        # 預算總字數（從 ZIP 中央目錄讀 file_size，不需二次讀檔）
        total_bytes = 0
        for item in docs:
            try:
                total_bytes += self._zipfile.getinfo(self._epub_root + item.get_name()).file_size
            except KeyError:
                total_bytes += len(item.get_content())

        with tqdm(total=total_bytes, unit="字", unit_scale=True, dynamic_ncols=True) as bar:
            for item in docs:
                name = item.get_name()
                if verbose:
                    bar.set_description(name)
                try:
                    zip_path = self._epub_root + item.get_name()
                    try:
                        raw = self._zipfile.read(zip_path)
                    except KeyError:
                        raw = item.get_content()
                    new_content = process_xhtml(raw, translator, postprocessor, self._text_pairs, entities=entities, entities_pattern=entities_pattern)
                    item.set_content(new_content)
                    item.get_content = lambda _bytes=new_content, default=None: _bytes
                    bar.update(len(raw))
                except Exception as e:
                    tqdm.write(f"  ⚠️  跳過 {name}: {e}")

        self._translate_ncx(translator, postprocessor)
        self._translate_toc(translator, postprocessor)
        self._translate_metadata(translator, postprocessor)
        if postprocessor and getattr(postprocessor, "moe_words", None):
            s2t_k = frozenset(postprocessor.s2t_map.keys())
            self._source_guided_repair_pass(
                docs, s2t_k, postprocessor.moe_words, report_path=report_path
            )
        s2t = postprocessor.s2t_map if postprocessor else {}
        self._consistency_pass(docs, s2t_map=s2t, report_path=report_path)

        if report_path and entities:
            try:
                with open(report_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n=== 譯前命名實體保護 ({len(entities)} 詞) ===\n\n")
                    f.write("  ↑ 以下高頻固定詞在送交 Google NMT 前被標籤隔離，成功避免了因斷詞錯誤導致的幻覺\n\n")
                    sorted_entities = sorted(entities.keys(), key=lambda k: (-entities[k], len(k)))
                    for idx, e in enumerate(sorted_entities, 1):
                        f.write(f"[{idx:03d}] {e}  (出現 {entities[e]} 次)\n")
            except Exception as e:
                tqdm.write(f"  ⚠️  保護報告寫入失敗：{e}")

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
        min_total: int = 4,
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

        # 全書 VIP 門檻：至少 15 次，若書本極長則隨書本長度提升 (每 50 個段落提高 1 次出鏡要求)
        entity_thresh = max(15, pair_count // 50)

        # 句子分割：在句號/感嘆/問號/省略號後切開，讓 n-gram 配對在句子層級進行
        # 若來源與目標切出的句數一致，改用句子對；否則退回段落對，避免錯位
        _sent_re = re.compile(r'(?<=[。！？…\n])\s*')

        def _sentence_pairs(src: str, tgt: str):
            ss = [s for s in _sent_re.split(src) if s.strip()]
            ts = [t for t in _sent_re.split(tgt) if t.strip()]
            if len(ss) == len(ts) and len(ss) > 1:
                return zip(ss, ts)
            return [(src, tgt)]

        for src, tgt in self._text_pairs:
            for _src, _tgt in _sentence_pairs(src, tgt):
                # 建立 target n-gram 索引：(長度, 首字) → set(t_ng)
                # 用首字做第一層過濾，把候選從 O(全 n-gram) 降到 O(幾個)
                tgt_idx: dict[tuple[int, str], set[str]] = defaultdict(set)
                for n in range(3, 5):
                    for j in range(len(_tgt) - n + 1):
                        t_ng = _tgt[j:j+n]
                        if cjk.match(t_ng):
                            tgt_idx[(n, t_ng[0])].add(t_ng)

                # 每個 (s_ng, t_ng) 對在本句只計一次
                local_pairs: set[tuple[str, str]] = set()
                for n in range(3, 5):
                    for i in range(len(_src) - n + 1):
                        s_ng = _src[i:i+n]
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
                                # 若 t_ng 與本句另一個原文 s_alt 的差異數 ≤ 與 s_ng 的差異數，表示歧義，放棄歸屬
                                if is_all_fixed and s_ng != t_ng:
                                    my_diff = sum(1 for a, b in zip(s_ng, t_ng) if a != b)
                                    ambiguous = False
                                    for j in range(len(_src) - n + 1):
                                        s_alt = _src[j:j+n]
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

            for t_ng, count in tgt_counts.items():
                if t_ng == majority_tgt:
                    continue

                # 【全域防撞機制】：若此候選本身就是本書其他合法名詞的正解（例如雙胞胎），絕不能抹除它！
                if t_ng in global_legitimate_targets:
                    continue

                # 【來源合法性防護】：若少數派譯文本身是原文常見 n-gram，它是獨立合法詞彙而非幻覺錯字
                # 例：「一如既」在原文有「一如既往」，「政近走」在原文有「政近走出」，不應被多數決覆蓋
                if t_ng in src_to_tgt and sum(src_to_tgt[t_ng].values()) >= min_total:
                    continue

                # min_minority 適用於所有模式（含絕對真理），避免極低頻雜訊觸發修正
                if count < min_minority:
                    continue

                # 絕對真理模式：3-char 全固定詞仍需 VIP 門檻（與 _is_valid_replacement 對稱）
                if is_all_fixed and majority_tgt == s_ng and len(s_ng) == 3 and total < entity_thresh:
                    continue

                # 非絕對真理模式：額外比例與字形驗證
                if not (is_all_fixed and majority_tgt == s_ng):
                    if count / total >= max_minor_ratio:
                        continue
                    if not _is_valid_replacement(s_ng, t_ng, majority_tgt, s2t, is_all_fixed, total, entity_thresh):
                        continue
                    # wrong 和 right 都是原文 n-gram 的合法 s2t 繁體 →
                    # 這是歧義字（如 发→發/髮），語意取決於上下文，交給 postprocessor 負責
                    if all(
                        wc == rc
                        or (wc in s2t.get(sc, ()) and rc in s2t.get(sc, ()))
                        for sc, wc, rc in zip(s_ng, t_ng, majority_tgt)
                    ):
                        continue

                # 兩個模式都抵達此處才寫入（修正：原本在 if 塊內，絕對真理模式的寫入被跳過）
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
            if wa in subfrags:
                continue
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
        HEAD_PAT = re.compile(r'<head(?:\s[^>]*)?(?:/>|>.*?</head>)', re.DOTALL | re.IGNORECASE)

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
                    orig_head = HEAD_PAT.search(content)
                    if orig_head:
                        result = HEAD_PAT.sub(orig_head.group(0), result, count=1)
                    patched = result.encode("utf-8")
                    item.set_content(patched)
                    item.get_content = lambda b=patched, d=None: b
            except Exception:
                continue

        # ── 3b. 對 NCX 套用同一份 normalization（補完目錄頁的一致性）────
        for ncx_item in self.book.get_items():
            if not ncx_item.get_name().lower().endswith(".ncx"):
                continue
            try:
                ncx_bytes = ncx_item.get_content()
                ncx_str = ncx_bytes.decode("utf-8", errors="ignore")
                if not any(wrong in ncx_str for wrong, _ in sorted_norm):
                    continue
                ncx_soup = BeautifulSoup(ncx_bytes, "lxml-xml")
                ncx_changed = False
                for text_node in ncx_soup.find_all("text"):
                    new_text = str(text_node.string or "")
                    original_text = new_text
                    for wrong, right in sorted_norm:
                        if wrong in new_text:
                            new_text = new_text.replace(wrong, right)
                    if new_text != original_text:
                        text_node.string = new_text
                        ncx_changed = True
                if ncx_changed:
                    ncx_item.set_content(str(ncx_soup).encode("utf-8"))
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

    def _source_guided_repair_pass(
        self,
        docs,
        s2t_keys: frozenset,
        moe_words: frozenset,
        report_path: str | None = None,
    ):
        """以原文為基準，修復 Google NMT 截斷的固定字 MOE 詞頭。

        固定字詞（簡繁相同）在翻譯後應原封不動保留。
        當 Google 截斷最後一字（如 老神在在 → 老神在）時，
        此程序會在譯文中偵測前綴，並還原完整詞形。

        假陽性過濾：若前綴 + 下一字本身是 MOE 詞頭（如 不在乎），
        則視為合法的不同譯詞，跳過修復。
        """
        if not self._text_pairs or not moe_words:
            return

        # 固定字 MOE 詞，且長度 >= 4（前綴 >= 3 字）。
        # 2 字詞的前綴只有 1 字，範圍太廣，會產生大量誤觸（如 但 命中所有含「但」的位置）。
        fixed_moe = [
            w for w in moe_words
            if len(w) >= 4 and all(c not in s2t_keys for c in w)
        ]
        if not fixed_moe:
            return

        # 預篩：只保留實際出現在原文中的詞
        all_src = "\n".join(src for src, _ in self._text_pairs)
        candidate_fw = [fw for fw in fixed_moe if fw in all_src]
        if not candidate_fw:
            return

        # 收集全書原文中接在 fw[:-1] 後的所有字元（排除 fw[-1] 本身）。
        # 這些是前綴的「其他合法延伸」—— 即使不在 MOE 中，修復也不應覆蓋。
        fw_src_ext: dict[str, set] = {}
        for fw in candidate_fw:
            prefix, missing = fw[:-1], fw[-1]
            ext: set[str] = set()
            idx = all_src.find(prefix)
            while idx != -1:
                nxt = idx + len(prefix)
                nc = all_src[nxt] if nxt < len(all_src) else ""
                if nc and nc != missing:
                    ext.add(nc)
                idx = all_src.find(prefix, idx + 1)
            fw_src_ext[fw] = ext

        # 收集原文中緊接在完整詞 fw 之後的字元集合。
        # 用於區分「截斷」與「置換」：
        #   截斷：prefix 後直接接 post_char（正常接字）→ 插入 missing
        #   置換：prefix 後接了替代字，post_char 出現在更後面  → 丟掉替代字，補完整詞
        fw_post_chars: dict[str, set] = {}
        for src, tgt in self._text_pairs:
            for fw in candidate_fw:
                idx = src.find(fw)
                while idx != -1:
                    pc_pos = idx + len(fw)
                    pc = src[pc_pos] if pc_pos < len(src) else ""
                    if pc:
                        fw_post_chars.setdefault(fw, set()).add(pc)
                    idx = src.find(fw, idx + 1)

        # 掃描文字對：分別統計置換次數、截斷次數與假陽性次數。
        # 置換（nc 不在 known_post）：明顯字形錯誤，1 次即可修復。
        # 截斷（nc 在 known_post）：可能是合法翻譯簡化，需較高次數才修復。
        # 假陽性（prefix+nc 是 MOE 詞頭）：合法不同譯詞，不修復。
        fw_subst: dict[str, int] = {}   # 置換次數
        fw_trunc: dict[str, int] = {}   # 截斷次數
        fw_false: dict[str, int] = {}   # 假陽性次數
        for src, tgt in self._text_pairs:
            for fw in candidate_fw:
                if fw not in src:
                    continue
                if fw in tgt:
                    continue  # 本段翻譯正確，跳過
                prefix = fw[:-1]
                known_post = fw_post_chars.get(fw, frozenset())
                idx = tgt.find(prefix)
                while idx != -1:
                    nxt = idx + len(prefix)
                    nc = tgt[nxt] if nxt < len(tgt) else ""
                    if nc and (prefix + nc) in moe_words:
                        fw_false[fw] = fw_false.get(fw, 0) + 1
                    elif nc in known_post:
                        fw_trunc[fw] = fw_trunc.get(fw, 0) + 1
                    else:
                        fw_subst[fw] = fw_subst.get(fw, 0) + 1
                    idx = tgt.find(prefix, idx + 1)

        # 門檻判斷：
        # 置換（字被替換）：1 次即修；明顯字形錯誤，不可能是合法翻譯。
        # 截斷（字被刪除）：疊詞 1 次，其他詞 2 次；刪字有時是合法簡化。
        def _is_reduplication(w: str) -> bool:
            return (len(w) >= 4 and w[-2] == w[-1]) or \
                   (len(w) == 4 and w[0] == w[1] and w[2] == w[3])

        repairs: list[tuple[str, str, str]] = []  # (完整詞, 前綴, 遺失字)
        for fw in candidate_fw:
            sc  = fw_subst.get(fw, 0)
            tc  = fw_trunc.get(fw, 0)
            fc  = fw_false.get(fw, 0)
            true_count = sc + tc
            if true_count == 0 or true_count < fc:
                continue
            min_trunc = 1 if _is_reduplication(fw) else 2
            if sc >= 1 or tc >= min_trunc:
                repairs.append((fw, fw[:-1], fw[-1]))

        if not repairs:
            return

        # 為每個修復詞建立正規表達式與閉包 replacer
        def _make_replacer(fw: str, prefix: str, missing: str,
                           moe=moe_words, src_ext=fw_src_ext,
                           post_chars=fw_post_chars):
            other_ext  = src_ext.get(fw, frozenset())
            known_post = post_chars.get(fw, frozenset())
            def replacer(m: re.Match) -> str:
                nc = m.group(1)
                if nc == missing:
                    return m.group(0)          # 已是完整詞，不動
                if nc and (prefix + nc) in moe:
                    return m.group(0)          # 前綴+下一字是合法 MOE 詞，跳過
                if nc in other_ext:
                    return m.group(0)          # 前綴在原文有其他合法用途，跳過
                if nc in known_post:
                    return fw + nc             # 截斷：nc 是 fw 後的正常接字，插入 missing
                return fw                      # 置換：nc 是替代字，丟掉 nc 補完整詞
            return replacer

        compiled: list[tuple[str, re.Pattern, object]] = []
        for fw, prefix, missing in repairs:
            pat = re.compile(re.escape(prefix) + "(.?)", re.DOTALL)
            compiled.append((prefix, pat, _make_replacer(fw, prefix, missing)))

        HEAD_PAT = re.compile(
            r'<head(?:\s[^>]*)?(?:/>|>.*?</head>)', re.DOTALL | re.IGNORECASE
        )

        for item in docs:
            try:
                content_bytes = item.get_content()
                content_str = content_bytes.decode("utf-8", errors="ignore")
                if not any(prefix in content_str for prefix, _, _ in compiled):
                    continue

                try:
                    soup = BeautifulSoup(content_bytes, "lxml-xml")
                except Exception:
                    soup = BeautifulSoup(content_bytes, "html.parser")

                changed = False
                for text_node in soup.find_all(string=True):
                    if text_node.parent and text_node.parent.name in SKIP_ANCESTOR:
                        continue
                    text = str(text_node)
                    new_text = text
                    for prefix, pat, repl in compiled:
                        if prefix in new_text:
                            new_text = pat.sub(repl, new_text)
                    if new_text != text:
                        text_node.replace_with(new_text)
                        changed = True

                if changed:
                    result = str(soup)
                    result = re.sub(r'\bviewbox\b', 'viewBox', result)
                    orig_head = HEAD_PAT.search(content_str)
                    if orig_head:
                        result = HEAD_PAT.sub(orig_head.group(0), result, count=1)
                    patched = result.encode("utf-8")
                    item.set_content(patched)
                    item.get_content = lambda b=patched, d=None: b
            except Exception:
                continue

        if report_path:
            try:
                with open(report_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n=== 截斷詞修復 ({len(repairs)} 個) ===\n")
                    for fw, _, _ in repairs:
                        sc = fw_subst.get(fw, 0)
                        tc = fw_trunc.get(fw, 0)
                        fc = fw_false.get(fw, 0)
                        f.write(
                            f"  {fw[:-1]} → {fw}  "
                            f"(置換:{sc} 截斷:{tc} 假:{fc})\n"
                        )
            except Exception:
                pass

    def save(self, output_path: str):
        epub.write_epub(output_path, self.book, {})
