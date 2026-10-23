#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_converters.py — 繁化姬 vs OpenCC s2twp 分歧點萃取（評估集建立用）

對每本簡體 EPUB 抽出段落，分別以繁化姬（zhconvert.org）與系統 opencc（s2twp）
轉換，逐段 diff，彙整 (簡體原文, 繁化姬, OpenCC) 分歧三元組與例句。
分歧點即高風險位置，供人工/LLM 標註哪一方正確。

繁化姬結果快取於 <out>/zh_cache/，重跑不重打 API。

用法：
  python compare_converters.py 書1.epub 書2.epub -o eval
  → eval/divergences.json、eval/divergences.md
"""

import argparse
import json
import zipfile
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

from epub_handler import BLOCK_TAGS, SKIP_ANCESTOR
from llm_review import diverge, opencc_convert
from zhconvert_translator import ZhConvertTranslator



def extract_paragraphs(epub_path: Path) -> list[str]:
    paras = []
    with zipfile.ZipFile(epub_path) as z:
        for name in sorted(n for n in z.namelist() if n.endswith((".xhtml", ".html", ".htm"))):
            soup = BeautifulSoup(z.read(name), "lxml")
            for tag in soup.find_all(SKIP_ANCESTOR):
                tag.decompose()
            for tag in soup.find_all(BLOCK_TAGS):
                if tag.find(BLOCK_TAGS):  # 只取最內層 block，避免重複
                    continue
                text = " ".join(tag.get_text().split())
                if text:
                    paras.append(text)
    return paras


def zhconvert_cached(paras: list[str], cache: Path, zh: ZhConvertTranslator) -> list[str]:
    if cache.exists():
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data["src"] == paras:
            return data["out"]
    out = zh.translate_batch(paras)
    if not any(o.startswith("[ERROR") for o in out):
        cache.write_text(json.dumps({"src": paras, "out": out}, ensure_ascii=False), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("epubs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("eval"))
    args = ap.parse_args()
    (args.out / "zh_cache").mkdir(parents=True, exist_ok=True)

    zh = ZhConvertTranslator()
    groups: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "books": set(), "examples": []})
    total_paras = total_chars = 0
    for epub in args.epubs:
        paras = extract_paragraphs(epub)
        zh_out = zhconvert_cached(paras, args.out / "zh_cache" / f"{epub.stem}.json", zh)
        cc_out = opencc_convert(paras)
        total_paras += len(paras)
        total_chars += sum(map(len, paras))
        for src, a, b in zip(paras, zh_out, cc_out):
            if a == b or a.startswith("[ERROR"):
                continue
            for s, za, cb, ctx in diverge(src, a, b):
                g = groups[(s, za, cb)]
                g["count"] += 1
                g["books"].add(epub.stem)
                if len(g["examples"]) < 3:
                    g["examples"].append(ctx)
        print(f"✓ {epub.name}：{len(paras)} 段")
    zh.close()

    rows = sorted(
        ({"src": s, "zh": a, "cc": b, "count": g["count"], "books": len(g["books"]), "examples": g["examples"]}
         for (s, a, b), g in groups.items()),
        key=lambda r: -r["count"],
    )
    for i, r in enumerate(rows, 1):
        r["id"] = i
    meta = {"books": [e.stem for e in args.epubs], "paragraphs": total_paras, "chars": total_chars,
            "unique": len(rows), "instances": sum(r["count"] for r in rows)}
    (args.out / "divergences.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# 繁化姬 vs OpenCC s2twp 分歧\n\n{meta}\n\n| id | 原文 | 繁化姬 | OpenCC | 次數 | 例句 |\n|---|---|---|---|---|---|\n"]
    lines += [f"| {r['id']} | {r['src']} | {r['zh']} | {r['cc']} | {r['count']} | {r['examples'][0]} |\n" for r in rows]
    (args.out / "divergences.md").write_text("".join(lines), encoding="utf-8")
    print(f"分歧：{meta['unique']} 種 / {meta['instances']} 次（{total_chars:,} 字）→ {args.out}/")


if __name__ == "__main__":
    main()
