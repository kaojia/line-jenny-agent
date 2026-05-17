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

# 🔹 載入環境變數
load_dotenv()

app = Flask(__name__)

# 🔹 讀取金鑰
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
TARGET_GROUP_ID = "C25afbbbc3a5a4c6d8d1083c907dea2d7"
key_json_str = os.getenv("Creds2")
CREDENTIALS_DICT2 = json.loads(key_json_str)
GOOGLE_SHEET_KEY = "1P56w56RVhU9Re_Q6hehLbI6eXnOZ_x-VJdLYK1_kWRE"

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
            model="gpt-5.4-mini",
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


def parse_update_intent(user_message):
    """用 GPT 解析自然語言修改指令，回傳 {name, column_name, new_value} 或 None"""
    try:
        response = client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[
                {
                    "role": "system",
                    "content": """你是一個資料修改意圖解析助手。使用者會用自然語言描述想修改名片資料庫中的某筆資料。
請從使用者的訊息中提取以下資訊，並以 JSON 格式回傳：
{
    "name": "要修改的人的姓名",
    "column_name": "要修改的欄位名稱",
    "new_value": "新的值"
}

可用的欄位名稱只有以下幾種：姓名、英文姓名、公司、職稱、品牌、Email、電話。
請將使用者描述的欄位對應到上述名稱，例如：
- "手機"、"電話號碼"、"手機號碼"、"聯絡電話" → "電話"
- "信箱"、"郵件"、"mail" → "Email"
- "職位"、"頭銜" → "職稱"
- "英文名"、"英文名字" → "英文姓名"

如果無法從訊息中明確判斷要修改誰、改什麼欄位、或改成什麼值，請回傳：
{"error": "缺少資訊的描述"}"""
                },
                {"role": "user", "content": user_message}
            ],
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content)
        if "error" in result:
            return None, result["error"]
        if not all(k in result for k in ("name", "column_name", "new_value")):
            return None, "無法完整解析修改內容"
        return result, None
    except Exception as e:
        print(f"❌ parse_update_intent 錯誤：{e}")
        return None, f"解析失敗：{str(e)}"


def process_business_card(image_data, chat_id):
    """名片辨識、重複檢查、儲存"""
    try:
        base64_image = base64.b64encode(image_data).decode('utf-8')

        def do_vision():
            return client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """你是一個專業的名片辨識助手。請嚴格依照 JSON 格式回傳：
                        {
                            "is_card": true,
                            "name": "中文姓名",
                            "english_name": "英文姓名",
                            "company": "公司",
                            "title": "職稱",
                            "brand":"品牌",
                            "email": "Email",
                            "phone": "電話"
                        }

                        若名片上缺少某項資訊，請填入空字串。如果圖片內容不是名片，請回傳 {"is_card": false}。"""
                    },
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]
                    }
                ],
                response_format={"type": "json_object"}
            )

        response = retry_on_error(do_vision)
        res_data = json.loads(response.choices[0].message.content)
        if not res_data.get('is_card'):
            return "⚠️ 偵測到非名片內容，已停止操作。"

        new_name = res_data.get('name', '')
        new_eng_name = res_data.get('english_name', '')
        new_company = res_data.get('company', '')
        new_brand = res_data.get('brand', '')
        new_title = res_data.get('title', '')

        def do_sheet_read():
            gc = get_gs_client()
            sheet = gc.open_by_key(GOOGLE_SHEET_KEY).sheet1
            return sheet, sheet.get_all_records()

        sheet, all_records = retry_on_error(do_sheet_read)

        for index, row in enumerate(all_records, start=2):
            if str(row.get('姓名')) == new_name and str(row.get('公司')) == new_company:
                return f"🚫 內容重複！此名片已存在於試算表第 {index} 列。"

        created_time = time.strftime("%Y/%m/%d %H:%M", time.localtime())
        row_data = [
            new_name,
            new_eng_name,
            new_company,
            new_title,
            new_brand,
            res_data.get('email', ''),
            res_data.get('phone', ''),
            created_time,
        ]

        def do_append():
            sheet.append_row(row_data)
        retry_on_error(do_append)

        return f"✅ 成功儲存！\n姓名：{new_name}\n英文姓名：{new_eng_name}\n品牌:{new_brand}\n公司：{new_company}\n資料已存至 Google Sheet。"

    except Exception as e:
        print(f"❌ process_business_card 錯誤：{e}")
        return f"處理名片時發生錯誤：{str(e)}"


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

        result_msg = process_business_card(image_data, chat_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result_msg))

    # 如果使用者輸入特定指令
    if user_text == "show-group-id":
        # 直接回覆這個群組的 ID
        reply_text = f"📌 此群組的 ID：{chat_id}"
        line_bot_api.reply_message(event.reply_token, reply_text)


