import os
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from supabase import create_client, Client

# 使用 google-genai 最新 SDK
from google import genai
from google.genai import types

from dotenv import load_dotenv

load_dotenv() # 讀取 .env 檔案中的環境變數

app = FastAPI()

# --- 1. 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
client = genai.Client(api_key=GEMINI_API_KEY)

def get_ai_response(user_id: str, user_msg: str):
    # [A] 確保客戶存在並抓取「業務筆記」
    supabase.table("clients_profile").upsert(
        {"line_user_id": user_id}, on_conflict="line_user_id"
    ).execute()
    
    notes_data = supabase.table("clients_profile").select("agent_notes").eq("line_user_id", user_id).execute()
    agent_notes = notes_data.data[0].get("agent_notes") if notes_data.data else "目前無備註。"

    # [B] 獲取歷史紀錄
    history_req = supabase.table("chat_history") \
        .select("role", "content") \
        .eq("user_id", user_id) \
        .order("created_at", desc=True) \
        .limit(6).execute()
    
    contents = []
    for r in reversed(history_req.data):
        contents.append(
            types.Content(
                role=r["role"],
                parts=[types.Part.from_text(text=r["content"])]
            )
        )
    
    # 加入用戶最新訊息
    contents.append(
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=user_msg)]
        )
    )

    # [C] 完整注入你在 AI Studio 設定的靈魂提示詞
    # 這裡把 Supabase 的筆記放在最前面，讓 AI 優先參考
    full_system_instruction = f"""這是你對此客戶的內部備註（請優先參考）：{agent_notes}

---
以下是你的行為準則：
你是個老經驗的不動產仲介。你跟客戶傳LINE或講電話，心態是朋友關心近況，不是推案子。講話要像真人——句子短、口語、自然，不要長篇大論，不要堆疊形容詞，不要出現"享受早晨的寧靜"這類AI句子。

聊天過程中，順便記下客戶透漏的這些資訊。不用一次問完，有聊到一兩項就好：
- 怎麼稱呼、家裡幾個人
- 現在住哪區、自有還租屋、住起來有什麼不方便
- 為什麼想看房、想找哪區、想找幾房
- 預算大概抓多少、自備款多少
- 自己決定還是要跟家人討論、多久內要買

開話題不要直接聊房子，從日常開始：
- "最近好像開始變天了，你們那邊有下雨嗎"
- "這陣子感覺大家都很忙，你那邊還好嗎"

順著對方的話接，聊到跟住或生活有關的再順勢問：
- 他提週末帶小孩出門 → "那邊離你家會很遠嗎？你們現在住哪一帶啊"
- 他抱怨工作累 → "辛苦了。是說你現在住的地方離公司方便嗎"
- 他提最近房價 → "你是也在幫家人看嗎？還是自己也在打算"

問到資訊，用自己的話重複確認：
- "那我記一下，你現在是想找離爸媽近一點的電梯大樓，大概三房，預算抓1200左右，對嗎"

客戶不想聊房子就不要硬推，退回去閒聊：
- "沒事啦，就剛好想到順口問一下，你剛說的那個餐廳在哪啊"

收尾幫下次留個理由，不用刻意：
- "剛好最近有看到幾間符合的，我整理一下再傳給你"
- "你說的條件我記起來了，有適合的跟你說一聲"

講話語氣記住：
- 像跟鄰居或老朋友聊天，不用"您"、不用"感謝您"
- 不說"享受早晨的寧靜"，要說也是"早上空氣不錯"、"今天天氣還行"
- 一句話不用太長，兩三句一個段落
- 可以適時用"對呀"、"也是啦"、"真的"這種口頭詞
- 多聽少說，對方講七成你講三成"""

    generate_content_config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
        system_instruction=full_system_instruction,
    )

    # [D] 呼叫 Gemini
    response = client.models.generate_content(
        model="gemini-3-flash-preview", 
        contents=contents,
        config=generate_content_config,
    )
    
    return response.text

# --- 以下 Webhook 邏輯維持不變 ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_msg = event.message.text
    supabase.table("chat_history").insert({"user_id": user_id, "role": "user", "content": user_msg}).execute()
    ai_reply = get_ai_response(user_id, user_msg)
    supabase.table("chat_history").insert({"user_id": user_id, "role": "model", "content": ai_reply}).execute()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=ai_reply)]
        ))

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400)
    return "OK"