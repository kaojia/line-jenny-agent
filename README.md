# LINE Jenny Agent

一個以 Flask 打造的 LINE Bot，整合 OpenAI GPT 進行對話與名片辨識，並透過 Google Sheet 建檔、GitHub Actions 排程推送內容到 LINE 群組。

## 功能特色

**AI 聊天助手**
一般文字訊息會交給 GPT-4o-mini 處理，並依聊天室（個人或群組）各自保留最近 10 筆對話紀錄，超過 30 分鐘沒有互動則自動清空，讓回覆能延續上下文。

**名片辨識與自動建檔**
在指定的「名片群組」（`CARD_GROUP_ID`）上傳圖片時，Bot 會下載圖片並交給 GPT-4o 影像辨識，擷取姓名、公司、職稱、電話、手機、Email、地址、網站、備註等欄位，再自動寫入指定的 Google Sheet（「名片」工作表，不存在時會自動建立），並附上重試機制避免因網路問題寫入失敗。

**每日 Claude Code 專案靈感推送**
提供 `POST /push/daily` 端點，供 GitHub Actions 排程呼叫，帶入專案清單後會整理成含難度標示（🟢初階／🟡中階／🔴高階）、分類與說明的單則訊息，推送到目標群組，並附上完整列表網站連結。

**日文 N1 單字推送**
提供 `POST /push/vocab` 端點，同樣由 GitHub Actions 排程呼叫，將整理好的單字內容推送到目標群組。

**群組導覽訊息**
若在目標推送群組（`TARGET_GROUP_ID`）中發送一般訊息，Bot 不會呼叫 GPT，而是固定回覆該群組用途說明與網站連結，避免非必要的 API 呼叫。

**除錯輔助**
在任何聊天室輸入 `show-group-id` 可取得該聊天室的 `chat_id`，方便設定環境變數；另提供 `GET /debug/group-id` 查看目前設定的 `TARGET_GROUP_ID`。

**Loading 動畫**
回覆前會呼叫 LINE 的 Loading API，讓使用者在等待 GPT 回應時看到讀取動畫，提升體驗。

## 技術架構

- **Web 框架**：Flask + gunicorn（`Procfile`：`web: gunicorn LINE_bot:app`）
- **LINE 整合**：`line-bot-sdk`（Webhook 簽章驗證、訊息推播）
- **AI 模型**：OpenAI `gpt-4o-mini`（文字對話）、`gpt-4o`（名片圖片辨識）
- **資料儲存**：Google Sheets（透過 `gspread` + service account 憑證寫入）
- **排程觸發**：`.github/workflows` 中的 GitHub Actions，定期呼叫 `/push/daily`、`/push/vocab` 端點
- **設定管理**：`python-dotenv` 讀取環境變數

## API 路由

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| POST | `/callback` | LINE Webhook 主入口，接收並處理訊息事件 |
| POST | `/push/daily` | 接收 GitHub Actions 推送的每日專案清單（需帶 `secret`） |
| POST | `/push/vocab` | 接收 GitHub Actions 推送的單字內容（需帶 `secret`） |
| GET | `/debug/group-id` | 查看目前設定的 `TARGET_GROUP_ID` |

## 環境變數

| 變數 | 用途 |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging API 存取權杖 |
| `LINE_CHANNEL_SECRET` | LINE Webhook 簽章驗證密鑰 |
| `OPENAI_API_KEY` | OpenAI API 金鑰 |
| `TARGET_GROUP_ID` | 接收每日專案／單字推送的群組 ID |
| `CARD_GROUP_ID` | 啟用名片辨識功能的群組 ID |
| `Creds2` | Google service account 憑證 JSON（字串） |
| `PUSH_SECRET` | `/push/daily`、`/push/vocab` 端點的驗證密鑰 |

## 部署

專案以 `Procfile` 搭配 `gunicorn` 啟動，可部署於 Heroku、Render 等支援 Procfile 的平台；`requirements.txt` 列出所有 Python 相依套件。

---

*此 README 由觀察 repository 原始碼（`LINE_bot.py`、`requirements.txt`、`Procfile`）整理產生，`.github/workflows` 的排程細節（觸發時間等）建議直接查看該資料夾內的 workflow 檔案確認。*
