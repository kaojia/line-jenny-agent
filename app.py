"""合併入口：同一個 Flask app 同時服務兩隻 LINE bot。

- Jenny 個人助理  → /jenny/...   （webhook: /jenny/callback）
- 賣家客服 bot     → /customer/... （webhook: /customer/callback）
- 共用健康檢查     → /ping

每隻 bot 各自持有獨立的 LineBotApi / WebhookHandler（不同 channel token / secret），
在各自模組以 module-level 方式初始化，互不干擾。
"""
import os

from flask import Flask
from dotenv import load_dotenv

load_dotenv()

from bot_jenny import bp as jenny_bp
from bot_customer import bp as customer_bp

app = Flask(__name__)
app.register_blueprint(jenny_bp, url_prefix="/jenny")
app.register_blueprint(customer_bp, url_prefix="/customer")


@app.route("/ping", methods=["GET"])
def ping():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
