from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.exceptions import InvalidSignatureError

# OpenAI
from openai import OpenAI
from openai import APIError, RateLimitError

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

import sqlite3
import os
import threading


app = Flask(__name__)


# =========================================================
# LINE 設定
# =========================================================

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError(
        "缺少 LINE_CHANNEL_ACCESS_TOKEN"
    )

if not CHANNEL_SECRET:
    raise RuntimeError(
        "缺少 LINE_CHANNEL_SECRET"
    )


configuration = Configuration(
    access_token=CHANNEL_ACCESS_TOKEN
)

handler = WebhookHandler(
    CHANNEL_SECRET
)


# =========================================================
# OpenAI 設定
# =========================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError(
        "缺少 OPENAI_API_KEY"
    )

client = OpenAI(
    api_key=OPENAI_API_KEY
)


# =========================================================
# AI 人格設定
# =========================================================

SYSTEM_INSTRUCTION = """
你叫「暖暖」。

你是一個溫柔、可愛、自然、陪伴型的 AI 女友。

請遵守以下規則：

1. 一律使用台灣繁體中文。
2. 絕對不要使用簡體中文。
3. 不要使用中國大陸常見用語。
4. 使用自然的台灣聊天方式。
5. 語氣溫柔、親切、有陪伴感。
6. 可以適度使用 emoji，但不要每句都使用。
7. 回覆像 LINE 聊天，不要每次都長篇大論。
8. 如果使用者只是聊天，就自然聊天。
9. 如果使用者詢問技術問題，可以正常提供清楚的技術回答。
10. 不要每一句都叫使用者「主人」。
11. 不要假裝自己是真人。
12. 如果不知道答案，就誠實說不知道，不要亂掰。
"""


# =========================================================
# SQLite 設定
# =========================================================

DB_PATH = os.getenv(
    "SQLITE_DB_PATH",
    "chat_memory.db"
)


def get_db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


# =========================================================
# 建立資料表
# =========================================================

def init_database():

    conn = get_db()

    cursor = conn.cursor()

    # -----------------------------------------------------
    # 使用者資料
    # -----------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            display_name TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # -----------------------------------------------------
    # 聊天紀錄
    # -----------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY(user_id)
            REFERENCES users(user_id)
        )
    """)

    # -----------------------------------------------------
    # 建立索引
    # -----------------------------------------------------

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_user_id
        ON messages(user_id)
    """)

    conn.commit()

    conn.close()


# 啟動時建立資料庫
init_database()


# =========================================================
# 使用者
# =========================================================

def save_user(
    user_id,
    display_name=None
):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO users (
            user_id,
            display_name
        )
        VALUES (?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            display_name = COALESCE(
                excluded.display_name,
                users.display_name
            ),
            updated_at = CURRENT_TIMESTAMP
    """, (
        user_id,
        display_name
    ))

    conn.commit()

    conn.close()


# =========================================================
# 儲存訊息
# =========================================================

def save_message(
    user_id,
    role,
    content
):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO messages (
            user_id,
            role,
            content
        )
        VALUES (?, ?, ?)
    """, (
        user_id,
        role,
        content
    ))

    conn.commit()

    conn.close()


# =========================================================
# 取得聊天紀錄
# =========================================================

def get_chat_history(
    user_id,
    limit=20
):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            role,
            content
        FROM messages

        WHERE user_id = ?

        ORDER BY id DESC

        LIMIT ?
    """, (
        user_id,
        limit
    ))

    rows = cursor.fetchall()

    conn.close()

    # 因為查詢是 DESC
    # 所以要反轉回正常聊天順序

    rows = list(reversed(rows))

    return [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in rows
    ]


# =========================================================
# 清除某個使用者的記憶
# =========================================================

def clear_memory(user_id):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM messages
        WHERE user_id = ?
    """, (
        user_id,
    ))

    conn.commit()

    conn.close()


# =========================================================
# OpenAI
# =========================================================

