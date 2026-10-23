#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_review.py — 以 Claude（claude -p，用訂閱額度）校對繁化姬輸出

兩個來源：
  1. 分歧裁決：繁化姬 vs 系統 opencc（s2twp）不一致處，交給 Claude 判斷誰對。
     抓得到斷詞錯誤、殘留簡體（樹乾髮出、担任）；系統沒有 opencc 時略過。
  2. 名字檢查：含「一簡對多繁、且本書實際出現多種轉法」字的高頻 n-gram，
     抓兩個轉換器都錯的專名（乌里朵拉→烏裡朵拉）與前後不一致（春斗/春鬥）。

規則只在通過 s2t 合法字檢查、且不會改到對齊範圍以外時才自動套用；
其餘列為待審。評估流程見 compare_converters.py / llm_adjudicate.py。
"""

import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

MODEL = "claude-opus-5-5"
EFFORT = "medium"
ADJ_BATCH = 40
NAME_BATCH = 150
DIFF_CTX = 12   # 分歧例句左右字數
NAME_CTX = 8    # 名字例句左右字數
RULE_K = 2      # 分歧規則的上下文字數
CJK = re.compile(r"^[一-鿿]+$")
CTX_RE = re.compile(r"^(.*)【(.*)\|(.*)】(.*)$", re.DOTALL)

ADJUDICATE_SYSTEM = """你是簡體中文→台灣繁體中文的校對員。每筆資料是同一段簡體原文經兩個轉換器（繁化姬=zh、OpenCC=cc）轉換後不一致的地方。
例句格式：左文【zh的寫法|cc的寫法】右文（空字串表示該轉換器在此沒有字）。

判斷準則：
1. 目標是台灣繁體：以教育部國語辭典為準，大陸用語轉成台灣慣用語是正確的（如 芝士→起司、米→公尺、蛋黃醬→美乃滋）。
2. 必須忠實原文：改變原意、錯字、斷詞錯誤（如把「樹幹發出」轉成「樹乾髮出」）都是錯的。
3. 人名、音譯名、日文名：取音用字保留原字，不套用一般詞義（如 乌里朵拉→烏里朵拉，不是烏裡朵拉）。但已有台灣通行譯名的地名、譯名，以台灣通行寫法為準（如 格林兰→格陵蘭）。
4. 純日文文字（標題等）保持日文字形；中文句子即使夾帶假名，也必須轉成繁體。
5. 使用者偏好：殭屍（不用僵屍）。
6. 在地化改寫視為正確：把詞換成意思相同的台灣慣用說法（如 甭→不用、玩意儿→東西、有劳→麻煩、胡萝卜→紅蘿蔔）不算違反忠實原文。
   只有 zh 有明確錯誤（錯字、斷詞錯誤、殘留簡體、人名誤轉、意思改變）才選 cc 或 neither；純用詞風格差異選 zh 或 both。
7. 兩種寫法都是合法台灣繁體（異體字、同義用語，如 濕/溼、遺跡/遺蹟）→ both。不確定也選 both。

verdict：zh=只有繁化姬對；cc=只有OpenCC對（cc 的寫法要完全正確）；both=兩者皆可；mixed=不同例句答案不同；
neither=兩者皆錯，或 zh 錯但 cc 也帶大陸用語（如 zh「融合併最佳化」、cc「融合並優化」→ 正確為「並最佳化」）。
correct：neither 時填【】內位置的正確寫法（可含前後幾字，必須是例句中連續出現的文字改正後的樣子）；其他情況留空。"""

NAME_SYSTEM = """你是簡體中文→台灣繁體中文的校對員。以下是一本小說經轉換器轉換後的高頻詞或可能的專名，
格式：id 簡體原文 → 繁體轉換結果(次數)，有多種轉法時全部列出；例句中【】標出該詞。