# --- 工具函式：搜尋與修改 Google Sheets ---

def search_sheet_data(keyword):
    """根據姓名或公司名稱查找資料"""
    try:
        print("📡 開始查詢 Google Sheet")

        def do_read():
            gc = get_gs_client()
            sheet = gc.open_by_key(GOOGLE_SHEET_KEY).sheet1
            return sheet.get_all_records()

        all_records = retry_on_error(do_read)
        print(f"📊 讀到資料筆數: {len(all_records)}")
        results = []

        for row in all_records:
            if keyword.lower() in str(row.get('姓名', '')).lower() or \
               keyword.lower() in str(row.get('英文姓名', '')).lower() or \
               keyword.lower() in str(row.get('公司', '')).lower() or \
               keyword.lower() in str(row.get('品牌', '')).lower() or \
               keyword.lower() in str(row.get('職稱', '')).lower():
                results.append(row)

        if not results:
            return None, f"🔍 找不到與「{keyword}」相關的資料。"

        return results, None
    except Exception as e:
        return None, f"❌ 查詢發生錯誤：{e}"


def build_text_results(results, keyword):
    """將查詢結果組成純文字格式，方便複製"""
    lines = [f"✅ 找到 {len(results)} 筆「{keyword}」的名片資料：\n"]

    for i, row in enumerate(results, start=1):
        name = row.get('姓名', '') or ''
        eng_name = row.get('英文姓名', '') or ''
        company = row.get('公司', '') or ''
        title = row.get('職稱', '') or ''
        brand = row.get('品牌', '') or ''
        email = row.get('Email', '') or ''
        phone = row.get('電話', '') or ''

        card = f"【{i}】{name}"
        if eng_name:
            card += f" ({eng_name})"
        card += "\n"
        if company:
            card += f"🏢 公司：{company}\n"
        if title:
            card += f"💼 職稱：{title}\n"
        if brand:
            card += f"⭐ 品牌：{brand}\n"
        if email:
            card += f"📧 Email：{email}\n"
        if phone:
            card += f"📞 電話：{phone}\n"

        lines.append(card)

    return "\n".join(lines)


def delete_sheet_data(name, company=None):
    """刪除指定姓名的整列資料，支援多筆重複時用公司名稱區分"""
    try:
        def do_read():
            gc = get_gs_client()
            sheet = gc.open_by_key(GOOGLE_SHEET_KEY).sheet1
            return sheet, sheet.get_all_records()

        sheet, all_records = retry_on_error(do_read)

        matches = []
        for index, row in enumerate(all_records, start=2):
            if str(row.get('姓名', '')).strip() == name.strip():
                matches.append({"row": index, "data": row})

        if not matches:
            return f"❌ 找不到姓名為「{name}」的資料，請確認輸入是否正確。"

        if company:
            filtered = [m for m in matches if company.strip() in str(m["data"].get('公司', '')).strip()]
            if not filtered:
                return f"❌ 找不到姓名為「{name}」且公司包含「{company}」的資料。"
            if len(filtered) == 1:
                target = filtered[0]
                sheet.delete_rows(target["row"])
                return (f"🗑️ 已刪除資料：\n"
                        f"👤 姓名：{target['data'].get('姓名', '')}\n"
                        f"🏢 公司：{target['data'].get('公司', '')}")
            else:
                matches = filtered

        if len(matches) == 1:
            target = matches[0]
            sheet.delete_rows(target["row"])
            return (f"🗑️ 已刪除資料：\n"
                    f"👤 姓名：{target['data'].get('姓名', '')}\n"
                    f"🏢 公司：{target['data'].get('公司', '')}")

        result_lines = [f"⚠️ 找到 {len(matches)} 筆「{name}」的資料，請指定更精確的條件：\n"]
        for i, m in enumerate(matches, start=1):
            d = m["data"]
            result_lines.append(
                f"【{i}】{d.get('姓名', '')}｜{d.get('公司', '')}｜{d.get('職稱', '')}｜{d.get('品牌', '')}"
            )
        result_lines.append(f"\n💡 請加上公司名稱，例如：\n刪除 {name}，公司是XXX的那個")
        return "\n".join(result_lines)

    except Exception as e:
        return f"❌ 刪除失敗：{e}"


