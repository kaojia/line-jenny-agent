import os
import base64
import time
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
from openai import OpenAI
from dotenv import load_dotenv
import gspread
import json
from google.oauth2.service_account import Credentials
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# 🔹 載入環境變數
load_dotenv()

app = Flask(__name__)

# 🔹 讀取金鑰
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID", "C25afbbbc3a5a4c6d8d1083c907dea2d7")
CARD_GROUP_ID = os.getenv("CARD_GROUP_ID", "C25afbbbc3a5a4c6d8d1083c907dea2d7")
key_json_str = os.getenv("Creds2")
CREDENTIALS_DICT2 = json.loads(key_json_str) if key_json_str else {}
GOOGLE_SHEET_KEY = "1P56w56RVhU9Re_Q6hehLbI6eXnOZ_x-VJdLYK1_kWRE"
PUSH_SECRET = os.getenv("PUSH_SECRET", "jenny-daily-push")

# 初始化
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_KEY)

# 🔹 對話歷史管理
MAX_HISTORY = 10
HISTORY_TIMEOUT = 1800

conversation_history = {}


def get_chat_history(chat_id):
    """取得對話歷史，超過 30 分鐘自動清空"""
    now = time.time()
    if chat_id in conversation_history:
        last_time = conversation_history[chat_id]["last_time"]
        if now - last_time > HISTORY_TIMEOUT:
            del conversation_history[chat_id]
            return []
        return conversation_history[chat_id]["messages"]
    return []


def add_to_history(chat_id, role, content):
    """新增一筆對話到歷史"""
    now = time.time()
    if chat_id not in conversation_history:
        conversation_history[chat_id] = {"messages": [], "last_time": now}

    conversation_history[chat_id]["messages"].append({"role": role, "content": content})
    conversation_history[chat_id]["last_time"] = now

    while len(conversation_history[chat_id]["messages"]) > MAX_HISTORY * 2:
        conversation_history[chat_id]["messages"].pop(0)


def get_gs_client():
    SCOPE = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(CREDENTIALS_DICT2, SCOPE)
    return gspread.authorize(creds)


def retry_on_error(func, max_retries=3, delay=2):
    """通用重試機制"""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            print(f"⚠️ 第 {attempt + 1} 次嘗試失敗：{e}")
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise e


# --- 名片寫入 Google Sheet ---

def save_card_to_sheet(card_text):
    """解析名片辨識結果並寫入 Google Sheet"""
    # 解析欄位
    fields = {"姓名": "", "公司": "", "職稱": "", "電話": "", "手機": "", "Email": "", "地址": "", "網站": "", "備註": ""}
    for line in card_text.split("\n"):
        line = line.strip()
        for key in fields:
            if line.startswith(f"{key}：") or line.startswith(f"{key}:"):
                fields[key] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                break

    # 寫入 Google Sheet
    def _write():
        gc = get_gs_client()
        sh = gc.open_by_key(GOOGLE_SHEET_KEY)
        # 嘗試開啟「名片」工作表，若不存在則建立
        try:
            ws = sh.worksheet("名片")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="名片", rows=1000, cols=10)
            ws.append_row(["日期", "姓名", "公司", "職稱", "電話", "手機", "Email", "地址", "網站", "備註"])

        row = [
            datetime.now().strftime("%Y/%m/%d %H:%M"),
            fields["姓名"],
            fields["公司"],
            fields["職稱"],
            fields["電話"],
            fields["手機"],
            fields["Email"],
            fields["地址"],
            fields["網站"],
            fields["備註"]
        ]
        ws.append_row(row)

    retry_on_error(_write)


# --- 推送功能區 ---

def push_daily_projects(group_id, projects):
    """
    推送每日專案更新到指定群組（單一通知）
    """
    try:
        # 構建單一推送訊息
        message_text = "📚 Claude Code 專案靈感集 - 每日新增\n"
        message_text += f"📅 {datetime.now().strftime('%Y/%m/%d')}\n"
        message_text += f"✨ 今日新增 {len(projects)} 個專案\n"
        message_text += "=" * 40 + "\n\n"

        # 逐個專案加入訊息
        for i, project in enumerate(projects, 1):
            title = project.get("title", "未命名")
            level = project.get("level", "")
            category = project.get("category", "")
            description = project.get("description", "")

            # 難度等級符號
            level_emoji = {
                "初階": "🟢",
                "中階": "🟡",
                "高階": "🔴"
            }.get(level, "⭕")

            # 構建訊息
            message_text += f"{i}. {level_emoji} {title}\n"
            message_text += f"   難度：{level} | 分類：{category}\n"
            if description:
                message_text += f"   {description}\n"
            message_text += "\n"

        # 加入網頁連結
        message_text += "=" * 40 + "\n"
        message_text += "👉 查看完整列表：\n"
        message_text += "https://kaojia.github.io/claude-code-inspirations/\n\n"
        message_text += "💡 點擊左側欄「分類」可篩選專案\n"
        message_text += "🔍 使用搜尋功能尋找感興趣的專案"

        # 一次推送整個訊息
        line_bot_api.push_message(group_id, TextSendMessage(text=message_text))

        print(f"✅ 成功推送 {len(projects)} 個專案到群組 {group_id}（單一通知）")
        return True

    except Exception as e:
        print(f"❌ 推送失敗：{e}")
        return False