找出轉換有誤的項目：
1. 人名、音譯名、日文名：取音用字保留原字，不套用一般詞義（乌里朵拉→烏里朵拉，不是烏裡朵拉；春斗→春斗，不是春鬥）。
2. 同一詞出現多種轉法時，判斷哪個正確，其他寫法視為錯誤。
3. 斷詞錯誤造成的錯字（如 树干发出→樹乾髮出，應為樹幹發出）。
4. 已有台灣通行譯名的地名、譯名以台灣寫法為準。異體字、兩可寫法（如 濕/溼）不算錯。
5. 跨詞邊界的片段（如「了头发」）只看其中的字是否轉對。

只回傳有誤的項目；全部正確就回傳空陣列。correct 填正確的繁體寫法（與原文等長）。"""

VERDICTS_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "verdict": {"enum": ["zh", "cc", "both", "neither", "mixed"]},
            "correct": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["id", "verdict", "correct", "reason"],
    }}},
    "required": ["items"],
}

NAMES_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "correct": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["id", "correct", "reason"],
    }}},
    "required": ["items"],
}


# ── Claude CLI ──────────────────────────────────────────────────────────

def claude_cli(system: str, prompt: str, schema: dict, model: str = MODEL, effort: str = EFFORT) -> tuple[dict, float]:
    """走 `claude -p`（用 Claude 訂閱額度，免 API key），回傳 (structured_output, 等值費用)。
    在空的暫存目錄執行、關閉所有工具與設定來源，模型讀不到專案檔案（如 labels.json）。"""
    with tempfile.TemporaryDirectory() as iso:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--effort", effort, "--tools", "",
             "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
             "--system-prompt", system, "--output-format", "json", "--json-schema", json.dumps(schema)],
            input=prompt, capture_output=True, text=True, cwd=iso, check=True,
        )
    res = json.loads(proc.stdout)
    if res.get("is_error") or not res.get("structured_output"):
        raise RuntimeError(f"{res.get('subtype')} {res.get('result', '')[:200]}")
    return res["structured_output"], res.get("total_cost_usd", 0.0)


# ── 字表 ────────────────────────────────────────────────────────────────

def load_s2t(path: str = "STCharacters.txt", tw_path: str = "TWVariants.txt") -> dict[str, set]:
    """簡體字 → 合法繁體字集合，含台灣異體（裏→裡）。"""
    tw: dict[str, str] = {}
    for line in Path(tw_path).read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and not line.startswith("#"):
            tw[parts[0]] = parts[1].split()[0]
    s2t: dict[str, set] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            targets = set(parts[1].split())
            s2t[parts[0]] = targets | {tw[t] for t in targets if t in tw}
    return s2t


def legal(src: str, t: str, s2t: dict[str, set]) -> bool:
    """t 的每個字都是 src 對應字的合法轉法（或原字）。"""
    return len(src) == len(t) and all(b == a or b in s2t.get(a, ()) for a, b in zip(src, t))


# ── 分歧裁決 ────────────────────────────────────────────────────────────

def opencc_convert(paras: list[str]) -> list[str]:
    """系統 opencc CLI（s2twp），逐行對應。段內換行先換成空白以維持行數。"""
    out = subprocess.run(
        ["opencc", "-c", "s2twp.json"], input="\n".join(p.replace("\n", " ") for p in paras),
        capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    if len(out) > len(paras):  # 結尾換行
        out = out[:len(paras)]
    assert len(out) == len(paras), f"opencc 行數不符 {len(out)} != {len(paras)}"
    return out


def diverge(src: str, a: str, b: str, ctx: int = DIFF_CTX):
    """yield (src_span, a_span, b_span, a_context)。src 只在與 a 等長時可逐字對應。"""
    same_len = len(src) == len(a)
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        s = src[i1:i2] if same_len else ""
        yield s, a[i1:i2], b[j1:j2], f"{a[max(0, i1 - ctx):i1]}【{a[i1:i2]}|{b[j1:j2]}】{a[i2:i2 + ctx]}"


def divergence_rows(pairs: list[tuple[str, str]], cc_out: list[str]) -> list[dict]:
    """(原文, 繁化姬) 段落對 + OpenCC 輸出 → 分歧種類（依次數排序，id 從 1 起）。"""
    groups: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "examples": []})
    for (src, a), b in zip(pairs, cc_out):
        a = a.replace("\n", " ")
        if a == b:
            continue
        for s, za, cb, ctx in diverge(src.replace("\n", " "), a, b):
            g = groups[(s, za, cb)]
            g["count"] += 1
            if len(g["examples"]) < 3:
                g["examples"].append(ctx)
    rows = sorted(({"src": s, "zh": a, "cc": b, **g} for (s, a, b), g in groups.items()),
                  key=lambda r: -r["count"])
    for i, r in enumerate(rows, 1):
        r["id"] = i
    return rows


def build_adjudicate_prompt(rows: list[dict]) -> str:
    lines = [f"id={r['id']} 原文={r['src'] or '（未對齊）'} zh={r['zh'] or '∅'} cc={r['cc'] or '∅'}\n  例句：" +
             " ／ ".join(r["examples"]) for r in rows]
    return "逐筆判斷，每個 id 都要回傳：\n\n" + "\n".join(lines)


def adjudicate(rows: list[dict], model: str = MODEL, effort: str = EFFORT, log=print) -> tuple[dict[int, dict], float]:
    out: dict[int, dict] = {}
    cost = 0.0
    for i in range(0, len(rows), ADJ_BATCH):
        chunk = rows[i:i + ADJ_BATCH]
        res, c = claude_cli(ADJUDICATE_SYSTEM, build_adjudicate_prompt(chunk), VERDICTS_SCHEMA, model, effort)
        for v in res["items"]:
            out[v["id"]] = v
        cost += c
        missing = {r["id"] for r in chunk} - set(out)
        log(f"  分歧裁決 batch {i // ADJ_BATCH + 1}: {len(chunk)} 筆" + (f"，缺 {sorted(missing)}" if missing else ""))
    return out, cost


# ── 名字檢查 ────────────────────────────────────────────────────────────

def name_candidates(pairs: list[tuple[str, str]], s2t: dict[str, set], moe: frozenset,
                    min_total: int = 3, min_consistent: int = 5) -> list[dict]:
    """含「一簡對多繁、且本書實際出現多種轉法」字的 2–4 字 n-gram；
    留轉法不一致的，或高頻且轉換結果不是辭典詞的（專名候選）。"""
    # ponytail: 只看等長段落；含在地化改寫（優化→最佳化）的段落會漏，量少可接受
    pairs = [(s, o) for s, o in pairs if len(s) == len(o)]
    multi = {a for a, ts in s2t.items() if len(ts) >= 2}
    # 只留這本書裡真的出現兩種以上轉法的字（了、种 在字表有多對應，但實際永遠只轉一種）
    seen: dict[str, set] = defaultdict(set)
    for s, o in pairs:
        for a, b in zip(s, o):
            if a in multi:
                seen[a].add(b)
    amb = {a for a, bs in seen.items() if len(bs) >= 2}

    grams: dict[str, Counter] = defaultdict(Counter)
    example: dict[tuple[str, str], str] = {}
    for s, o in pairs:
        for n in (2, 3, 4):
            for i in range(len(s) - n + 1):
                w = s[i:i + n]
                if CJK.match(w) and any(c in amb for c in w):
                    t = o[i:i + n]
                    grams[w][t] += 1
                    example.setdefault((w, t), f"{o[max(0, i - NAME_CTX):i]}【{t}】{o[i + n:i + n + NAME_CTX]}")

    total = {w: sum(c.values()) for w, c in grams.items() if sum(c.values()) >= min_total}
    # 只留最長：被等次數的 n+1 字 n-gram 完全包含的就丟掉（烏裡朵 ⊂ 烏裡朵拉）
    longer = defaultdict(set)
    for v in total:
        for k in (v[1:], v[:-1]):
            longer[k].add(total[v])
    rows = []
    for w, n in total.items():
        if n in longer[w]:
            continue
        c = grams[w]
        if len(c) >= 2 or (n >= min_consistent and not any(t in moe for t in c)):
            rows.append({"src": w, "renderings": dict(c.most_common()),
                         "examples": [example[(w, t)] for t in c]})
    rows.sort(key=lambda r: -sum(r["renderings"].values()))
    for i, r in enumerate(rows):
        r["id"] = i
    return rows


def build_name_prompt(rows: list[dict]) -> str:
    return "\n".join(
        f"id={r['id']} {r['src']} → " + "、".join(f"{t}({n})" for t, n in r["renderings"].items())
        + "　例：" + " ／ ".join(r["examples"][:2])
        for r in rows)


def check_names(rows: list[dict], model: str = MODEL, effort: str = EFFORT) -> tuple[list[dict], float]:
    """回傳被標出的候選（候選欄位 + correct/reason）。"""
    flags, cost = [], 0.0
    for i in range(0, len(rows), NAME_BATCH):
        res, c = claude_cli(NAME_SYSTEM, build_name_prompt(rows[i:i + NAME_BATCH]), NAMES_SCHEMA, model, effort)
        flags += res["items"]
        cost += c
    by_id = {r["id"]: r for r in rows}
    return [{**by_id[f["id"]], **f} for f in flags if f["id"] in by_id], cost


# ── 規則 ────────────────────────────────────────────────────────────────

def name_rules(results: list[dict], s2t: dict[str, set], log=print) -> list[tuple[str, str, int, str, str]]:
    """名字檢查結果 → (wrong, right, 對齊次數, 原文 n-gram, 註記)。丟棄對齊假陽性。"""
    out = []
    for r in results:
        right = r["correct"]
        if not legal(r["src"], right, s2t):
            log(f"  丟棄（正確寫法不合法）{r['src']}→{right}")
            continue
        majority = max(r["renderings"], key=r["renderings"].get)
        for t, n in r["renderings"].items():
            # 轉法不一致時，保留原字的多數寫法在其他語境通常是對的（有些困擾 vs 有些睏）→ 跳過；
            # 修正若是把字還原成原字（烏裡朵拉→烏里朵拉，取音用字）則照修
            keeps_src = all(a == b for a, b, c in zip(r["src"], t, right) if b != c)
            if t == right or (len(r["renderings"]) >= 2 and t == majority and keeps_src):
                continue
            if not legal(r["src"], t, s2t):
                log(f"  丟棄（對齊假陽性）{r['src']}：{t}→{right}")
                continue
            out.append((t, right, n, r["src"], f"名字檢查：{r['reason']}"))
    return out


def verdict_rules(rows: list[dict], verdicts: dict[int, dict]) -> list[tuple[str, str, int, str, str]]:
    """裁決為 cc（OpenCC 對）或 neither（附正確寫法）的分歧 → 以例句左右 RULE_K 字為上下文的規則。"""
    by_id = {r["id"]: r for r in rows}
    out = []
    for k, v in verdicts.items():
        if v["verdict"] not in ("cc", "neither") or int(k) not in by_id:
            continue
        r = by_id[int(k)]
        # 只修中文轉換問題；數字、英文的差異多半是註腳編號等抽取差（萬人迷【|1】）
        if not re.search(r"[\u4e00-\u9fff]", r["zh"] + r["cc"]) or re.search(r"[0-9A-Za-z]", r["zh"] + r["cc"]):
            continue
        if v["verdict"] == "neither" and not v.get("correct"):
            continue
        for ex in r["examples"]:
            m = CTX_RE.match(ex)
            if not m:
                continue
            left, a, b, right = m.groups()
            if v["verdict"] == "neither":
                fix = _window_fix(left, a, right, v["correct"])
                if fix is None:
                    continue
                p, q, b = fix
                left, a, right = left[:len(left) - p], left[len(left) - p:] + a + right[:q], right[q:]
            out.append((left[-RULE_K:] + a + right[:RULE_K], left[-RULE_K:] + b + right[:RULE_K], 0, "",
                        f"分歧裁決 #{k}：{v['reason']}"))
    return out


def _window_fix(left: str, a: str, right: str, correct: str, span: int = 4) -> tuple[int, int, str] | None:
    """neither 的正確寫法可能連【】外的字一起改（乾農【事】→ 幹農活）。
    在【】左右各擴 0–span 字的窗口中，找最像 correct 的一段：優先等長且同位置相同字最多，
    否則取相似度最高。回傳 (左擴字數, 右擴字數, correct)；對不上回傳 None。"""
    best, best_score = None, (0, 0.0)
    for p in range(min(span, len(left)) + 1):
        for q in range(min(span, len(right)) + 1):
            w = left[len(left) - p:] + a + right[:q]
            same = sum(x == y for x, y in zip(w, correct)) if len(w) == len(correct) else 0
            score = (same, SequenceMatcher(None, w, correct).ratio())
            if score > best_score:
                best, best_score = (p, q, w), score
    if best is None or best[2] == correct:
        return None
    return best[0], best[1], correct


def build_rules(cands: list[tuple[str, str, int, str, str]], pairs: list[tuple[str, str]]) -> tuple[dict, dict, dict]:
    """合併候選規則 → (rules, review, notes)。
    依命中數依序套用，被前面規則吃掉而不再命中的刪除（重疊 n-gram 只留一條）；
    名字規則逐段比對：某段命中次數 > 該段原文出現原文詞的次數（會改到原文不是這個詞的地方）
    → review，要人工看例句。不靠對齊次數，避免候選分組變動（烏裡朵拉 被拆到別組）造成誤判。"""
    merged: dict[str, list] = {}
    for wrong, right, n, src, note in cands:
        if len(wrong) < 2 or wrong == right:
            continue
        if wrong in merged:
            merged[wrong][1] += n
        else:
            merged[wrong] = [right, n, src, note]

    srcs = [s for s, _ in pairs]
    texts = [t for _, t in pairs]
    hits = {w: sum(t.count(w) for t in texts) for w in merged}
    rules, review, notes = {}, {}, {}
    for w in sorted(merged, key=lambda w: (-hits[w], -len(w))):
        right, aligned, src, note = merged[w]
        remaining = sum(t.count(w) for t in texts)
        if remaining == 0:
            continue
        # ponytail: 只比次數，同原文詞不同語境（布制的 vs 限制的）分不出來，靠 LLM 的判斷與多數寫法跳過
        risky = bool(src) and any(t.count(w) > s.count(src) for s, t in zip(srcs, texts))
        ctx = [t[max(0, m.start() - 6):m.end() + 6] for t in texts for m in re.finditer(re.escape(w), t)][:5]
        # 去重時把 review 也視為核准，避免它的重疊片段又進規則
        (review if risky else rules)[w] = right
        texts = [t.replace(w, right) for t in texts]
        notes[w] = f"命中 {remaining}" + (f"（對齊 {aligned}）" if aligned else "") + f"；{note}" \
            + (f"；例：{' ／ '.join(c.replace(chr(10), ' ') for c in ctx)}" if risky else "")
    return rules, review, notes


# ── 整本書 ──────────────────────────────────────────────────────────────

def review_book(pairs: list[tuple[str, str]], s2t: dict[str, set], moe: frozenset,
                model: str = MODEL, effort: str = EFFORT, log=print) -> tuple[dict, dict, dict]:
    """(原文, 繁化姬輸出) 段落對 → (自動套用規則, 待審規則, 註記)。"""
    cands, cost = [], 0.0

    if shutil.which("opencc"):
        rows = divergence_rows(pairs, opencc_convert([s for s, _ in pairs]))
        log(f"  🤖 分歧裁決：{len(rows)} 種")
        verdicts, c = adjudicate(rows, model, effort, log)
        cands += verdict_rules(rows, verdicts)
        cost += c
    else:
        log("  ⚠️  找不到 opencc CLI，略過分歧裁決（brew install opencc）")

    rows = name_candidates(pairs, s2t, moe)
    log(f"  🤖 名字檢查：{len(rows)} 個候選")
    results, c = check_names(rows, model, effort)
    cands += name_rules(results, s2t, log)
    cost += c

    rules, review, notes = build_rules(cands, pairs)
    log(f"  🤖 LLM 校對：自動套用 {len(rules)} 條、待審 {len(review)} 條（等值 ${cost:.2f}）")
    return rules, review, notes


def write_report(path: str, rules: dict, review: dict, notes: dict, hits: dict[str, list[tuple[str, str]]]) -> None:
    lines = [f"# LLM 校對報告\n\n自動套用 {len(rules)} 條、待審 {len(review)} 條。"
             "待審規則未套用；確認無誤可加進 corrections.json。\n"]
    lines.append("\n## 已套用\n")
    for w, r in rules.items():
        lines.append(f"- `{w}` → `{r}`　{notes[w]}")
        lines += [f"  - …{b}… → …{a}…" for b, a in hits.get(w, [])[:2]]
    lines.append("\n## 待審（未套用）\n")
    lines += [f"- `{w}` → `{r}`　{notes[w]}" for w, r in review.items()]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    s2t = load_s2t()
    assert legal("乌里朵拉", "烏裡朵拉", s2t) and legal("乌里朵拉", "烏里朵拉", s2t)
    assert not legal("的玩", "的東", s2t)
    # 多數寫法不出規則；對齊假陽性丟棄
    res = [{"src": "有些困", "renderings": {"有些困": 3, "有些睡": 1}, "correct": "有些睏", "reason": ""},
           {"src": "乌里朵拉", "renderings": {"烏裡朵拉": 5}, "correct": "烏里朵拉", "reason": ""}]
    assert [w for w, *_ in name_rules(res, s2t, log=lambda _: None)] == ["烏裡朵拉"]
    # 多數寫法若是「把原字轉錯」（烏裡朵拉），即使組內不一致也要修
    res = [{"src": "乌里朵拉", "renderings": {"烏裡朵拉": 9, "烏里朵拉": 1}, "correct": "烏里朵拉", "reason": ""}]
    assert [w for w, *_ in name_rules(res, s2t, log=lambda _: None)] == ["烏裡朵拉"]
    # neither：correct 可能連【】外的字一起改，要找對窗口
    assert _window_fix("而融合", "併最佳", "化了", "並最佳") == (0, 0, "並最佳")
    assert _window_fix("而融合", "併最佳", "化了", "融合並最佳化") == (2, 1, "融合並最佳化")
    assert _window_fix("類族乾農", "事", "，開什", "幹農活") == (2, 0, "幹農活")
    assert _window_fix("我是", "担", "任插画的カ", "擔任插畫") == (0, 3, "擔任插畫")
    v = {"1": {"verdict": "neither", "correct": "幹農活", "reason": ""}}
    assert verdict_rules([{"id": 1, "zh": "事", "cc": "活", "examples": ["讓人類族乾農【事|活】，開什麼"]}], v)[0][:2] \
        == ("類族乾農事，開", "類族幹農活，開")
    v = {"2": {"verdict": "cc", "correct": "", "reason": ""}}
    assert verdict_rules([{"id": 2, "zh": "", "cc": "1", "examples": ["萬人迷【|1】』的"]}], v) == []
    # 重疊規則只留一條；命中超出對齊的進 review；不等長段落但原文含該詞 → 不算超出
    rules, review, _ = build_rules(
        [("烏裡朵拉", "烏里朵拉", 2, "乌里朵拉", ""), ("裡朵拉和", "里朵拉和", 1, "里朵拉和", ""),
         ("制的", "製的", 1, "布制的", ""), ("春鬥", "春斗", 1, "春斗", "")],
        [("乌里朵拉和玛利。乌里朵拉。", "烏裡朵拉和瑪利。烏裡朵拉。"),
         ("布制的衣服、限制的次数", "布制的衣服、限制的次數"),
         ("春斗打开瓶盖", "春鬥打開瓶蓋"), ("春斗优化了", "春鬥最佳化了")])
    assert rules == {"烏裡朵拉": "烏里朵拉", "春鬥": "春斗"} and review == {"制的": "製的"}, (rules, review)
    print("ok")
