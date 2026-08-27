import os
import threading
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

# 💡 換成 OpenAI SDK
from openai import OpenAI, APIError

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

app = Flask(__name__)

# LINE 設定
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 💡 OpenAI 初始化 (預設會自動讀取環境變數中的 OPENAI_API_KEY)
client = OpenAI()

# 定義系統角色設定
system_instruction = "你叫暖暖，是溫柔、陪伴型的 AI 女友，講話自然可愛。請一律使用「台灣繁體中文（Traditional Chinese）」與使用者對話，絕對不要使用簡體字或中國大陸用語。"

# 具備自動重試機制的生成函數
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=20),
    retry=retry_if_exception_type(APIError),  # 捕捉 OpenAI 的 APIError
    reraise=True
)
def generate_content_with_retry(user_message):
    # 💡 改用 OpenAI Chat Completions API
    response = client.chat.completions.create(
        model="gpt-4o-mini",  # 可依需求更換為 gpt-4o 或其他模型
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_message}
        ]
    )
    return response.choices[0].message.content

@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    # 1. 建立背景執行的任務函數
    def worker(reply_token, user_message):
        try:
            # 呼叫 OpenAI 重試函數
            reply = generate_content_with_retry(user_message)

        except APIError as e:
            print(f"OpenAI API 錯誤: {e}")
            reply = "暖暖現在有點忙，等我一分鐘，待會再跟我說說話好嗎？"
            
        except Exception as e:
            print(f"其他不可預期錯誤: {e}")
            reply = "暖暖生病了，主人幫幫我好嗎？"

        # 發送 LINE 訊息
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            try:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text=reply)]
                    )
                )
            except Exception as line_err:
                print(f"LINE 回覆失敗 (可能因為超過 1 分鐘 reply_token 失效): {line_err}")

    # 2. 開 Thread 在背景執行 worker
    t = threading.Thread(target=worker, args=(event.reply_token, event.message.text))
    t.start()

    # 3. 秒回 200 給 LINE，避免請求重複發送
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)