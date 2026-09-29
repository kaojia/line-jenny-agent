"""賣家客服 bot — 亞馬遜賣家問答，System Prompt 由 Google Sheet 動態載入。

以 Flask Blueprint 形式提供，掛載於 /customer 前綴（見 app.py）。
"""
import os
import json
import re
import time

import requests
import gspread
from flask import Blueprint, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI

# 🔹 金鑰（CUST_ 前綴，與 Jenny bot 分開）
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("CUST_LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("CUST_LINE_CHANNEL_SECRET")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

key_json_str = os.getenv("GOOGLE_SHEETS_KEY")
if key_json_str is None:
    print("⚠️ 警告：找不到 GOOGLE_SHEETS_KEY 環境變數")
    CREDS_DICT = {}
else:
    CREDS_DICT = json.loads(key_json_str)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_KEY)
BOT_TRIGGER = "@bot"

bp = Blueprint("customer", __name__)

# ✅ 快取與 FAQ
cache = {}
FAQ_RESPONSES = {
    "你好": "你好！我是Jenny 的 AI 助理，關於亞馬遜的問題歡迎詢問～",
    "幫助": "需要幫助嗎？請輸入：功能 / 教學 / 聯絡客服",
    "hello": "Hello! I'm Jenny's AI assistant. Feel free to ask anything about Amazon seller business.",
    "hi": "Hi there! I'm Jenny's AI assistant. You can ask me anything about Amazon seller topics.",
    "help": "Need help? You can type: features / tutorial / contact support.",
}

# ✅ 官方帳號已回覆的關鍵字（不需要 ChatGPT 再回覆）
OFFICIAL_HANDLED_KEYWORDS = [
    "wifi", "預約諮詢", "促銷提報", "新賣家大禮包", "全球跟賣", "註冊文件",
    "品牌授權", "倉庫位置", "出貨注意事項", "發票", "佣金", "歡迎", "品牌註冊",
]

# ✅ Google Sheets 設定
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS = ServiceAccountCredentials.from_json_keyfile_dict(CREDS_DICT, SCOPE) if CREDS_DICT else None
GCLIENT = gspread.authorize(CREDS) if CREDS else None

SHEET_NAME = "AI_Assistant_Config"


def send_loading_animation(user_id, duration=10):
    url = "https://api.line.me/v2/bot/chat/loading/start"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {"chatId": user_id, "loadingSeconds": duration}
    try:
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        print("❌ Loading Animation API 錯誤：", e)


def get_prompt_from_sheet(mode_name="default"):
    try:
        sheet = GCLIENT.open(SHEET_NAME).get_worksheet(0)
        print(f"✅ 成功連線到：{SHEET_NAME} - {sheet.title}")
        cell = sheet.find(mode_name)
        if cell:
            return sheet.cell(cell.row, 2).value
        print(f"⚠️ 找不到模式 {mode_name}，使用預設 Prompt")
        return "You are a helpful AI assistant."
    except Exception as e:
        print(f"❌ 讀取 Google Sheet 失敗: {e}")
        return "You are a helpful AI assistant."


def is_english_message(text):
    """英文比例 >50% → 視為英文"""
    letters = re.findall(r'[A-Za-z]', text)
    return len(letters) / max(len(text), 1) > 0.5


def get_gpt_reply(user_message):
    text = user_message.strip()
    text_lower = text.lower()

    system_prompt = "You are a helpful AI assistant."
    clean_text = text

    # 1. 判斷指令並設定對應的 System Prompt，同時移除指令標籤
    if "#polish" in text_lower:
        system_prompt = get_prompt_from_sheet("Polish")
        clean_text = re.sub(r'#polish', '', text, flags=re.IGNORECASE).strip()
    elif "#trans" in text_lower:
        system_prompt = get_prompt_from_sheet("Translate")
        clean_text = re.sub(r'#trans', '', text, flags=re.IGNORECASE).strip()
    elif "#biz" in text_lower:
        system_prompt = get_prompt_from_sheet("Business_Review")
        clean_text = re.sub(r'#biz', '', text, flags=re.IGNORECASE).strip()
    elif "#line" in text_lower:
        system_prompt = get_prompt_from_sheet("Line_Blurb")
        clean_text = re.sub(r'#line', '', text, flags=re.IGNORECASE).strip()
    elif "#ai" in text_lower:
        system_prompt = get_prompt_from_sheet("AI")
        clean_text = re.sub(r'#ai', '', text, flags=re.IGNORECASE).strip()
    else:
        sheet_default = get_prompt_from_sheet("default")
        if sheet_default != "You are a helpful AI assistant.":
            system_prompt = sheet_default

    # 2. FAQ 模糊匹配（有下指令時不進 FAQ）
    greetings_keywords = ["你好", "您好", "hello", "hi", "hey", "yo"]
    if not any(tag in text_lower for tag in ["#polish", "#trans", "#biz", "#line", "#ai"]):
        if (1 <= len(text) <= 5) and (any(k in text_lower for k in greetings_keywords) or any(k in text for k in ["你好", "您好"])):
            return FAQ_RESPONSES.get("你好", "你好！我是 AI 助理，歡迎詢問～")

    # 3. 快取查詢
    if text in cache:
        return cache[text]

    english_input = is_english_message(clean_text)

    # 4. 呼叫 OpenAI
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": clean_text},
                ],
                max_completion_tokens=2000,  # 推理型模型會先耗 token 推理，需留足輸出空間
            )
            reply_text = (response.choices[0].message.content or "").strip()
            if not reply_text:
                print("⚠️ 模型回傳空內容，重試")
                time.sleep(1)
                continue

            if english_input:
                reply_text += "\n\n(AI response for reference only)"
            else:
                reply_text += "\n\n(AI 回覆僅供參考)"

            cache[text] = reply_text
            return reply_text
        except Exception as e:
            print(f"❌ GPT API 錯誤（嘗試 {attempt + 1}/3）：{e}")
            time.sleep(1)

    return "⚠️ 系統繁忙，請稍後再試。"


# --- Webhook 路由（掛載後為 /customer/...）---

@bp.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_text = event.message.text.strip()
        source_type = event.source.type

        if source_type == "user":
            chat_id = event.source.user_id
        elif source_type == "group":
            chat_id = event.source.group_id
        elif source_type == "room":
            chat_id = event.source.room_id
        else:
            chat_id = "UNKNOWN"

        print(f"✅ 收到訊息：{user_text} | 來源：{source_type} | ID：{chat_id}")

        # 🟢 私聊：直接回
        if source_type == "user":
            send_loading_animation(chat_id, duration=20)
            if any(kw in user_text.lower() for kw in OFFICIAL_HANDLED_KEYWORDS):
                print("⏭️ 官方已處理訊息，跳過")
                return
            reply_text = get_gpt_reply(user_text)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return

        # 🟡 群組 / Room：只有 @bot 才回
        trigger = BOT_TRIGGER.lower()
        if trigger in user_text.lower():
            cleaned_text = re.sub(trigger, "", user_text, count=1, flags=re.IGNORECASE).strip()
            if not cleaned_text:
                print("⚠️ 只有 @bot，沒有問題內容，跳過")
                return
            print(f"🤖 群組觸發成功，問題內容：{cleaned_text}")
            reply_text = get_gpt_reply(cleaned_text)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        else:
            print("⏭️ 群組未 @bot，跳過")

    except Exception as e:
        print("❌ handle_message 發生錯誤：", e)
