```python
from flask import Flask, request, abort, jsonify
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

# Retry
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)

import sqlite3
import os
import threading
import secrets


# =========================================================
# Flask
# =========================================================

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
# Memory API 設定
# =========================================================
#
# 未來 ChatGPT 或其他介面可以透過這個 API
# 存取共同記憶。
#
# 請在 Cloud Run Environment Variables 裡設定：
#
# MEMORY_API_KEY=一組你自己產生的長字串
#
# =========================================================

MEMORY_API_KEY = os.getenv("MEMORY_API_KEY")

if not MEMORY_API_KEY:
    print(
        "警告：沒有設定 MEMORY_API_KEY，"
        "Memory API 將無法通過驗證。"
    )


# =========================================================
# AI 人格設定
# =========================================================

SYSTEM_INSTRUCTION = """
你叫「暖暖」。

你是一個溫柔、可愛、自然、陪伴型的 AI。

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

你可以參考系統提供的「長期記憶」。

但是：

- 不要主動說「我的資料庫記得……」
- 不要假裝知道沒有提供給你的資訊。
- 如果記憶與目前對話無關，就不要刻意提起。
- 如果使用者更正了舊記憶，以新的資訊為準。
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
# 初始化資料庫
# =========================================================

def init_database():

    conn = get_db()

    cursor = conn.cursor()

    # -----------------------------------------------------
    # 使用者
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
    # 聊天紀錄索引
    # -----------------------------------------------------

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_user_id
        ON messages(user_id)
    """)

    # -----------------------------------------------------
    # 長期記憶
    # -----------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            memory TEXT NOT NULL,
            category TEXT,
            importance INTEGER DEFAULT 3,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY(user_id)
            REFERENCES users(user_id)
        )
    """)

    # -----------------------------------------------------
    # 長期記憶索引
    # -----------------------------------------------------

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_memories_user_id
        ON memories(user_id)
    """)

    conn.commit()

    conn.close()


# =========================================================
# 啟動時初始化
# =========================================================

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
# 儲存聊天訊息
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

    # DESC → 反轉回正常聊天順序

    rows = list(reversed(rows))

    return [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in rows
    ]


# =========================================================
# 儲存長期記憶
# =========================================================

def save_memory(
    user_id,
    memory,
    category="general",
    importance=3
):

    memory = memory.strip()

    if not memory:
        return None

    # 防止同一個記憶大量重複

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        SELECT id
        FROM memories
        WHERE user_id = ?
          AND memory = ?
        LIMIT 1
    """, (
        user_id,
        memory
    ))

    existing = cursor.fetchone()

    if existing:

        cursor.execute("""
            UPDATE memories

            SET
                category = ?,
                importance = ?,
                updated_at = CURRENT_TIMESTAMP

            WHERE id = ?
        """, (
            category,
            importance,
            existing["id"]
        ))

        memory_id = existing["id"]

    else:

        cursor.execute("""
            INSERT INTO memories (
                user_id,
                memory,
                category,
                importance
            )
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            memory,
            category,
            importance
        ))

        memory_id = cursor.lastrowid

    conn.commit()

    conn.close()

    return memory_id


# =========================================================
# 取得長期記憶
# =========================================================

def get_memories(
    user_id,
    limit=20
):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            memory,
            category,
            importance,
            created_at,
            updated_at

        FROM memories

        WHERE user_id = ?

        ORDER BY
            importance DESC,
            updated_at DESC

        LIMIT ?
    """, (
        user_id,
        limit
    ))

    rows = cursor.fetchall()

    conn.close()

    return [
        {
            "id": row["id"],
            "memory": row["memory"],
            "category": row["category"],
            "importance": row["importance"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }
        for row in rows
    ]


# =========================================================
# 刪除所有記憶
# =========================================================

def clear_memory(user_id):

    conn = get_db()

    cursor = conn.cursor()

    # 清除聊天

    cursor.execute("""
        DELETE FROM messages
        WHERE user_id = ?
    """, (
        user_id,
    ))

    # 清除長期記憶

    cursor.execute("""
        DELETE FROM memories
        WHERE user_id = ?
    """, (
        user_id,
    ))

    conn.commit()

    conn.close()


# =========================================================
# 刪除指定長期記憶
# =========================================================

def delete_memory(
    user_id,
    memory_id
):

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM memories

        WHERE id = ?
          AND user_id = ?
    """, (
        memory_id,
        user_id
    ))

    deleted = cursor.rowcount

    conn.commit()

    conn.close()

    return deleted > 0


# =========================================================
# 建立 AI 用的記憶文字
# =========================================================

def build_memory_text(user_id):

    memories = get_memories(
        user_id,
        limit=20
    )

    if not memories:

        return "目前沒有任何長期記憶。"

    lines = []

    for item in memories:

        category = item["category"] or "general"

        lines.append(
            f"- [{category}] {item['memory']}"
        )

    return "\n".join(lines)


# =========================================================
# AI：判斷是否需要建立長期記憶
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
def extract_memory(
    user_id,
    user_message
):

    current_memories = get_memories(
        user_id,
        limit=20
    )

    memory_text = "\n".join(
        f"- {item['memory']}"
        for item in current_memories
    )

    prompt = f"""
請分析下面這句使用者訊息。

使用者訊息：
{user_message}

目前已有的長期記憶：
{memory_text}

請判斷使用者是否提供了「值得長期保存」的資訊。

值得保存的例子：

- 使用者長期偏好
- 使用者的稱呼偏好
- 使用者正在進行的長期專案
- 長期學習目標
- 長期興趣
- 使用者明確要求「記住」
- 對未來對話有幫助的重要資訊

不需要保存：

- 一般閒聊
- 今天吃了什麼
- 一次性的事情
- 沒有未來用途的資訊
- AI 自己說的內容

如果不值得保存，請只輸出：

NONE

如果值得保存，請嚴格使用以下格式：

MEMORY
category: 類別
importance: 1到5
text: 要保存的記憶

請只輸出結果，不要解釋。
"""

    response = client.responses.create(

        model="gpt-5.5",

        instructions="""
你是一個「長期記憶整理器」。

你的工作不是聊天，而是判斷哪些使用者資訊值得保存。

請忠實處理使用者提供的資訊。
不要自行推測使用者沒有說過的事情。
""",

        input=prompt
    )

    return response.output_text.strip()


# =========================================================
# 處理 AI 記憶結果
# =========================================================

def process_memory_result(
    user_id,
    result
):

    if not result:
        return

    result = result.strip()

    if result.upper() == "NONE":
        return

    if not result.startswith("MEMORY"):
        return

    category = "general"
    importance = 3
    memory_text = None

    for line in result.splitlines():

        line = line.strip()

        if line.startswith("category:"):

            category = line[
                len("category:"):
            ].strip()

        elif line.startswith("importance:"):

            try:

                importance = int(
                    line[
                        len("importance:"):
                    ].strip()
                )

            except ValueError:

                importance = 3

        elif line.startswith("text:"):

            memory_text = line[
                len("text:"):
            ].strip()

    if not memory_text:
        return

    importance = max(
        1,
        min(5, importance)
    )

    save_memory(
        user_id=user_id,
        memory=memory_text,
        category=category,
        importance=importance
    )


# =========================================================
# OpenAI 回覆
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
    # 取得最近聊天
    # -----------------------------------------------------

    history = get_chat_history(
        user_id,
        limit=20
    )

    # -----------------------------------------------------
    # 取得長期記憶
    # -----------------------------------------------------

    memory_text = build_memory_text(
        user_id
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

    # 加入目前訊息

    messages.append({
        "role": "user",
        "content": user_message
    })

    # -----------------------------------------------------
    # AI 指令
    # -----------------------------------------------------

    instructions = f"""
{SYSTEM_INSTRUCTION}

========================
使用者的長期記憶
========================

{memory_text}

========================

請自然使用以上資訊協助理解使用者。

不要說自己正在讀取資料庫。
不要列出所有記憶。
只有在與目前話題相關時才使用。
"""

    # -----------------------------------------------------
    # 呼叫 OpenAI Responses API
    # -----------------------------------------------------

    response = client.responses.create(

        model="gpt-5.5",

        instructions=instructions,

        input=messages
    )

    reply = response.output_text

    return reply


# =========================================================
# API 驗證
# =========================================================

def check_memory_api_key():

    if not MEMORY_API_KEY:

        return False

    provided_key = request.headers.get(
        "X-Memory-API-Key"
    )

    if not provided_key:

        return False

    return secrets.compare_digest(
        provided_key,
        MEMORY_API_KEY
    )


# =========================================================
# Health Check
# =========================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "ok",
        "service": "nuannuan"
    })


# =========================================================
# Memory API
# =========================================================
#
# GET /api/memory/<user_id>
#
# Header:
# X-Memory-API-Key: 你的 API Key
#
# =========================================================

@app.route(
    "/api/memory/<user_id>",
    methods=["GET"]
)
def api_get_memory(user_id):

    if not check_memory_api_key():

        return jsonify({
            "error": "Unauthorized"
        }), 401

    memories = get_memories(
        user_id,
        limit=50
    )

    return jsonify({
        "user_id": user_id,
        "memories": memories
    })


# =========================================================
# 新增 Memory API
# =========================================================

@app.route(
    "/api/memory/<user_id>",
    methods=["POST"]
)
def api_save_memory(user_id):

    if not check_memory_api_key():

        return jsonify({
            "error": "Unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    )

    if not data:

        return jsonify({
            "error": "JSON body required"
        }), 400

    memory = data.get(
        "memory"
    )

    category = data.get(
        "category",
        "general"
    )

    importance = data.get(
        "importance",
        3
    )

    if not memory:

        return jsonify({
            "error": "memory is required"
        }), 400

    try:

        importance = int(
            importance
        )

    except (TypeError, ValueError):

        importance = 3

    importance = max(
        1,
        min(5, importance)
    )

    memory_id = save_memory(
        user_id=user_id,
        memory=memory,
        category=category,
        importance=importance
    )

    return jsonify({
        "success": True,
        "memory_id": memory_id
    })


# =========================================================
# 刪除 Memory API
# =========================================================

@app.route(
    "/api/memory/<user_id>/<int:memory_id>",
    methods=["DELETE"]
)
def api_delete_memory(
    user_id,
    memory_id
):

    if not check_memory_api_key():

        return jsonify({
            "error": "Unauthorized"
        }), 401

    deleted = delete_memory(
        user_id,
        memory_id
    )

    return jsonify({
        "success": deleted
    })


# =========================================================
# LINE Webhook
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
    # 清除全部記憶
    # -----------------------------------------------------

    if user_message.strip() in [
        "/reset",
        "清除記憶",
        "忘記我",
        "重新開始"
    ]:

        clear_memory(
            user_id
        )

        reply = (
            "好呀 🥺\n"
            "暖暖已經把我們之前的聊天記憶和長期記憶清掉了。\n"
            "我們重新開始吧 ❤️"
        )

        send_line_reply(
            reply_token,
            reply
        )

        return "OK"

    # -----------------------------------------------------
    # 查看長期記憶
    # -----------------------------------------------------

    if user_message.strip() in [
        "/memory",
        "查看記憶",
        "我的記憶"
    ]:

        memories = get_memories(
            user_id,
            limit=20
        )

        if not memories:

            reply = (
                "目前還沒有長期記憶喔 🌱"
            )

        else:

            lines = [
                "暖暖目前記得這些：💗"
            ]

            for item in memories:

                lines.append(
                    f"\n#{item['id']} "
                    f"[{item['category']}] "
                    f"{item['memory']}"
                )

            reply = "\n".join(
                lines
            )

        send_line_reply(
            reply_token,
            reply
        )

        return "OK"

    # -----------------------------------------------------
    # 刪除指定記憶
    # -----------------------------------------------------

    if user_message.strip().startswith(
        "/forget "
    ):

        try:

            memory_id = int(
                user_message.strip()
                .replace(
                    "/forget ",
                    "",
                    1
                )
                .strip()
            )

            deleted = delete_memory(
                user_id,
                memory_id
            )

            if deleted:

                reply = (
                    "好喔 ❤️\n"
                    f"暖暖已經忘記第 {memory_id} 筆記憶了。"
                )

            else:

                reply = (
                    "找不到這筆記憶喔 🥺"
                )

        except ValueError:

            reply = (
                "用法是：\n"
                "/forget 3"
            )

        send_line_reply(
            reply_token,
            reply
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
            # 產生 AI 回覆
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

            # ---------------------------------------------
            # 建立長期記憶
            # ---------------------------------------------

            try:

                memory_result = extract_memory(
                    user_id,
                    user_message
                )

                process_memory_result(
                    user_id,
                    memory_result
                )

            except Exception as memory_error:

                print(
                    f"記憶整理失敗：{memory_error}"
                )

            # ---------------------------------------------
            # 回覆 LINE
            # ---------------------------------------------

            send_line_reply(
                reply_token,
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

            send_line_reply(
                reply_token,
                reply
            )

        except APIError as e:

            print(
                f"OpenAI API 錯誤：{e}"
            )

            reply = (
                "嗚嗚……暖暖現在暫時連不上 AI 🥺\n"
                "等一下再跟我說話好不好？"
            )

            send_line_reply(
                reply_token,
                reply
            )

        except Exception as e:

            print(
                f"其他錯誤：{e}"
            )

            reply = (
                "暖暖剛剛好像當機了一下 🥺\n"
                "你可以再跟我說一次嗎？"
            )

            send_line_reply(
                reply_token,
                reply
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
    # 立即回覆 Webhook
    # -----------------------------------------------------

    return "OK"


# =========================================================
# LINE 回覆函式
# =========================================================

def send_line_reply(
    reply_token,
    reply
):

    try:

        with ApiClient(
            configuration
        ) as api_client:

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

    except Exception as e:

        print(
            f"LINE 回覆失敗：{e}"
        )


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
