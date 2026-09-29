"""Jenny 個人助理 bot — 帶上下文 GPT 聊天、名片辨識、專案/新聞/單字推送。

以 Flask Blueprint 形式提供，掛載於 /jenny 前綴（見 app.py）。
"""
import os
import re
import base64
import time
import json
from datetime import datetime

import requests
import gspread
from flask import Blueprint, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage
from oauth2client.service_account import ServiceAccountCredentials
from openai import OpenAI

# 🔹 金鑰（優先讀 JENNY_ 前綴，向下相容舊變數名）
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("JENNY_LINE_CHANNEL_ACCESS_TOKEN") or os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("JENNY_LINE_CHANNEL_SECRET") or os.getenv("LINE_CHANNEL_SECRET")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TARGET_GROUP_ID = os.getenv("TARGET_GROUP_ID", "C25afbbbc3a5a4c6d8d1083c907dea2d7")
CARD_GROUP_ID = os.getenv("CARD_GROUP_ID", "C25afbbbc3a5a4c6d8d1083c907dea2d7")
key_json_str = os.getenv("Creds2")
CREDENTIALS_DICT2 = json.loads(key_json_str) if key_json_str else {}
GOOGLE_SHEET_KEY = "1P56w56RVhU9Re_Q6hehLbI6eXnOZ_x-VJdLYK1_kWRE"
PUSH_SECRET = os.getenv("PUSH_SECRET", "jenny-daily-push")
MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_KEY)

bp = Blueprint("jenny", __name__)

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
    fields = {"姓名": "", "公司": "", "職稱": "", "電話": "", "手機": "", "Email": "", "地址": "", "網站": "", "備註": ""}
    for line in card_text.split("\n"):
        line = line.strip()
        for key in fields:
            if line.startswith(f"{key}：") or line.startswith(f"{key}:"):
                fields[key] = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                break

    def _write():
        gc = get_gs_client()
        sh = gc.open_by_key(GOOGLE_SHEET_KEY)
        try:
            ws = sh.worksheet("名片")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="名片", rows=1000, cols=10)
            ws.append_row(["日期", "姓名", "公司", "職稱", "電話", "手機", "Email", "地址", "網站", "備註"])

        row = [
            datetime.now().strftime("%Y/%m/%d %H:%M"),
            fields["姓名"], fields["公司"], fields["職稱"], fields["電話"],
            fields["手機"], fields["Email"], fields["地址"], fields["網站"], fields["備註"],
        ]
        ws.append_row(row)

    retry_on_error(_write)


# --- 推送功能區 ---

def push_daily_projects(group_id, projects):
    """推送每日專案更新到指定群組（單一通知）"""
    try:
        message_text = "📚 Claude Code 專案靈感集 - 每日新增\n"
        message_text += f"📅 {datetime.now().strftime('%Y/%m/%d')}\n"
        message_text += f"✨ 今日新增 {len(projects)} 個專案\n"
        message_text += "=" * 40 + "\n\n"

        for i, project in enumerate(projects, 1):
            title = project.get("title", "未命名")
            level = project.get("level", "")
            category = project.get("category", "")
            description = project.get("description", "")
            level_emoji = {"初階": "🟢", "中階": "🟡", "高階": "🔴"}.get(level, "⭕")

            message_text += f"{i}. {level_emoji} {title}\n"
            message_text += f"   難度：{level} | 分類：{category}\n"
            if description:
                message_text += f"   {description}\n"
            message_text += "\n"

        message_text += "=" * 40 + "\n"
        message_text += "👉 查看完整列表：\n"
        message_text += "https://kaojia.github.io/claude-code-inspirations/\n\n"
        message_text += "💡 點擊左側欄「分類」可篩選專案\n"
        message_text += "🔍 使用搜尋功能尋找感興趣的專案"

        line_bot_api.push_message(group_id, TextSendMessage(text=message_text))
        print(f"✅ 成功推送 {len(projects)} 個專案到群組 {group_id}（單一通知）")
        return True
    except Exception as e:
        print(f"❌ 推送失敗：{e}")
        return False


def push_market_news(group_id, news_items):
    """推送市場新聞到指定群組（純文字格式）"""
    try:
        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}

        message_text = "📰 AU/MENA 市場快報 - 每日精選\n"
        message_text += f"📅 {datetime.now().strftime('%Y/%m/%d')}\n"
        message_text += f"✨ 今日精選 {len(news_items)} 則新聞\n"
        message_text += "=" * 40 + "\n\n"

        for i, item in enumerate(news_items[:5], 1):
            title = item.get("title", "").strip() or "未命名"
            category = item.get("category", "").strip() or "新聞"
            priority = item.get("priority", "low")
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
        "Content-Type": "application/json",
    }
    data = {"chatId": chat_id, "loadingSeconds": duration}
    try:
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        print(f"❌ Loading API 錯誤：{e}")


SYSTEM_PROMPT = (
    "你是一個友善的 AI 助手。"
    "請一律使用與使用者訊息相同的語言回覆："
    "使用者用英文就用英文回、用中文就用中文回、用日文就用日文回，不要自行切換語言。"
    "回覆時務必使用純文字，不要使用任何 Markdown 語法——"
    "不要用 **粗體**、*斜體*、`程式碼`、# 標題、> 引用或 [] () 連結語法，"
    "因為 LINE 無法顯示這些符號，會直接出現原始星號。"
    "需要條列時用「1. 」數字或「・」符號，不要用 - 或 *。"
)

