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

from bs4 import BeautifulSoup
import ebooklib
from ebooklib import epub
from tqdm import tqdm

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


# ── 主要處理函式 ───────────────────────────────────────────────────────

def process_xhtml(content: bytes, translator) -> bytes:
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

    # 純文字批次翻譯
    if plain_segs:
        texts      = [t for _, t in plain_segs]
        translated = translator.translate_batch(texts, fmt="text")
        for (tag, _), new_text in zip(plain_segs, translated):
            tag.string = new_text

    # HTML 格式翻譯（含 inline 標籤）
    if html_segs:
        html_texts      = [h for _, h in html_segs]
        translated_html = translator.translate_batch(html_texts, fmt="html")
        for (tag, _), new_html in zip(html_segs, translated_html):
            _replace_inner_html(tag, new_html)

    return str(soup).encode("utf-8")


# ── EpubProcessor ──────────────────────────────────────────────────────

class EpubProcessor:

    def __init__(self, path: str):
        self.path = path
        self.book = epub.read_epub(path, {"ignore_ncx": False})

    def translate(self, translator, verbose: bool = False):
        """翻譯整本 EPUB 的 HTML 文件與 metadata。"""
        docs = [i for i in self.book.get_items()
                if i.get_type() in (ebooklib.ITEM_DOCUMENT, ebooklib.ITEM_NAVIGATION)]

        with tqdm(docs, unit="xhtml", dynamic_ncols=True) as bar:
            for item in bar:
                name = item.get_name()
                if verbose:
                    bar.set_description(name)
                try:
                    new_content = process_xhtml(item.get_content(), translator)
                    item.set_content(new_content)
                except Exception as e:
                    tqdm.write(f"  ⚠️  跳過 {name}: {e}")

        self._translate_ncx(translator)
        self._translate_toc(translator)
        self._translate_metadata(translator)

    def _translate_ncx(self, translator):
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
                    tag.string = new_text
                item.set_content(str(soup).encode("utf-8"))
            except Exception as e:
                tqdm.write(f"  ⚠️  跳過 NCX: {e}")

    def _translate_toc(self, translator):
        """翻譯 book.toc（ebooklib 用來生成 nav.xhtml 的來源）。"""
        from ebooklib.epub import Link, Section

        def translate_entries(entries):
            for entry in entries:
                if isinstance(entry, Link):
                    entry.title = translator.translate(entry.title)
                elif isinstance(entry, Section):
                    entry.title = translator.translate(entry.title)
                    if entry.children:
                        translate_entries(entry.children)
                elif isinstance(entry, tuple) and len(entry) == 2:
                    # (Section, [children]) 形式
                    translate_entries([entry[0]])
                    translate_entries(entry[1])

        try:
            translate_entries(self.book.toc)
        except Exception as e:
            tqdm.write(f"  ⚠️  翻譯 TOC 失敗: {e}")

    def _translate_metadata(self, translator):
        DC = "http://purl.org/dc/elements/1.1/"
        for field in META_FIELDS:
            try:
                items = self.book.get_metadata("DC", field)
                if not items:
                    continue
                self.book.metadata[DC][field] = [
                    (translator.translate(value) if value else value, attrs)
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
                    (translator.translate(v) if attrs.get("property") in TRANSLATE_PROPS and v else v, attrs)
                    for v, attrs in self.book.metadata[OPF][None]
                ]
        except Exception as e:
            tqdm.write(f"  ⚠️  OPF meta: {e}")

    def save(self, output_path: str):
        epub.write_epub(output_path, self.book, {})
