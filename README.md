# epub-s2tw

將簡體中文的 EPUB 翻譯成適合自己「舒服閱讀」的繁體中文（專為閱讀簡中電子書的台灣讀者打造）。
本專案不只是一個單純的 Google 翻譯腳本，而是一套完整的「在地化管線（Localization Pipeline）」。它除了使用機翻解決語意問題外，更整合了 OpenCC 官方台灣異體字標準、NMT 幻覺降級機制與全書全域一致性檢查，致力於產出如同台版實體書般自然、順暢且沒有機翻怪味的閱讀體驗。

> **免責聲明**：免費模式使用 Google 未公開的非官方端點，可能隨時失效或違反服務條款，請自行評估風險。

## 為什麼不用純文字轉換 (OpenCC)？

本專案與傳統的 `opencc -c s2tw` 轉換有著本質上的架構與效果差異。這裡並非只是一個「打 API 的翻譯腳本」，而是一套兼具除錯與全域統計的在地化管線 (Localization Pipeline)：

| 核心特性 | epub-translator | 純 OpenCC |
| --- | --- | --- |
| **翻譯引擎** | **神經機器翻譯 (Google Translate)**<br>基於整段數百字的上下文進行語意對齊轉換。能精確辨識輕小說中的俚語、自創詞與語法。 | **靜態 Trie Tree 文字對應**<br>基於單字或固定詞組的冰冷替換，遇到未收錄的新穎詞語往往會翻車。 |
| **後處理防護** | **逆向防禦網 + 台灣過濾鏡**<br>將 OpenCC 字典轉化為「捕捉 Google 潛在錯誤的陷阱」。利用 `STPhrases` 偵測轉換錯詞並導正，外加 `TWVariants` 強制洗滌成台灣本地標準字 (如裏→裡)。 | **正向單行道**<br>給簡體、吐繁體。若原始資料本身就是錯字，吐出來的也是錯字。 |
| **全域視野** | **具備整本電子書的全域記憶**<br>掃描全書 N-gram，基於多數決修復個別章節的人名或術語變異（例如：全域統一主角名字「艾莉莎」）。 | **無狀態 (Stateless)**<br>第一行與第一千行的翻譯完全獨立，沒有前後文連貫性概念。 |
| **容錯機制** | **自動碎紙重譯自救機制**<br>若偵測到 Google NMT 遇到高重複疊詞而卡死截斷，能自動觸發標點符號降級拆分重譯，防止漏句。 | **無容錯**<br>Garbage In, Garbage Out。 |


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

純粹的機翻或 OpenCC 都有各自的死穴。`postprocess.py` 會自動建立防護網來修正這些翻譯或轉換錯誤：

**1. 台灣異體字標準化（TWVariants 洗滌機制）**

一般簡轉繁詞庫（如 STPhrases）會輸出極度古典的考據繁體字（例如把「才會」轉成「纔會」、「裡面」轉成「裏面」）。
系統會動態載入最高權限的 `TWVariants.txt`，並充當「淨水器」，將翻譯後的所有字元與內部字典強制洗滌成台灣現代標準字形（如 纔→才、裏→裡、麪→麵），徹底消除對岸機翻古典味。

**2. 字元歧義防誤判**

利用 `STPhrases.txt` 與 `STCharacters.txt` 逆向生成陷阱規則。當 Google 斷錯詞（將「料理发问」翻成「料理髮問」）時，系統能主動攔截並修復回「料理發問」。

**3. 詞組整體修復與自訂補丁**

如「七竅冒煙」被翻成「七技巧冒煙」等 Google 系統性詞彙翻譯錯誤。這類專屬補丁收錄於 `corrections.json` 中，支援手動維護。

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
1. 收集全書純文字與含 inline 標籤的所有段落。
2. 以 N-gram 抽樣提取：若遇到全固定字串（如人名變體），嚴格要求長度達 4 字以上才允許 1 字元容錯，徹底阻斷「情緒/情感」這類同義詞被誤殺的情況。
3. 若同一原文的翻譯方式產生分歧，少數派（佔比 < 25%）將被強制統一為多數派。
4. **反向防撞機制**：若某「翻譯候選」逆轉回簡體後，恰巧撞名原文中存在的另一個截然不同的詞，系統會取消統一，以保護作者原意。
5. **核心差異精簡 (Core Diffs)**：剝離雜訊只提煉關鍵字差異（如 `艾麗莎不→艾莉莎不` 精簡為 `艾麗→艾莉`），以極具破壞力的短規則完成全域修改。

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

- 單次請求上限約 1800 字元，自動切塊，多段合併並行請求大幅提速。
- **神經網路幻覺防護 (NMT Fallback)**：當遇到角色語調激動（包含大量重複字眼如「我知道...我知道...我知道！」）時，Google 翻譯極易陷入迴圈，發生造詞幻覺或直接吃句（截斷）。
- **異常偵測與閃電隔離重譯**：一旦偵測到文字長度異常流失 (>15%) 或是句尾標點消失，終端機會拋出警告 `⚠️`，並觸發自救機制：將該段落物理切碎，塞入 `⚡` 分隔符號強制切斷 NMT 注意力機制後，以單一請求發送。這不僅完美繞過漏句 Bug，更免去了 HTTP 500 擋刷限流，達成近乎 4 倍的重譯效能提升。
- 失敗自動以指數退讓無限重試，絕不產出夾雜簡體的半成品。

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