def batch_delete_sheet_data(keyword, field):
    """批次刪除：根據條件刪除所有符合的資料"""
    try:
        def do_read():
            gc = get_gs_client()
            sheet = gc.open_by_key(GOOGLE_SHEET_KEY).sheet1
            return sheet, sheet.get_all_records()

        sheet, all_records = retry_on_error(do_read)

        matches = []
        for index, row in enumerate(all_records, start=2):
            if keyword.strip().lower() in str(row.get(field, '')).strip().lower():
                matches.append({"row": index, "data": row})

        if not matches:
            return f"❌ 找不到{field}包含「{keyword}」的資料。"

        deleted_count = 0
        for m in reversed(matches):
            try:
                sheet.delete_rows(m["row"])
                deleted_count += 1
            except Exception as e:
                print(f"⚠️ 刪除第 {m['row']} 列失敗：{e}")

        return f"🗑️ 批次刪除完成！\n共刪除 {deleted_count} 筆{field}包含「{keyword}」的資料。"

    except Exception as e:
        return f"❌ 批次刪除失敗：{e}"


def parse_delete_intent(user_message):
    """用 GPT 解析自然語言刪除指令，支援單筆和批次刪除"""
    try:
        response = client.chat.completions.create(
            model="gpt-5.4-mini",
            messages=[
                {
                    "role": "system",
                    "content": """你是一個資料刪除意圖解析助手。使用者會用自然語言描述想刪除名片資料庫中的資料。
請判斷是「單筆刪除」還是「批次刪除」，並以 JSON 格式回傳。

單筆刪除（刪除特定一個人）：
{"mode": "single", "name": "姓名", "company": "公司名稱（沒提到就填空字串）"}

批次刪除（刪除符合某條件的所有資料）：
{"mode": "batch", "keyword": "關鍵字", "field": "欄位名稱"}

可用的欄位名稱：姓名、英文姓名、公司、職稱、品牌、Email、電話。

範例：
- "把王小明的資料刪掉" → {"mode": "single", "name": "王小明", "company": ""}
- "刪除Jenny，公司是Google的" → {"mode": "single", "name": "Jenny", "company": "Google"}
- "刪除所有Amazon的名片" → {"mode": "batch", "keyword": "Amazon", "field": "公司"}
- "把品牌是Nike的全部刪掉" → {"mode": "batch", "keyword": "Nike", "field": "品牌"}
- "刪除所有PM的資料" → {"mode": "batch", "keyword": "PM", "field": "職稱"}

如果無法判斷，請回傳：
{"error": "無法判斷要刪除的對象"}"""
                },
                {"role": "user", "content": user_message}
            ],
            response_format={"type": "json_object"}
        )
        result = json.loads(response.choices[0].message.content)
        if "error" in result:
            return None, result["error"]
        return result, None
    except Exception as e:
        print(f"❌ parse_delete_intent 錯誤：{e}")
        return None, f"解析失敗：{str(e)}"


