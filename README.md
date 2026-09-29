# LINE Bots（Jenny 助理 + 賣家客服，合併部署）

同一個 Flask app 同時服務**兩隻獨立的 LINE Bot**，讓兩者共用一個部署（一份 Render 服務），節省費用。兩隻各自使用自己的 LINE channel（不同 token / secret），透過不同的 URL 前綴分流。

| Bot | 前綴 | Webhook URL | 說明 |
| --- | --- | --- | --- |
| Jenny 個人助理 | `/jenny` | `.../jenny/callback` | 帶上下文 GPT 聊天、名片辨識、專案/新聞/單字推送 |
| 賣家客服 | `/customer` | `.../customer/callback` | 亞馬遜賣家問答，System Prompt 由 Google Sheet 動態載入 |

## 檔案結構

| 檔案 | 說明 |
| --- | --- |
| `app.py` | 入口：建立 Flask app、掛載兩個 blueprint、提供 `/ping` |
| `bot_jenny.py` | Jenny 個人助理（blueprint `jenny`，掛載於 `/jenny`） |
| `bot_customer.py` | 賣家客服 bot（blueprint `customer`，掛載於 `/customer`） |
| `notion_vocab.py` | 日文 N1 單字整理（供推送用） |
| `Procfile` | `web: gunicorn app:app` |

## 功能

### Jenny 個人助理（`/jenny`）
- **AI 聊天助手**：一般文字交給 OpenAI 模型（由 `OPENAI_MODEL` 環境變數指定），依聊天室各自保留最近 10 筆對話，超過 30 分鐘無互動自動清空
- **純文字回覆**：system prompt 禁用 Markdown，並以 `strip_markdown()` 過濾 `**粗體**`/`# 標題`/`` `code` ``/`- 條列` 等符號，避免 LINE 顯示原始星號
- **語言跟隨**：一律以使用者訊息的語言回覆（英文問英文答、中文問中文答），不自行切換
- **空回覆防呆**：模型回傳空內容時改回 fallback 文案，不送空訊息給 LINE（避免 400 `May not be empty`）
- **名片辨識自動建檔**：在 `CARD_GROUP_ID` 群組上傳圖片，影像辨識後寫入 Google Sheet「名片」工作表（附重試機制）
- **每日專案／市場新聞／N1 單字推送**：`POST /jenny/push/daily`、`/jenny/push/news`、`/jenny/push/vocab`（帶 `secret`），供 GitHub Actions 排程呼叫
- **群組導覽 & 除錯**：目標群組發訊回固定導覽；輸入 `show-group-id` 取得 `chat_id`；`GET /jenny/debug/group-id`

### 賣家客服（`/customer`）
- **AI 客服問答**：OpenAI 模型（由 `OPENAI_MODEL` 指定），自動偵測中/英文並附免責聲明
- **Google Sheet 動態 Prompt / 指令模式**：試算表 `AI_Assistant_Config`，指令切換人設——`#polish` 潤稿、`#trans` 翻譯、`#biz` 商業檢視、`#line` LINE 文案、`#ai`
- **官方已處理關鍵字跳過**：wifi／預約諮詢／促銷提報／品牌註冊等不呼叫 GPT
- **FAQ 罐頭回覆 + 快取**；**私聊直接回、群組需 `@bot` 才回**

## API 路由

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| POST | `/jenny/callback` | Jenny bot LINE Webhook |
| POST | `/jenny/push/daily` | 每日專案推送（需 `secret`） |
| POST | `/jenny/push/news` | 市場新聞推送（需 `secret`） |
| POST | `/jenny/push/vocab` | N1 單字推送（需 `secret`） |
| GET | `/jenny/debug/group-id` | 顯示目前 `TARGET_GROUP_ID` |
| POST | `/customer/callback` | 賣家客服 bot LINE Webhook |
| GET | `/ping` | Keep-alive 健康檢查 |

## 環境變數

| 變數 | 用途 |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI 金鑰（兩隻共用） |
| `OPENAI_MODEL` | （選填）指定使用的模型，未設定時用程式碼預設值。聊天機器人建議用非推理型模型（如 `gpt-5.4-mini` / `gpt-4o-mini`），推理型模型（如 `gpt-6-luna`）易因推理耗盡 token 導致輸出為空 |
| `JENNY_LINE_CHANNEL_ACCESS_TOKEN` | Jenny channel 存取權杖（相容舊名 `LINE_CHANNEL_ACCESS_TOKEN`） |
| `JENNY_LINE_CHANNEL_SECRET` | Jenny channel 簽章密鑰（相容舊名 `LINE_CHANNEL_SECRET`） |
| `TARGET_GROUP_ID` | Jenny 推送目標群組 |
| `CARD_GROUP_ID` | 名片辨識群組 |
| `Creds2` | Jenny 用 Google service account 憑證 JSON |
| `PUSH_SECRET` | Jenny 推送端點驗證密鑰 |
| `CUST_LINE_CHANNEL_ACCESS_TOKEN` | 客服 channel 存取權杖 |
| `CUST_LINE_CHANNEL_SECRET` | 客服 channel 簽章密鑰 |
| `GOOGLE_SHEETS_KEY` | 客服用 Google service account 憑證 JSON |

## 部署

以 `Procfile` 搭配 `gunicorn` 啟動（`web: gunicorn app:app`），可部署於 Render、Heroku 等平台。目前兩隻 bot 已**合併為單一 Render 服務**部署（原本各一份服務，合併後省下一份月費）。部署設定：

1. 於平台設定上述環境變數
2. LINE Developers Console 將兩個 channel 的 Webhook URL 分別設為 `.../jenny/callback` 與 `.../customer/callback`
3. GitHub Actions 的推送 URL 指向 `.../jenny/push/daily`、`/jenny/push/news`、`/jenny/push/vocab`

> **備註**：本 repo 已整併原 `kaojia/line-bot-customer` 的賣家客服程式，合併後由此單一服務部署；`line-bot-customer` repo 可保留為歷史或封存。

---

*本 README 由觀察 repository 原始碼整理產生。*
