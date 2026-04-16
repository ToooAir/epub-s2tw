#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate_epub.py — 將簡體中文 EPUB 翻譯為繁體中文

用法：
  python translate_epub.py 小说.epub
  python translate_epub.py ./*.epub -o ./output
  python translate_epub.py ./*.epub --api-key YOUR_KEY
  python translate_epub.py ./*.epub --free --no-rename
  python translate_epub.py ./*.epub --dry-run

注意事項：
  - 預設輸出至 ./output，並用 opencc 將檔名轉為繁體中文
  - 若已存在同名輸出檔則自動跳過（可重跑）
  - 翻譯快取存於 .translate_cache.json（大幅降低重複費用）
  - 免費模式（--free）不需 API Key，但速度較慢且有字元限制
  - API 模式需設定 GOOGLE_API_KEY（可放在 .env 或直接用 -k 傳入）
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

from epub_handler import EpubProcessor
from translator import Translator

load_dotenv()


# ── 檔名轉繁體 ─────────────────────────────────────────────────────────

def to_trad_filename(stem: str) -> str:
    """用系統 opencc 把簡體檔名轉成台灣繁體。若 opencc 未安裝則回傳原名。"""
    try:
        r = subprocess.run(
            ["opencc", "-c", "s2twp.json"],
            input=stem, capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or stem
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return stem


# ── 單檔處理 ───────────────────────────────────────────────────────────

def process_file(
    input_path: Path,
    output_dir: Path,
    translator: Translator,
    rename: bool = True,
    verbose: bool = False,
) -> bool:
    stem     = input_path.stem.replace("_zhTW", "").strip()
    new_stem = to_trad_filename(stem) if rename else stem
    out_path = output_dir / f"{new_stem}.epub"

    if out_path.exists():
        print(f"  ⏭️  已存在，跳過：{out_path.name}")
        return False

    print(f"\n📖 {input_path.name}")
    try:
        proc = EpubProcessor(str(input_path))
        proc.translate(translator, verbose=verbose)
        proc.save(str(out_path))
        print(f"  ✅ → {out_path.name}")
        return True
    except Exception as e:
        print(f"  ❌ 失敗：{e}")
        if verbose:
            import traceback
            traceback.print_exc()
        return False


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="將簡體中文 EPUB 翻譯為繁體中文",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("files", nargs="*", help=".epub 檔案路徑（支援萬用字元）")
    parser.add_argument("-d", "--dir", metavar="DIR",
                        help="遞迴掃描資料夾內所有 .epub（與 files 擇一或合併使用）")
    parser.add_argument("-o", "--output-dir", default="./output", metavar="DIR",
                        help="輸出資料夾（預設：./output）")
    parser.add_argument("-k", "--api-key", default=os.getenv("GOOGLE_API_KEY"),
                        metavar="KEY", help="Google Cloud Translation API Key")
    parser.add_argument("--free", action="store_true",
                        help="使用免費版 Google 翻譯（不需 API Key）")
    parser.add_argument("--no-rename", action="store_true",
                        help="不將輸出檔名轉為繁體")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="顯示每個 XHTML 檔案的進度")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出會處理的檔案，不實際翻譯")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()

    epub_files: list[Path] = []

    # 明確傳入的檔案
    for f in args.files:
        p = Path(f)
        if p.is_file() and p.suffix.lower() == ".epub":
            epub_files.append(p)

    # -d 遞迴掃描，排除 output 目錄
    if args.dir:
        scan_root = Path(args.dir).resolve()
        for p in sorted(scan_root.rglob("*.epub")):
            if output_dir in p.resolve().parents:
                continue  # 排除 output 目錄下的檔案
            epub_files.append(p)

    # 去重（明確傳入 + -d 可能重疊）
    seen, unique = set(), []
    for p in epub_files:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    epub_files = unique

    if not epub_files:
        print("❌ 未找到有效的 .epub 檔案")
        sys.exit(1)

    print(f"📚 找到 {len(epub_files)} 個 EPUB")

    if args.dry_run:
        for f in epub_files:
            print(f"  · {f}")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    use_free   = args.free or not args.api_key
    translator = Translator(
        api_key = None if use_free else args.api_key,
        source  = "zh-CN",
        target  = "zh-TW",
    )

    success = fail = 0
    with tqdm(epub_files, unit="本", dynamic_ncols=True) as book_bar:
        for f in book_bar:
            book_bar.set_description(f.stem[:30])
            ok = process_file(f, output_dir, translator,
                              rename=not args.no_rename, verbose=args.verbose)
            if ok:
                success += 1
            else:
                fail += 1

    translator.close()
    print(f"\n{'='*48}")
    print(f"✅ 成功：{success}　❌ 失敗：{fail}")
    print(f"輸出資料夾：{output_dir.resolve()}")


if __name__ == "__main__":
    main()
