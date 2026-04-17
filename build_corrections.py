#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_corrections.py
讀取 STPhrases.txt，把簡體詞組送進 Google 免費 API，
比對期望繁體結果，輸出 corrections.json（Google 翻錯的詞組）。

用法：
    python build_corrections.py [--limit N] [--output corrections.json]
"""

import json
import argparse
import sys
import time
from pathlib import Path
from tqdm import tqdm

from translator import Translator


def load_phrases(path: str) -> list[tuple[str, str]]:
    """讀取 STPhrases.txt，回傳 [(simplified, expected_traditional), ...]。
    - 過濾掉簡繁完全相同的條目
    - 多個繁體選項時取第一個
    """
    pairs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" not in line:
                continue
            simplified, traditional = line.split("\t", 1)
            simplified = simplified.strip()
            traditional = traditional.strip().split()[0]  # 取第一個選項
            if simplified and traditional and simplified != traditional:
                pairs.append((simplified, traditional))
    return pairs


def build_corrections(
    phrases_path: str,
    output_path: str,
    limit: int = 0,
    batch_size: int = 80,
) -> None:
    pairs = load_phrases(phrases_path)
    print(f"總詞組：{len(pairs):,}（已過濾簡繁相同）")

    if limit:
        pairs = pairs[:limit]
        print(f"限制前 {limit} 條")

    # 載入既有進度（支援中斷續跑）
    out = Path(output_path)
    corrections: dict = {}
    tested: set = set()
    if out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
        corrections = data.get("corrections", {})
        tested = set(data.get("tested", []))
        print(f"載入既有進度：已測 {len(tested):,} 條，已知修正 {len(corrections):,} 條")

    translator = Translator()  # 免費模式

    # 過濾已測過的
    pending = [(s, t) for s, t in pairs if s not in tested]
    print(f"待測：{len(pending):,} 條")

    if not pending:
        print("無待測條目，結束。")
        translator.close()
        return

    new_corrections = 0
    try:
        for i in tqdm(range(0, len(pending), batch_size), desc="比對中"):
            batch = pending[i : i + batch_size]
            simplified_batch = [s for s, _ in batch]
            expected_batch   = [t for _, t in batch]

            translated_batch = translator.translate_batch(simplified_batch)

            for simplified, expected, got in zip(simplified_batch, expected_batch, translated_batch):
                tested.add(simplified)
                got = got.strip()
                if got != expected:
                    corrections[got] = expected
                    new_corrections += 1

            # 每批儲存一次進度
            _save(out, corrections, tested)
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n中斷，已儲存進度。")
    finally:
        _save(out, corrections, tested)
        translator.close()

    print(f"\n完成：新增修正 {new_corrections} 條，累計修正 {len(corrections)} 條")
    print(f"結果儲存至 {output_path}")


def _save(path: Path, corrections: dict, tested: set) -> None:
    path.write_text(
        json.dumps(
            {"corrections": corrections, "tested": sorted(tested)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="建立 Google 翻譯修正字典")
    parser.add_argument("--phrases", default="STPhrases.txt", help="詞組來源檔")
    parser.add_argument("--output",  default="corrections.json", help="輸出修正檔")
    parser.add_argument("--limit",   type=int, default=0, help="限制測試筆數（0=全部）")
    parser.add_argument("--batch",   type=int, default=80, help="每批筆數")
    args = parser.parse_args()

    build_corrections(args.phrases, args.output, args.limit, args.batch)
