#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速測試：直接送 Google vs STPhrases 前處理後再送 Google"""

from translator import Translator

TEST = "我指着正中央盘子里各处叠了好几层的半固体鸡蛋料理发问，有希眨了眨眼，以若无其事的表情回答："


def load_stphrases(path="STPhrases.txt") -> dict:
    """讀取 STPhrases，回傳 {simplified: traditional}，長詞優先（之後排序用）"""
    pairs = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "\t" not in line:
                continue
            s, t = line.rstrip("\n").split("\t", 1)
            s, t = s.strip(), t.strip().split()[0]
            if s and t and s != t:
                pairs[s] = t
    return pairs


def preprocess(text: str, phrases: dict) -> str:
    """最長匹配優先，將簡體詞組替換為繁體。"""
    sorted_keys = sorted(phrases, key=len, reverse=True)
    result = list(text)
    i = 0
    while i < len(result):
        for key in sorted_keys:
            end = i + len(key)
            segment = "".join(result[i:end])
            if segment == key:
                result[i:end] = list(phrases[key])
                i += len(phrases[key])
                break
        else:
            i += 1
    return "".join(result)


def main():
    print("載入 STPhrases...")
    phrases = load_stphrases()
    print(f"共 {len(phrases):,} 條詞組\n")

    preprocessed = preprocess(TEST, phrases)
    print(f"原文：\n  {TEST}\n")
    print(f"STPhrases 前處理後：\n  {preprocessed}\n")

    t = Translator()

    result_a = t.translate(TEST)
    print(f"方法 A（直接送 Google）：\n  {result_a}\n")

    result_b = t.translate(preprocessed)
    print(f"方法 B（前處理後送 Google）：\n  {result_b}\n")

    t.close()


if __name__ == "__main__":
    main()