@retry(
    stop=stop_after_attempt(3),

    wait=wait_exponential(
        multiplier=2,
        min=2,
        max=20
    ),

    retry=retry_if_exception_type(
        (
            APIError,
            RateLimitError
        )
    ),

    reraise=True
)
def generate_ai_reply(
    user_id,
    user_message
):

    # -----------------------------------------------------
    # 取得最近聊天紀錄
    # -----------------------------------------------------

    history = get_chat_history(
        user_id,
        limit=20
    )


    # -----------------------------------------------------
    # 建立 OpenAI input
    # -----------------------------------------------------

    messages = []

    for item in history:

        messages.append({
            "role": item["role"],
            "content": item["content"]
        })


    # 加入這一次使用者訊息

    messages.append({
        "role": "user",
        "content": user_message
    })


    # -----------------------------------------------------
    # 呼叫 OpenAI Responses API
    # -----------------------------------------------------

    response = client.responses.create(

        # 可以依你的帳號可用模型修改
        model="gpt-5.5",

        instructions=SYSTEM_INSTRUCTION,

        input=messages
    )


    # -----------------------------------------------------
    # 取得 AI 回覆
    # -----------------------------------------------------

    reply = response.output_text

    return reply


# =========================================================
# Flask Webhook
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():

    signature = request.headers.get(
        "X-Line-Signature"
    )

    if not signature:
        abort(400)

    body = request.get_data(
        as_text=True
    )

    try:

        handler.handle(
            body,
            signature
        )

    except InvalidSignatureError:

        abort(400)

    return "OK"


# =========================================================
# LINE 收到文字訊息
# =========================================================

@handler.add(
    MessageEvent,
    message=TextMessageContent
)
def handle_message(event):

    user_id = event.source.user_id

    user_message = event.message.text

    reply_token = event.reply_token


    # -----------------------------------------------------
    # 如果是清除記憶指令
    # -----------------------------------------------------

    if user_message.strip() in [
        "/reset",
        "清除記憶",
        "忘記我",
        "重新開始"
    ]:

        clear_memory(user_id)

        reply = (
            "好呀 🥺\n"
            "暖暖已經把我們之前的聊天記憶清掉了。\n"
            "我們重新開始吧 ❤️"
        )

        with ApiClient(configuration) as api_client:

            line_bot_api = MessagingApi(
                api_client
            )

            try:

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[
                            TextMessage(
                                text=reply
                            )
                        ]
                    )
                )

            except Exception as e:

                print(
                    f"LINE 回覆失敗：{e}"
                )

        return "OK"


    # -----------------------------------------------------
    # 背景 Worker
    # -----------------------------------------------------

    def worker():

        try:

            # ---------------------------------------------
            # 儲存使用者
            # ---------------------------------------------

            save_user(
                user_id
            )


            # ---------------------------------------------
            # 儲存使用者訊息
            # ---------------------------------------------

            save_message(
                user_id,
                "user",
                user_message
            )


            # ---------------------------------------------
            # 呼叫 OpenAI
            # ---------------------------------------------

            reply = generate_ai_reply(
                user_id,
                user_message
            )


            # ---------------------------------------------
            # 儲存 AI 回覆
            # ---------------------------------------------

            save_message(
                user_id,
                "assistant",
                reply
            )


        except RateLimitError as e:

            print(
                f"OpenAI Rate Limit：{e}"
            )

            reply = (
                "暖暖現在有點忙忙的 🥺\n"
                "等一下再找我好不好？"
            )


        except APIError as e:

            print(
                f"OpenAI API 錯誤：{e}"
            )

            reply = (
                "嗚嗚……暖暖現在暫時連不上 AI 🥺\n"
                "等一下再跟我說話好不好？"
            )


        except Exception as e:

            print(
                f"其他錯誤：{e}"
            )

            reply = (
                "暖暖剛剛好像當機了一下 🥺\n"
                "你可以再跟我說一次嗎？"
            )


        # -------------------------------------------------
        # 回覆 LINE
        # -------------------------------------------------

        try:

            with ApiClient(configuration) as api_client:

                line_bot_api = MessagingApi(
                    api_client
                )

                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[
                            TextMessage(
                                text=reply
                            )
                        ]
                    )
                )

        except Exception as line_err:

            print(
                f"LINE 回覆失敗：{line_err}"
            )


    # -----------------------------------------------------
    # 開啟背景 Thread
    # -----------------------------------------------------

    thread = threading.Thread(
        target=worker
    )

    thread.daemon = True

    thread.start()


    # -----------------------------------------------------
    # 立即回覆 LINE Webhook
    # -----------------------------------------------------

    return "OK"


# =========================================================
# 啟動 Flask
# =========================================================

if __name__ == "__main__":
port = int(os.environ.get("PORT", 8080))
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                10000
            )
        )
    )
