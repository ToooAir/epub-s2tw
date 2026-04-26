#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_rules.py — 後處理規則品質分析腳本（Direction 2）

讀取所有 *_consistency.txt 的「後處理修正紀錄」，計算每條觸發規則的
接縫風險分數，輸出 markdown 報告並建議 layer2_seam_threshold 校準值。

用法：
  python analyze_rules.py                       # 掃描 ./output/*.txt
  python analyze_rules.py -d /path/to/output    # 指定目錄
  python analyze_rules.py -o report.md          # 指定輸出檔
"""

import argparse
import json
import lzma
import re
from collections import Counter, defaultdict
from pathlib import Path


# ── 已知案例（用於校準驗證） ──────────────────────────────────────────
KNOWN_FP_WRONG = frozenset({
    "天後", "被發", "地只", "了望", "布後", "反觀", "鑑於", "大力推薦",
})
KNOWN_TP_WRONG = frozenset({
    "僵屍", "布帘", "匯整", "遊移", "巨細靡遺",
})

# ── 解析常數 ──────────────────────────────────────────────────────────
_RE_RULE   = re.compile(r'^\[(\d+)\]\s+(.+?)\s+→\s+(.+?)\s+\((\d+)\+\s*次\)\s*$')
_RE_SNIP   = re.compile(r'^  …(.+)…\s*$')
_RE_TAG    = re.compile(r'\s+\[(?:ckip↑)?(?:lookbehind|lookahead)[^\]]*\]\s*$')
_APPLIED_HEADER = '=== 後處理修正紀錄'
_BLOCKED_HEADER = '=== Bigram 攔截紀錄'

CJK = re.compile(r'[一-鿿㐀-䶿]')


# ── MOE 載入與頻率表 ──────────────────────────────────────────────────

def load_moe_headwords(path: str = "dict-revised.json.xz") -> frozenset:
    p = Path(path)
    if not p.exists():
        print(f"[warn] MOE 字典不存在：{path}，接縫評分將為 0")
        return frozenset()
    with lzma.open(p, "rt", encoding="utf-8") as f:
        data = json.load(f)
    return frozenset(e["title"] for e in data if "title" in e)


def build_freq_maps(headwords: frozenset) -> tuple[Counter, Counter]:
    """
    suffix_freq[c] = 以 c 結尾的 MOE 詞頭數量
    prefix_freq[c] = 以 c 開頭的 MOE 詞頭數量
    """
    suffix_freq: Counter = Counter()
    prefix_freq: Counter = Counter()
    for hw in headwords:
        if len(hw) >= 2:
            suffix_freq[hw[-1]] += 1
            prefix_freq[hw[0]] += 1
    return suffix_freq, prefix_freq


def gen_seam_score(wrong: str, suffix_freq: Counter, prefix_freq: Counter) -> int:
    """
    生成時接縫評分：
      suffix_freq[wrong[-2]] × prefix_freq[wrong[-1]]
    衡量「wrong 的末二字作為跨詞接縫的頻率」。
    4+ char 規則同樣計算末二字（跨詞風險集中在兩端）。
    """
    if len(wrong) < 2:
        return 0
    return suffix_freq.get(wrong[-2], 0) * prefix_freq.get(wrong[-1], 0)


# ── 解析 consistency 檔案 ─────────────────────────────────────────────

def parse_file(path: Path) -> dict[tuple, list[str]]:
    """
    解析單一 consistency 檔的後處理修正紀錄。
    回傳 {(wrong, right): [stripped_snippet, ...]}
    """
    results: dict[tuple, list[str]] = defaultdict(list)
    in_applied = False
    current_key = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()

        # 段落偵測
        if line.startswith("==="):
            if _APPLIED_HEADER in line:
                in_applied = True
                current_key = None
            else:
                in_applied = False
            continue

        if not in_applied:
            continue

        # 規則標頭 [NNN] wrong → right  (N+ 次)
        m = _RE_RULE.match(line.strip())
        if m:
            current_key = (m.group(2).strip(), m.group(3).strip())
            continue

        # Snippet 行  …...…
        if current_key is None:
            continue
        m = _RE_SNIP.match(line)
        if m:
            raw = _RE_TAG.sub("", m.group(1)).strip()
            results[current_key].append(raw)

    return dict(results)


def parse_all(logs_dir: Path) -> dict[tuple, dict]:
    """
    掃描 logs_dir 下所有 *_consistency.txt。
    回傳 {(wrong, right): {"n_files": int, "snippets": [str]}}
    """
    agg: dict[tuple, dict] = {}
    files = sorted(logs_dir.glob("*_consistency.txt"))
    for f in files:
        for key, snips in parse_file(f).items():
            if key not in agg:
                agg[key] = {"n_files": 0, "snippets": []}
            agg[key]["n_files"] += 1
            agg[key]["snippets"].extend(snips)
    return agg


# ── 從 snippet 提取左右鄰字 ──────────────────────────────────────────

def find_central(text: str, pattern: str) -> int:
    """找最靠近 snippet 中心的 pattern 出現位置。"""
    center = len(text) // 2
    best, best_d = -1, float("inf")
    pos = 0
    while True:
        pos = text.find(pattern, pos)
        if pos < 0:
            break
        d = abs(pos + len(pattern) // 2 - center)
        if d < best_d:
            best_d, best = d, pos
        pos += 1
    return best


def context_chars(snippet: str, wrong: str) -> tuple[str | None, str | None]:
    """回傳 wrong 在 snippet 中的左鄰字和右鄰字（限漢字，否則 None）。"""
    pos = find_central(snippet, wrong)
    if pos < 0:
        return None, None
    lc = snippet[pos - 1] if pos > 0 else None
    rc = snippet[pos + len(wrong)] if pos + len(wrong) < len(snippet) else None
    if lc and not CJK.match(lc):
        lc = None
    if rc and not CJK.match(rc):
        rc = None
    return lc, rc


# ── 每條規則指標計算 ─────────────────────────────────────────────────

def rule_metrics(
    wrong: str,
    right: str,
    snippets: list[str],
    n_files: int,
    suffix_freq: Counter,
    prefix_freq: Counter,
) -> dict:
    ctx_scores = []
    left_chars: Counter = Counter()
    right_chars: Counter = Counter()

    for snip in snippets:
        lc, rc = context_chars(snip, wrong)
        left_chars[lc] += 1
        right_chars[rc] += 1
        s_left  = suffix_freq.get(lc, 0) if lc else 0
        s_right = prefix_freq.get(rc, 0) if rc else 0
        ctx_scores.append(s_left + s_right)

    avg_ctx = sum(ctx_scores) / len(ctx_scores) if ctx_scores else 0
    max_ctx = max(ctx_scores) if ctx_scores else 0
    cross_rate = sum(1 for s in ctx_scores if s > 50) / len(ctx_scores) if ctx_scores else 0

    return {
        "wrong":       wrong,
        "right":       right,
        "n_files":     n_files,
        "n_snippets":  len(snippets),
        "gen_seam":    gen_seam_score(wrong, suffix_freq, prefix_freq),
        "avg_ctx":     avg_ctx,
        "max_ctx":     max_ctx,
        "cross_rate":  cross_rate,
        "left_chars":  left_chars.most_common(3),
        "right_chars": right_chars.most_common(3),
        "sample_snips": snippets[:2],
        "label": (
            "【已知FP】" if wrong in KNOWN_FP_WRONG else
            "【已知TP】" if wrong in KNOWN_TP_WRONG else
            ""
        ),
    }


# ── 報告生成 ─────────────────────────────────────────────────────────

def write_report(
    metrics_list: list[dict],
    suffix_freq: Counter,
    prefix_freq: Counter,
    n_log_files: int,
    out_path: Path,
) -> None:

    # 已知 FP / TP 的 gen_seam 分布（用於校準）
    fp_scores = [m["gen_seam"] for m in metrics_list if m["wrong"] in KNOWN_FP_WRONG]
    tp_scores = [m["gen_seam"] for m in metrics_list if m["wrong"] in KNOWN_TP_WRONG]
    theta_suggest = (
        int(min(fp_scores) * 0.8) if fp_scores else "N/A（無已知FP出現在本次數據中）"
    )

    lines = []

    # ── 標頭 ──
    lines += [
        "# 後處理規則品質分析報告（Direction 2）\n\n",
        f"- 分析 consistency 檔：{n_log_files} 本\n",
        f"- 唯一觸發規則：{len(metrics_list)} 條\n",
        f"- 已知 FP 規則出現：{len(fp_scores)} 條 / {len(KNOWN_FP_WRONG)} 條\n",
        f"- 已知 TP 規則出現：{len(tp_scores)} 條 / {len(KNOWN_TP_WRONG)} 條\n\n",
    ]

    # ── 校準建議 ──
    lines.append("## θ 校準建議\n\n")
    if fp_scores and tp_scores:
        lines += [
            f"| 類別 | gen_seam 分數 |\n",
            f"|------|---------------|\n",
        ]
        for m in sorted(metrics_list, key=lambda x: -x["gen_seam"]):
            if m["wrong"] in KNOWN_FP_WRONG or m["wrong"] in KNOWN_TP_WRONG:
                lines.append(f"| {m['label']} `{m['wrong']}→{m['right']}` | {m['gen_seam']:,} |\n")
        lines += [
            f"\n",
            f"- 已知 FP gen_seam 最小值：**{min(fp_scores):,}**\n",
            f"- 已知 TP gen_seam 最大值：**{max(tp_scores) if tp_scores else 'N/A':,}**\n",
            f"- **建議 θ = {theta_suggest}**（FP 最小值 × 0.8，保守設定）\n\n",
        ]
    else:
        lines.append(f"資料不足，暫無法自動校準。θ 建議值：{theta_suggest}\n\n")

    # ── 主排序表 ──
    lines.append("## 規則品質排序（按 gen_seam 降序）\n\n")
    lines.append("| 標記 | wrong → right | 書數 | 片段數 | gen_seam | avg_ctx | cross_rate |\n")
    lines.append("|------|---------------|------|--------|----------|---------|------------|\n")

    for m in sorted(metrics_list, key=lambda x: -x["gen_seam"]):
        label = m["label"] or "　　　　"
        lines.append(
            f"| {label} | `{m['wrong']}→{m['right']}` "
            f"| {m['n_files']} | {m['n_snippets']} "
            f"| {m['gen_seam']:,} | {m['avg_ctx']:.0f} | {m['cross_rate']:.0%} |\n"
        )

    # ── 詳細記錄 ──
    lines.append("\n## 詳細規則記錄\n\n")
    for m in sorted(metrics_list, key=lambda x: -x["gen_seam"]):
        label = f" {m['label']}" if m["label"] else ""
        lines.append(f"### `{m['wrong']} → {m['right']}`{label}\n\n")
        lines.append(f"- 出現書數：{m['n_files']}，片段數：{m['n_snippets']}\n")
        lines.append(f"- gen_seam：{m['gen_seam']:,}\n")
        lines.append(f"- avg_ctx：{m['avg_ctx']:.1f}，max_ctx：{m['max_ctx']:.0f}，cross_rate：{m['cross_rate']:.0%}\n")
        if m["left_chars"]:
            lc_str = "，".join(f"`{c}`×{n}" for c, n in m["left_chars"] if c)
            lines.append(f"- 左鄰字（高頻）：{lc_str or '—'}\n")
        if m["right_chars"]:
            rc_str = "，".join(f"`{c}`×{n}" for c, n in m["right_chars"] if c)
            lines.append(f"- 右鄰字（高頻）：{rc_str or '—'}\n")
        for snip in m["sample_snips"]:
            lines.append(f"  > …{snip}…\n")
        lines.append("\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    print(f"✅ 報告已寫入：{out_path}")


# ── 主流程 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="後處理規則品質分析（Direction 2）")
    parser.add_argument("-d", "--dir",    default="./output", help="consistency 檔所在目錄")
    parser.add_argument("-m", "--moe",    default="dict-revised.json.xz", help="MOE 字典路徑")
    parser.add_argument("-o", "--output", default="rule_analysis.md", help="輸出報告檔名")
    args = parser.parse_args()

    logs_dir = Path(args.dir)
    if not logs_dir.exists():
        print(f"❌ 找不到目錄：{logs_dir}")
        return

    print("載入 MOE 字典…")
    headwords = load_moe_headwords(args.moe)
    suffix_freq, prefix_freq = build_freq_maps(headwords)
    print(f"  詞頭數：{len(headwords):,}，suffix 唯一字：{len(suffix_freq):,}，prefix 唯一字：{len(prefix_freq):,}")

    print("解析 consistency 檔案…")
    all_rules = parse_all(logs_dir)
    print(f"  發現 {len(all_rules)} 條唯一規則（共 {sum(d['n_files'] for d in all_rules.values())} 次書本觸發）")

    print("計算指標…")
    metrics_list = [
        rule_metrics(w, r, d["snippets"], d["n_files"], suffix_freq, prefix_freq)
        for (w, r), d in all_rules.items()
    ]

    out_path = Path(args.output)
    write_report(metrics_list, suffix_freq, prefix_freq, len(list(logs_dir.glob("*_consistency.txt"))), out_path)

    # 終端摘要
    print("\n── 校準摘要 ──────────────────────────────────────")
    for m in sorted(metrics_list, key=lambda x: -x["gen_seam"])[:20]:
        label = m["label"] or "      "
        print(f"  {label}  {m['wrong']:<10} → {m['right']:<10}  gen_seam={m['gen_seam']:>8,}  "
              f"書={m['n_files']}  ctx={m['avg_ctx']:.0f}  cross={m['cross_rate']:.0%}")


if __name__ == "__main__":
    main()
