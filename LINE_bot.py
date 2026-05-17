import os
import base64
import time
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage, FlexSendMessage
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
key_json_str = os.getenv("Creds2")
CREDENTIALS_DICT2 = json.loads(key_json_str) if key_json_str else {}
GOOGLE_SHEET_KEY = "1P56w56RVhU9Re_Q6hehLbI6eXnOZ_x-VJdLYK1_kWRE"
PUSH_SECRET = os.getenv("PUSH_SECRET", "jenny-daily-push")  # GitHub Actions 推送的密鑰

# 初始化
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_KEY)

# 🔹 對話歷史管理
MAX_HISTORY = 10          # 每個使用者最多保留 10 輪對話
HISTORY_TIMEOUT = 1800    # 30 分鐘（秒）沒互動就清空

# 格式：{ chat_id: { "messages": [...], "last_time": timestamp } }
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
    """新增一筆對話到歷史，超過上限就移除最舊的"""
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
    """通用重試機制，遇到錯誤自動重試"""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            print(f"⚠️ 第 {attempt + 1} 次嘗試失敗：{e}")
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise e


# --- 推送功能區 ---

def create_project_flex_message(projects):
    """
    根據專案清單生成 Flex Message
    projects: [{"title": "...", "level": "...", "category": "...", "description": "..."}, ...]
    """
    bubbles = []

    for project in projects[:5]:  # 最多展示 5 個專案
        title = project.get("title", "未命名")
        level = project.get("level", "")
        category = project.get("category", "")
        description = project.get("description", "")

        # 截斷長度
        desc_short = description[:100] + "..." if len(description) > 100 else description

        # 根據難度等級選擇顏色
        level_colors = {
            "初階": "#047857",
            "中階": "#1e40af",
            "高階": "#9d174d"
        }
        level_color = level_colors.get(level, "#78716c")

        bubble = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": title,
                        "weight": "bold",
                        "size": "lg",
                        "wrap": True,
                        "color": "#c2410c"
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "text",
                                        "text": level,
                                        "size": "xs",
                                        "color": "#fff",
                                        "weight": "bold",
                                        "align": "center"
                                    }
                                ],
                                "backgroundColor": level_color,
                                "paddingAll": "4px",
                                "borderRadius": "4px",
                                "width": "50px"
                            },
                            {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "text",
                                        "text": category,
                                        "size": "xs",
                                        "color": "#9a3412",
                                        "weight": "bold"
                                    }
                                ],
                                "backgroundColor": "#fef3e7",
                                "paddingAll": "4px",
                                "borderRadius": "4px"
                            }
                        ]
                    },
                    {
                        "type": "text",
                        "text": desc_short,
                        "size": "sm",
                        "color": "#78716c",
                        "wrap": True
                    }
                ]
            }
        }
        bubbles.append(bubble)

    # 總結訊息
    carousel = {
        "type": "carousel",
        "contents": bubbles
    }

    return FlexSendMessage(alt_text=f"📚 Claude Code 每日新增 {len(projects)} 個專案", contents=carousel)


def push_daily_projects(group_id, projects):
    """
    推送每日專案更新到指定群組
    """
    try:
        # 生成 Flex Message
        flex_message = create_project_flex_message(projects)

        # 推送訊息
        line_bot_api.push_message(group_id, flex_message)

        # 推送文字摘要
        summary = f"📚 Claude Code 專案靈感集 - 每日新增\n\n今日新增 {len(projects)} 個專案：\n"
        for i, p in enumerate(projects[:3], 1):
            summary += f"{i}. {p.get('title', '未命名')} ({p.get('level', '')})\n"
        if len(projects) > 3:
            summary += f"\n... 還有 {len(projects) - 3} 個專案"

        line_bot_api.push_message(group_id, TextSendMessage(text=summary))

        print(f"✅ 成功推送 {len(projects)} 個專案到群組 {group_id}")
        return True
    except Exception as e:
        print(f"❌ 推送失敗：{e}")
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
    """ChatGPT 帶上下文回覆（僅一般對話模式使用）"""
    try:
        history = get_chat_history(chat_id)
        messages = [{"role": "system", "content": "你是一個友善的 AI 助手，請用繁體中文回覆。"}]
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
    預期的 JSON 格式：
    {
        "secret": "xxx",
        "projects": [
            {
                "title": "專案名稱",
                "level": "初階",
                "category": "生產力",
                "description": "描述"
            }
        ]
    }
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


@app.route("/debug/group-id", methods=['GET'])
def debug_group_id():
    """
    除錯用：顯示當前 TARGET_GROUP_ID
    """
    return {
        "current_group_id": TARGET_GROUP_ID,
        "instruction": "當有訊息發送到群組時，會在 console 打印出 chat_id"
    }, 200


# --- 訊息事件處理 ---

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    """處理圖片訊息 (僅限特定群組)"""
    source_type = event.source.type
    chat_id = getattr(event.source, f"{source_type}_id", "UNKNOWN")
    print(f"📌 目前訊息來源 chat_id: {chat_id}")

    if source_type == "group" and chat_id == TARGET_GROUP_ID:
        send_loading_animation(chat_id, duration=10)
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = message_content.content

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="收到圖片"))


# --- LINE 訊息處理邏輯 ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_text = event.message.text.strip().replace("，", ",").replace("  ", " ")
        source_type = event.source.type
        chat_id = getattr(event.source, f"{source_type}_id", "UNKNOWN")

        print(f"📌 群組 ID：{chat_id}")

        # 顯示當前 GROUP ID (用於設定)
        if user_text == "show-group-id":
            reply_text = f"📌 此群組的 ID：{chat_id}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return

        # 一般訊息
        if source_type == "group" and chat_id == TARGET_GROUP_ID:
            reply_text = "💡 此群組用於接收 Claude Code 每日專案推送"
        else:
            send_loading_animation(chat_id, duration=10)
            reply_text = get_gpt_reply(user_text, chat_id)

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    except Exception as e:
        print(f"❌ handle_message 發生錯誤：{e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
