"""
Notion N1 單字抽取腳本
從 Notion 資料庫隨機抽取 10 個 N1 單字，推送到 LINE Bot
"""
import os
import json
import random
import requests

# Notion API 設定
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

# LINE Bot 推送設定
RENDER_DEPLOY_URL = os.environ.get("RENDER_DEPLOY_URL")
PUSH_SECRET = os.environ.get("PUSH_SECRET")


def query_notion_database(start_cursor=None):
    """查詢 Notion 資料庫，取得所有單字"""
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    payload = {"page_size": 100}
    if start_cursor:
        payload["start_cursor"] = start_cursor

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_all_vocab():
    """取得 Notion 資料庫中所有單字"""
    all_results = []
    has_more = True
    start_cursor = None

    while has_more:
        data = query_notion_database(start_cursor)
        all_results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return all_results


def extract_vocab_info(page):
    """
    從 Notion page 提取單字資訊
    請根據你的 Notion 資料庫欄位名稱調整 property name
    """
    props = page.get("properties", {})

    def get_title(prop):
        """取得 title 類型的值"""
        items = prop.get("title", [])
        return items[0]["plain_text"] if items else ""

    def get_rich_text(prop):
        """取得 rich_text 類型的值"""
        items = prop.get("rich_text", [])
        return items[0]["plain_text"] if items else ""

    # ============================================================
    # ⚠️ 請根據你的 Notion 資料庫欄位名稱修改以下 key
    # 常見欄位名稱範例：
    #   單字: "単語", "単字", "Word", "Name"
    #   讀音: "読み", "読み方", "Reading", "ふりがな"
    #   意思: "意味", "中文", "Meaning", "翻訳"
    #   例句: "例文", "Sentence", "Example"
    #   例句翻譯: "例文翻訳", "Translation", "中文翻訳"
    # ============================================================

    vocab = {
        "word": "",       # 日文單字
        "reading": "",    # 讀音（假名）
        "meaning": "",    # 中文意思
        "sentence": "",   # 例句
        "sentence_zh": "" # 例句中文翻譯
    }

    # 嘗試常見的欄位名稱
    for key, candidates in {
        "word": ["単語", "単字", "Word", "Name", "名前"],
        "reading": ["読み", "読み方", "Reading", "ふりがな", "かな"],
        "meaning": ["意味", "中文", "Meaning", "翻訳", "中文意思"],
        "sentence": ["例文", "Sentence", "Example", "例句"],
        "sentence_zh": ["例文翻訳", "Translation", "中文翻訳", "例句翻譯", "例句中文"],
    }.items():
        for candidate in candidates:
            if candidate in props:
                prop = props[candidate]
                prop_type = prop.get("type", "")
                if prop_type == "title":
                    vocab[key] = get_title(prop)
                elif prop_type == "rich_text":
                    vocab[key] = get_rich_text(prop)
                break

    return vocab


def format_vocab_message(vocab_list, session="上午"):
    """格式化單字推送訊息"""
    now_date = __import__("datetime").datetime.now().strftime("%Y/%m/%d")

    msg = f"📖 N1 單字 — {session}學習\n"
    msg += f"📅 {now_date}\n"
    msg += "━" * 30 + "\n\n"

    for i, v in enumerate(vocab_list, 1):
        word = v.get("word", "—")
        reading = v.get("reading", "")
        meaning = v.get("meaning", "")
        sentence = v.get("sentence", "")
        sentence_zh = v.get("sentence_zh", "")

        msg += f"【{i}】{word}"
        if reading:
            msg += f"（{reading}）"
        msg += "\n"

        if meaning:
            msg += f"   💡 {meaning}\n"
        if sentence:
            msg += f"   📝 {sentence}\n"
        if sentence_zh:
            msg += f"   🔄 {sentence_zh}\n"
        msg += "\n"

    msg += "━" * 30 + "\n"
    msg += "💪 加油！每天進步一點點"

    return msg


def push_to_line(message):
    """推送訊息到 LINE Bot"""
    payload = {
        "secret": PUSH_SECRET,
        "message": message
    }

    response = requests.post(
        f"{RENDER_DEPLOY_URL}/push/vocab",
        json=payload,
        timeout=30
    )

    print(f"推送狀態碼：{response.status_code}")
    print(f"回應：{response.text}")

    if response.status_code != 200:
        raise Exception(f"推送失敗：{response.status_code} - {response.text}")

    return True


def main():
    """主程式"""
    session = os.environ.get("VOCAB_SESSION", "上午")

    print(f"📚 開始抽取 N1 單字（{session}）...")

    # 1. 從 Notion 取得所有單字
    print("🔍 正在查詢 Notion 資料庫...")
    all_pages = fetch_all_vocab()
    print(f"✅ 共找到 {len(all_pages)} 個單字")

    if len(all_pages) == 0:
        print("❌ 資料庫中沒有單字")
        exit(1)

    # 2. 提取單字資訊
    all_vocab = []
    for page in all_pages:
        vocab = extract_vocab_info(page)
        if vocab["word"]:  # 只保留有單字的項目
            all_vocab.append(vocab)

    print(f"✅ 有效單字：{len(all_vocab)} 個")

    if len(all_vocab) < 10:
        print(f"⚠️ 單字不足 10 個，將推送全部 {len(all_vocab)} 個")
        selected = all_vocab
    else:
        # 3. 隨機抽取 10 個
        selected = random.sample(all_vocab, 10)

    print(f"🎲 已隨機抽取 {len(selected)} 個單字")

    # 4. 格式化訊息
    message = format_vocab_message(selected, session)
    print(f"\n{'='*40}")
    print(message)
    print(f"{'='*40}\n")

    # 5. 推送到 LINE
    print("📤 正在推送到 LINE...")
    push_to_line(message)
    print("✅ 推送完成！")


if __name__ == "__main__":
    main()