def push_market_news(group_id, news_items):
    """
    推送市場新聞到指定群組（純文字格式）
    """
    try:
        priority_emoji = {
            "high": "🔴",
            "medium": "🟡",
            "low": "🟢",
        }

        message_text = "📰 AU/MENA 市場快報 - 每日精選\n"
        message_text += f"📅 {datetime.now().strftime('%Y/%m/%d')}\n"
        message_text += f"✨ 今日精選 {len(news_items)} 則新聞\n"
        message_text += "=" * 40 + "\n\n"

        for i, item in enumerate(news_items[:3], 1):
            title = item.get("title", "").strip() or "未命名"
            summary = item.get("summary", "").strip()
            category = item.get("category", "").strip() or "新聞"
            priority = item.get("priority", "low")
            source_url = item.get("source_url", "").strip()
            marketplace = item.get("marketplace", "").strip()

            emoji = priority_emoji.get(priority, "🟢")
            mp_str = f" | 站點：{marketplace}" if marketplace else ""

            message_text += f"{i}. {emoji} {title}\n"
            message_text += f"   分類：{category}{mp_str}\n"
            message_text += "\n"

        message_text += "=" * 40 + "\n"
        message_text += "👉 查看完整新聞：\n"
        message_text += "https://kaojia.github.io/amazon-market-news-aumena/\n\n"
        message_text += "🔍 支援分類篩選與全文搜尋"

        line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
        print(f"Successfully pushed {len(news_items)} news items to group {group_id}")
        return True

    except Exception as e:
        import traceback
        print(f"Market news push failed: {e}")
        traceback.print_exc()
        return False


# --- 功能函式區 ---

def send_loading_animation(chat_id, duration=20):
    """觸發 LINE Loading 動畫"""
    url = "https://api.line.me/v2/bot/chat/loading/start"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {"chatId": chat_id, "loadingSeconds": duration}
    try:
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        print(f"❌ Loading API 錯誤：{e}")


def get_gpt_reply(user_message, chat_id):
    """ChatGPT 帶上下文回覆"""
    try:
        history = get_chat_history(chat_id)
        messages = [{"role": "system", "content": "你是一個友善的 AI 助手。"}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_completion_tokens=500
        )
        reply = response.choices[0].message.content.strip()

        add_to_history(chat_id, "user", user_message)
        add_to_history(chat_id, "assistant", reply)

        return reply
    except Exception as e:
        print(f"❌ ChatGPT API 錯誤：{e}")
        return "系統發生錯誤，請稍後再試。"


# --- Webhook 路由 ---

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@app.route("/push/daily", methods=['POST'])
def push_daily():
    """
    接收 GitHub Actions 的推送請求
    """
    try:
        data = request.get_json()

        # 驗證密鑰
        if data.get("secret") != PUSH_SECRET:
            return {"status": "error", "message": "Invalid secret"}, 401

        projects = data.get("projects", [])
        if not projects:
            return {"status": "error", "message": "No projects provided"}, 400

        # 推送到目標群組
        success = push_daily_projects(TARGET_GROUP_ID, projects)

        if success:
            return {"status": "success", "message": f"Pushed {len(projects)} projects"}, 200
        else:
            return {"status": "error", "message": "Failed to push message"}, 500

    except Exception as e:
        print(f"❌ /push/daily 錯誤：{e}")
        return {"status": "error", "message": str(e)}, 500


@app.route("/push/news", methods=['POST'])
def push_news():
    """
    接收 GitHub Actions 的市場新聞推送請求
    Payload: { "secret": "...", "news": [{ "title", "summary", "category", "priority", "source_url", "source_name", "marketplace" }] }
    """
    try:
        data = request.get_json()

        if data.get("secret") != PUSH_SECRET:
            return {"status": "error", "message": "Invalid secret"}, 401

        news_items = data.get("news", [])
        if not news_items:
            return {"status": "error", "message": "No news provided"}, 400

        success = push_market_news(TARGET_GROUP_ID, news_items)

        if success:
            return {"status": "success", "message": f"Pushed {len(news_items)} news items"}, 200
        else:
            return {"status": "error", "message": "Failed to push message"}, 500

    except Exception as e:
        print(f"❌ /push/news 錯誤：{e}")
        return {"status": "error", "message": str(e)}, 500