# 移除模型可能殘留的 Markdown 標記（LINE 不支援）
_MD_BOLD_ITALIC = re.compile(r'(\*{1,3}|_{1,3})(.+?)\1', re.DOTALL)
_MD_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+', re.MULTILINE)
_MD_BULLET = re.compile(r'^(\s*)[-*+]\s+', re.MULTILINE)
_MD_INLINE_CODE = re.compile(r'`([^`]*)`')
_MD_CODE_FENCE = re.compile(r'```[a-zA-Z0-9]*\n?')


def strip_markdown(text):
    """把常見 Markdown 標記轉成 LINE 可讀的純文字。"""
    text = _MD_CODE_FENCE.sub('', text)
    text = _MD_INLINE_CODE.sub(r'\1', text)
    text = _MD_BOLD_ITALIC.sub(r'\2', text)
    text = _MD_HEADING.sub('', text)
    text = _MD_BULLET.sub(r'\1・', text)
    return text.strip()


def get_gpt_reply(user_message, chat_id):
    """ChatGPT 帶上下文回覆"""
    try:
        history = get_chat_history(chat_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_completion_tokens=2000,  # 推理型模型會先耗 token 推理，需留足輸出空間
        )
        reply = strip_markdown((response.choices[0].message.content or "").strip())
        if not reply:
            print("⚠️ 模型回傳空內容，使用 fallback")
            return "抱歉，我這次沒能生出回覆，請換個說法再問一次 🙏"

        add_to_history(chat_id, "user", user_message)
        add_to_history(chat_id, "assistant", reply)
        return reply
    except Exception as e:
        print(f"❌ ChatGPT API 錯誤：{e}")
        return "系統發生錯誤，請稍後再試。"


# --- Webhook 路由（掛載後為 /jenny/...）---

@bp.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@bp.route("/push/daily", methods=['POST'])
def push_daily():
    try:
        data = request.get_json()
        if data.get("secret") != PUSH_SECRET:
            return {"status": "error", "message": "Invalid secret"}, 401
        projects = data.get("projects", [])
        if not projects:
            return {"status": "error", "message": "No projects provided"}, 400
        success = push_daily_projects(TARGET_GROUP_ID, projects)
        if success:
            return {"status": "success", "message": f"Pushed {len(projects)} projects"}, 200
        return {"status": "error", "message": "Failed to push message"}, 500
    except Exception as e:
        print(f"❌ /push/daily 錯誤：{e}")
        return {"status": "error", "message": str(e)}, 500


@bp.route("/push/news", methods=['POST'])
def push_news():
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
        return {"status": "error", "message": "Failed to push message"}, 500
    except Exception as e:
        print(f"❌ /push/news 錯誤：{e}")
        return {"status": "error", "message": str(e)}, 500


@bp.route("/push/vocab", methods=['POST'])
def push_vocab():
    try:
        data = request.get_json()
        if data.get("secret") != PUSH_SECRET:
            return {"status": "error", "message": "Invalid secret"}, 401
        message = data.get("message", "")
        if not message:
            return {"status": "error", "message": "No message provided"}, 400
        line_bot_api.push_message(TARGET_GROUP_ID, TextSendMessage(text=message))
        print(f"✅ 成功推送 N1 單字到群組 {TARGET_GROUP_ID}")
        return {"status": "success", "message": "Vocab pushed"}, 200
    except Exception as e:
        print(f"❌ /push/vocab 錯誤：{e}")
        return {"status": "error", "message": str(e)}, 500


@bp.route("/debug/group-id", methods=['GET'])
def debug_group_id():
    return {
        "current_group_id": TARGET_GROUP_ID,
        "instruction": "當有訊息發送到群組時，會在 console 打印出 chat_id",
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
            message_id = event.message.id
            print(f"📷 下載圖片 message_id={message_id}")
            image_content = line_bot_api.get_message_content(message_id)
            image_bytes = b""
            for chunk in image_content.iter_content():
                image_bytes += chunk
            image_base64 = base64.b64encode(image_bytes).decode("utf-8")

            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是名片辨識助手。請從圖片中擷取名片資訊，"
                            "以下列格式回覆（若無該欄位請留空）：\n"
                            "姓名：\n公司：\n職稱：\n電話：\n手機：\nEmail：\n地址：\n網站：\n備註：\n\n"
                            "如果圖片不是名片，請回覆「這不是名片」。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "請辨識這張名片的內容"},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                        ],
                    },
                ],
                max_completion_tokens=2000,  # 推理型模型會先耗 token 推理，需留足輸出空間
            )
            result = (response.choices[0].message.content or "").strip()
            if not result:
                result = "⚠️ 辨識結果為空，請重拍清楚一點再試。"

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
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 圖片辨識失敗，請稍後再試。"))
    else:
        print(f"⚠️ 條件未通過：source_type={source_type}, chat_id={chat_id}, CARD_GROUP={CARD_GROUP_ID}")


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_text = event.message.text.strip().replace("，", ",").replace("  ", " ")
        source_type = event.source.type
        chat_id = getattr(event.source, f"{source_type}_id", "UNKNOWN")
        print(f"📌 群組 ID：{chat_id}")

        if user_text == "show-group-id":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📌 此群組的 ID：{chat_id}"))
            return

        if source_type == "group" and chat_id == TARGET_GROUP_ID:
            reply_text = "💡 此群組用於接收 Claude Code 每日專案推送\n\n👉 查看網站：https://kaojia.github.io/claude-code-inspirations/"
        else:
            send_loading_animation(chat_id, duration=10)
            reply_text = get_gpt_reply(user_text, chat_id)

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        print(f"❌ handle_message 發生錯誤：{e}")
