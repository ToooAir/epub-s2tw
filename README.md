# epub-translator

將簡體中文 EPUB 翻譯為繁體中文，支援免費版 Google 翻譯與官方 Cloud Translation API。

> **免責聲明**：免費模式使用 Google 未公開的非官方端點，可能隨時失效或違反服務條款，請自行評估風險。

## 專案結構

```
epub-translator/
├── translate_epub.py   # CLI 入口
├── epub_handler.py     # EPUB 讀取、文字抽取、翻譯替換、儲存
├── translator.py       # Google Translate 包裝層（快取、批次、並行、兩種模式）
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
- 若 Google 回傳的 HTML 有 entity 雙重編碼問題，可在 `epub_handler.py` 中把 `fmt="html"` 改為 `fmt="text"`