def update_sheet_data(name, column_name, new_value):
    """修改指定姓名的欄位資訊（含重試機制）"""
    try:
        def do_update():
            gc = get_gs_client()
            sheet = gc.open_by_key(GOOGLE_SHEET_KEY).sheet1
            headers = sheet.row_values(1)

            if column_name not in headers:
                raise ValueError(f"找不到欄位「{column_name}」")

            col_index = headers.index(column_name) + 1
            cell = sheet.find(name)

            if cell:
                sheet.update_cell(cell.row, col_index, new_value)
                return f"✨ 修改成功！\n已將「{name}」的【{column_name}】更新為：{new_value}"
            else:
                return f"❌ 找不到姓名為「{name}」的資料，請確認輸入是否正確。"

        return retry_on_error(do_update)
    except ValueError as e:
        return f"❌ {e}，請確認標題是否為：姓名、英文姓名、公司、職稱、品牌、Email、電話。"
    except Exception as e:
        return f"❌ 修改失敗：{e}"


# --- LINE 訊息處理邏輯 ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_text = event.message.text.strip().replace("，", ",").replace("  ", " ")
        source_type = event.source.type
        chat_id = getattr(event.source, f"{source_type}_id", "UNKNOWN")

        # 1. 🔍 查詢模式
        if user_text.startswith("查詢"):
            keyword = user_text.replace("查詢", "").strip()
            print(f"🔍 查詢關鍵字: {keyword}")
            if keyword:
                send_loading_animation(chat_id, duration=5)
                results, error_msg = search_sheet_data(keyword)
                if results:
                    reply_text = build_text_results(results, keyword)
                else:
                    reply_text = error_msg
            else:
                reply_text = "💡 請輸入關鍵字，例如：\n查詢 Jenny\n查詢 Amazon"

        # 2. 📝 修改模式 (自然語言)
        elif user_text.startswith("修改"):
            raw_text = user_text.replace("修改", "").strip()
            if not raw_text:
                reply_text = "💡 請描述你要修改的內容，例如：\n修改 把高嘉彣的電話改成0912345678\n修改 Jenny的公司改為Google"
            else:
                send_loading_animation(chat_id, duration=10)
                parsed, error_msg = parse_update_intent(raw_text)
                if parsed:
                    reply_text = update_sheet_data(parsed["name"], parsed["column_name"], parsed["new_value"])
                else:
                    reply_text = f"⚠️ 無法理解修改指令：{error_msg}\n\n💡 請試著說清楚要改誰的什麼資料，例如：\n修改 把高嘉彣的電話改成0912345678"

        # 3. 🗑️ 刪除模式 (自然語言，支援單筆與批次)
        elif user_text.startswith("刪除"):
            raw_text = user_text.replace("刪除", "").strip()
            if not raw_text:
                reply_text = "💡 請描述你要刪除的對象，例如：\n刪除 王小明\n刪除 把Jenny的資料刪掉\n刪除 所有Amazon的名片"
            else:
                send_loading_animation(chat_id, duration=10)
                parsed, error_msg = parse_delete_intent(raw_text)
                if parsed:
                    mode = parsed.get("mode", "single")
                    if mode == "batch":
                        keyword = parsed.get("keyword", "")
                        field = parsed.get("field", "公司")
                        reply_text = batch_delete_sheet_data(keyword, field)
                    else:
                        company = parsed.get("company", "").strip() or None
                        reply_text = delete_sheet_data(parsed.get("name", ""), company)
                else:
                    reply_text = f"⚠️ 無法理解刪除指令：{error_msg}\n\n💡 請試著說清楚要刪除誰的資料，例如：\n刪除 王小明\n刪除 所有Amazon的名片"

        # 4. 🤖 一般對話模式 (帶上下文的 ChatGPT)
        else:
            if source_type == "group" and chat_id == TARGET_GROUP_ID:
                reply_text = "💡 本群組僅支援以下指令：\n\n🔍 查詢 關鍵字\n📝 修改 自然語言描述\n🗑️ 刪除 自然語言描述\n\n範例：\n查詢 Jenny\n修改 把Jenny的電話改成0912345678\n刪除 王小明"
            else:
                send_loading_animation(chat_id, duration=10)
                reply_text = get_gpt_reply(user_text, chat_id)

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    except Exception as e:
        print(f"❌ handle_message 發生錯誤：{e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=500)
