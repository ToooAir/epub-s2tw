# epub-s2tw

將簡體中文 EPUB 轉換為繁體中文（台灣用語），適合閱讀簡中電子書的繁中讀者使用。
使用 Google 翻譯進行簡繁轉換與詞彙在地化，支援免費模式與官方 Cloud Translation API。

> **免責聲明**：免費模式使用 Google 未公開的非官方端點，可能隨時失效或違反服務條款，請自行評估風險。

## 專案結構

```
epub-translator/
├── translate_epub.py     # CLI 入口
├── epub_handler.py       # EPUB 讀取、文字抽取、翻譯替換、儲存
├── translator.py         # Google Translate 包裝層（快取、批次、並行、兩種模式）
├── postprocess.py        # 後處理層：修正 Google 的繁體歧義字與已知翻譯錯誤
├── STPhrases.txt         # OpenCC 詞組對照表（簡→繁，約 49k 條）
├── STCharacters.txt      # OpenCC 單字對照表（簡→繁，含多選項）
├── corrections.json      # 實測 Google 輸出所建的修正字典
├── build_corrections.py  # 建立 corrections.json 的工具腳本
├── requirements.txt
├── .env.example
└── README.md
```

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# （可選）建立 .env
cp .env.example .env
# 編輯 .env 填入 GOOGLE_API_KEY
```

## 用法

### 免費模式（不需 API Key）

```bash
# 單檔
python translate_epub.py 小说.epub --free