@app.route("/push/vocab", methods=['POST'])
def push_vocab():
    """
    接收 GitHub Actions 的 N1 單字推送請求
    """
    try:
        data = request.get_json()

        # 驗證密鑰
        if data.get("secret") != PUSH_SECRET:
            return {"status": "error", "message": "Invalid secret"}, 401

        message = data.get("message", "")
        if not message:
            return {"status": "error", "message": "No message provided"}, 400

        # 推送到目標群組
        line_bot_api.push_message(TARGET_GROUP_ID, TextSendMessage(text=message))
        print(f"✅ 成功推送 N1 單字到群組 {TARGET_GROUP_ID}")
        return {"status": "success", "message": "Vocab pushed"}, 200

    except Exception as e:
        print(f"❌ /push/vocab 錯誤：{e}")
        return {"status": "error", "message": str(e)}, 500


@app.route("/debug/group-id", methods=['GET'])
def debug_group_id():
    """除錯用：顯示當前 TARGET_GROUP_ID"""
    return {
        "current_group_id": TARGET_GROUP_ID,
        "instruction": "當有訊息發送到群組時，會在 console 打印出 chat_id"
    }, 200


# --- 訊息事件處理 ---

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    """處理圖片訊息 — 名片辨識"""
    source_type = event.source.type
    chat_id = getattr(event.source, f"{source_type}_id", "UNKNOWN")
    print(f"📌 目前訊息來源 chat_id: {chat_id}")

    print(f"📌 比對：chat_id={chat_id}, CARD_GROUP={CARD_GROUP_ID}, match={chat_id == CARD_GROUP_ID}")

    if source_type == "group" and chat_id == CARD_GROUP_ID:
        print("✅ 條件通過，開始辨識名片...")
        send_loading_animation(chat_id, duration=20)

        try:
            # 1. 下載圖片
            message_id = event.message.id
            print(f"📷 下載圖片 message_id={message_id}")
            image_content = line_bot_api.get_message_content(message_id)
            image_bytes = b""
            for chunk in image_content.iter_content():
                image_bytes += chunk
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")

            # 2. 用 GPT-4o 辨識名片
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是名片辨識助手。請從圖片中擷取名片資訊，"
                            "以下列格式回覆（若無該欄位請留空）：\n"
                            "姓名：\n公司：\n職稱：\n電話：\n手機：\nEmail：\n地址：\n網站：\n備註：\n\n"
                            "如果圖片不是名片，請回覆「這不是名片」。"
                        )
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "請辨識這張名片的內容"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                max_completion_tokens=800
            )
            result = response.choices[0].message.content.strip()

            # 3. 若辨識成功，寫入 Google Sheet
            if "這不是名片" not in result:
                try:
                    save_card_to_sheet(result)
                    result += "\n\n✅ 已儲存至 Google Sheet"
                except Exception as e:
                    print(f"❌ 寫入 Google Sheet 失敗：{e}")
                    result += "\n\n⚠️ 儲存失敗，請稍後再試"

            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))

        except Exception as e:
            print(f"❌ 名片辨識錯誤：{e}")
            import traceback
            traceback.print_exc()
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 圖片辨識失敗，請稍後再試。")
            )
    else:
        print(f"⚠️ 條件未通過：source_type={source_type}, chat_id={chat_id}, CARD_GROUP={CARD_GROUP_ID}")


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_text = event.message.text.strip().replace("，", ",").replace("  ", " ")
        source_type = event.source.type
        chat_id = getattr(event.source, f"{source_type}_id", "UNKNOWN")

        print(f"📌 群組 ID：{chat_id}")

        # 顯示當前 GROUP ID
        if user_text == "show-group-id":
            reply_text = f"📌 此群組的 ID：{chat_id}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return

        # 一般訊息
        if source_type == "group" and chat_id == TARGET_GROUP_ID:
            reply_text = "💡 此群組用於接收 Claude Code 每日專案推送\n\n👉 查看網站：https://kaojia.github.io/claude-code-inspirations/"
        else:
            send_loading_animation(chat_id, duration=10)
            reply_text = get_gpt_reply(user_text, chat_id)

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    except Exception as e:
        print(f"❌ handle_message 發生錯誤：{e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
