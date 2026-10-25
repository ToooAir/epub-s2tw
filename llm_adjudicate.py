#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_adjudicate.py — 以 Claude 裁決 繁化姬 vs OpenCC 分歧（評估用；主流程走 llm_review.py）

讀 compare_converters.py 產出的 divergences.json，分批送 Claude 判斷每種分歧
哪一方正確，結果快取於 <eval>/llm_<model>.json，並與 labels.json 比對準確率。

需求：pip install anthropic；憑證放 ANTHROPIC_API_KEY（可寫在 .env）。

用法：
  python llm_adjudicate.py --dry-run              # 只印 prompt 與 token 估算，不呼叫 API
  python llm_adjudicate.py                        # 呼叫 API（預設 claude-opus-5-5）並評分
  python llm_adjudicate.py --model claude-haiku-5-5
  python llm_adjudicate.py --backend cli --model claude-haiku-5-5   # 走 claude -p，免 API key
  python llm_adjudicate.py --score-only           # 只用既有快取重新評分
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from llm_review import ADJ_BATCH as BATCH, ADJUDICATE_SYSTEM as SYSTEM, build_adjudicate_prompt as build_prompt
from llm_review import adjudicate as adjudicate_cli


def adjudicate(rows: list[dict], model: str, effort: str) -> dict[int, dict]:
    import anthropic
    from dotenv import load_dotenv
    from pydantic import BaseModel

    class Verdict(BaseModel):
        id: int
        verdict: Literal["zh", "cc", "both", "neither", "mixed"]
        wrong: str
        correct: str
        reason: str

    class Verdicts(BaseModel):
        items: list[Verdict]

    load_dotenv()
    client = anthropic.Anthropic()
    out: dict[int, dict] = {}
    usage = Counter()
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        resp = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": effort},
            messages=[{"role": "user", "content": build_prompt(chunk)}],
            output_format=Verdicts,
        )
        if resp.stop_reason != "end_turn" or resp.parsed_output is None:
            raise RuntimeError(f"batch {i}: stop_reason={resp.stop_reason}")
        for v in resp.parsed_output.items:
            out[v.id] = v.model_dump()
        usage["in"] += resp.usage.input_tokens
        usage["out"] += resp.usage.output_tokens
        missing = {r["id"] for r in chunk} - set(out)
        print(f"  batch {i // BATCH + 1}: {len(chunk)} 筆" + (f"，缺 {sorted(missing)}" if missing else ""))
    print(f"token：input {usage['in']:,} / output {usage['out']:,}")
    return out


# labels.json：Z/C/B/N/M ↔ verdict
LABEL_OF = {"zh": "Z", "cc": "C", "both": "B", "neither": "N", "mixed": "M"}


def score(rows: list[dict], verdicts: dict[int, dict], labels: dict[str, list]) -> None:
    cm, cm_inst = Counter(), Counter()
    harmful, missed = [], []
    for r in rows:
        gold = labels[str(r["id"])][0]
        v = verdicts.get(r["id"])
        pred = LABEL_OF[v["verdict"]] if v else "?"
        cm[(gold, pred)] += 1
        cm_inst[(gold, pred)] += r["count"]
        # 會改壞：繁化姬本來對/皆可，LLM 卻要改成 OpenCC 或別的寫法
        if gold in "ZB" and pred in "CN":
            harmful.append((r, v))
        # 漏抓：繁化姬錯，LLM 卻保留繁化姬
        if gold in "CNM" and pred in "ZB":
            missed.append((r, v))

    cats = "ZBCNM?"
    print("\n混淆矩陣（列=標註, 欄=LLM；種類數 / 次數）")
    print("     " + "".join(f"{c:>12}" for c in cats))
    for g in "ZBCNM":
        print(f"  {g}  " + "".join(f"{cm[(g, p)]:>5} /{cm_inst[(g, p)]:>5}" for p in cats))

    total = sum(cm.values())
    agree = sum(cm[(c, c)] for c in "ZBCNM")
    # 寬鬆一致：Z 與 B 都代表「保留繁化姬」，實務上等價
    keep = set("ZB")
    lenient = sum(n for (g, p), n in cm.items() if g == p or (g in keep and p in keep))
    errs = sum(n for (g, _), n in cm.items() if g in "CNM")
    caught = sum(n for (g, p), n in cm.items() if g in "CNM" and p not in keep and p != "?")
    print(f"\n嚴格一致 {agree}/{total}　寬鬆一致（Z≈B）{lenient}/{total}")
    print(f"繁化姬錯誤抓到 {caught}/{errs}　會改壞 {len(harmful)} 種（{sum(r['count'] for r, _ in harmful)} 次）")
    for title, items in (("會改壞", harmful), ("漏抓", missed)):
        if items:
            print(f"\n{title}：")
            for r, v in items:
                print(f"  {r['id']} {r['zh']}|{r['cc']} 標註={labels[str(r['id'])]} LLM={v['verdict'] if v else '?'} "
                      f"{(v or {}).get('reason', '')[:60]}  例：{r['examples'][0]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval", type=Path, default=Path("eval"))
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--backend", default="api", choices=["api", "cli"], help="cli = claude -p（訂閱額度）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()

    rows = json.loads((args.eval / "divergences.json").read_text(encoding="utf-8"))["rows"]
    labels = json.loads((args.eval / "labels.json").read_text(encoding="utf-8"))
    cache = args.eval / f"llm_{args.model}_{args.effort}.json"

    if args.dry_run:
        prompt = build_prompt(rows[:BATCH])
        print(SYSTEM, "\n---\n", prompt[:1500], "…")
        chars = len(SYSTEM) * ((len(rows) + BATCH - 1) // BATCH) + len(build_prompt(rows))
        print(f"\n{len(rows)} 筆 / {(len(rows) + BATCH - 1) // BATCH} 批，輸入約 {chars:,} 字（中文約 1–1.5 token/字）")
        return

    if args.score_only:
        verdicts = {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    else:
        if args.backend == "cli":
            verdicts, cost = adjudicate_cli(rows, args.model, args.effort)
            print(f"等值費用約 ${cost:.3f}（訂閱方案不實際計費）")
        else:
            verdicts = adjudicate(rows, args.model, args.effort)
        cache.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1), encoding="utf-8")
    score(rows, verdicts, labels)


if __name__ == "__main__":
    main()