# 多檔（shell glob）
python translate_epub.py ./*.epub --free

# 遞迴掃描整個資料夾（自動排除 output 目錄）
python translate_epub.py -d ./books --free
```

### API 模式（Google Cloud Translation v2）

```bash
python translate_epub.py -d ./books --api-key YOUR_KEY
# 或在 .env 設定 GOOGLE_API_KEY，直接執行：
python translate_epub.py -d ./books
```

### 常用選項

| 選項 | 說明 |
|------|------|
| `-d DIR` | 遞迴掃描資料夾內所有 .epub |
| `-o DIR` | 指定輸出資料夾（預設 `./output`）|
| `--free` | 使用免費 Google 翻譯 |
| `-k KEY` | 直接傳入 API Key |
| `--no-rename` | 不把輸出檔名轉為繁體 |
| `--log-consistency` | 輸出一致性修正報告 (`_consistency.txt`) |
| `-v` | 顯示每個 XHTML 的處理進度 |
| `--dry-run` | 只列出會處理的檔案，不實際翻譯 |

### 完整範例

```bash
# 預覽會處理哪些檔案（確認不含 output）
python translate_epub.py -d . --dry-run

# 免費版翻譯整個資料夾，顯示詳細進度
python translate_epub.py -d ./books --free -v -o ./繁體輸出

# 重跑（已存在的輸出會自動跳過）
python translate_epub.py -d ./books --free
```

## 後處理層

Google Translate 在簡繁轉換上有兩類系統性錯誤，由 `postprocess.py` 自動修正：

**1. 字元歧義誤判**（最常見）

簡體字 `发` 對應繁體 `發`（動詞）或 `髮`（頭髮），Google 有時因斷詞錯誤而用錯。例如「料理发问」被斷成「料理髮問」，後處理會修正回「料理發問」。

修正規則從 `STPhrases.txt`（OpenCC 詞組表）與 `STCharacters.txt`（字元對照表）自動生成，涵蓋 60+ 種歧義字（发/髮、台/臺/颱、干/乾/幹、烟/菸 等）。

**2. 詞組整體翻錯**

部分詞組（尤其成語）Google 會整個翻錯，如「七竅冒煙」翻成「七技巧冒煙」。這類錯誤收錄在 `corrections.json`，由 `build_corrections.py` 實際測試 Google 輸出後建立。

後處理採 **single-pass 最長匹配**掃描，每個位置只處理一次，避免多條規則連鎖衝突。

### 更新 corrections.json

遇到新的翻譯錯誤時，可重新執行：

```bash
python build_corrections.py          # 全跑（約 15-20 分鐘）
python build_corrections.py --limit 500  # 快速測試前 500 條
```

腳本支援中斷續跑，進度儲存於 `corrections.json`。

## 一致性修正

翻譯完成後，會自動掃描全書統一同一原文被翻成不同繁體形式的情況。

**典型案例：** 簡體原文「艾莉莎」在不同章節被 Google 翻成「艾莉莎」或「艾麗莎」（字形混淆），一致性修正會統計兩者出現次數，將少數派改成多數派。

**演算法：**
1. 收集全書所有段落的（原文簡體, 翻譯繁體）對，涵蓋純文字與含 inline 標籤的段落
2. 對每個原文 3–4 字 n-gram，在對應的翻譯段落中搜尋合法的繁體對應形式
3. 若同一原文 n-gram 出現多種翻譯，少數派（佔比 < 25%）統一成多數派
4. 反向碰撞檢查防止跨詞彙誤連：若「候選翻譯」反轉回簡體後恰好是原文中另一個詞彙，則跳過
5. 核心差異精簡：只保留到最後一個差異字元，提升修正覆蓋率（e.g. `艾麗莎不→艾莉莎不` 精簡為 `艾麗→艾莉`）

### 除錯與回報問題

若發現翻譯結果中有不自然的名詞替換，或希望確認系統進行了哪些一致性修正：

1. 使用 `--log-consistency` 重新執行或處理該 EPUB 檔案。
2. 開啟 `output/` 目錄下的 `[檔名]_consistency.txt` 查看修改紀錄。
3. 若確認為系統的越界誤判或語意破壞，歡迎至 GitHub 建立 Issue。
4. **建立 Issue 時的建議**：請一併附上該篇報告中對應錯誤的那些「**修正前／修正後**」例句，這將大幅加快我們除錯與調整演算法的速度！

## 快取機制

翻譯結果儲存在 `.translate_cache.json`。同一段文字只會送 API 一次，重跑或中斷後繼續都能省下費用與時間。批次處理多本書時，跨書的重複段落（角色名、口頭禪等）同樣只翻一次。

## 翻譯引擎

免費模式使用 `translate.googleapis.com/translate_a/single` 非官方端點：

- 不需 API Key
- 單次請求上限約 1800 字元，自動切塊處理
- 多段文字以 `⚡` 分隔符合併為單一請求，大幅減少 API 呼叫次數
- 5 個並行請求同時送出，速度比逐條快 3～5 倍
- 失敗自動以指數退讓（2→4→8→…→120 秒）無限重試，不產出簡體夾繁體的半成品

## 費用估算（API 模式）

Google Cloud Translation Basic API：每 100 萬字元 **USD $20**。  
一本 40 萬字輕小說約 **USD $8**；免費額度每月 50 萬字元。

## 處理範圍

| 內容 | 說明 |
|------|------|
| 內文 XHTML | block-level 元素（p, h1～h6, li 等）全數翻譯 |
| 含 inline 標籤 | `<em>`, `<strong>` 等以 HTML 模式翻譯，保留格式 |
| nav.xhtml 目錄 | 透過 `book.toc` 更新，正確反映在電子書目錄 |
| toc.ncx | `<navLabel>` 與 `<docTitle>` 全數翻譯 |
| OPF metadata | title、description、publisher、subject、belongs-to-collection、file-as |
| 不翻譯 | `script`, `style`, `code`, `pre`, `rt`（注音）內的文字 |

## 已知限制

- 免費模式對超長段落會自動切塊，但極端情況仍可能觸發 rate limit（會自動重試）
- 內文中的注音（ruby/rt）標籤不會被翻譯
- 一致性修正目前不覆蓋 toc.ncx 與 nav.xhtml，目錄中的人名可能仍有不一致
- 若 Google 回傳的 HTML 有 entity 雙重編碼問題，可在 `epub_handler.py` 中把 `fmt="html"` 改為 `fmt="text"`
