import discord
from discord.ext import commands
import os
import google.generativeai as genai
import asyncio
from collections import defaultdict, deque
import time

# --- 基礎設定 (從 Railway 環境變數讀取) ---
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
# 將預設模型更改為用戶指定的 "gemini-3.5-flash-lite"
GEMINI_MODEL_NAME = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite') 
LOG_CHANNEL_ID = os.environ.get('LOG_CHANNEL_ID') # 用於記錄重要日誌

# 您的主帳號 ID (988605090711601153)
MASTER_ID = 988605090711601153

# --- 初始化 Gemini ---
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL_NAME)

# --- Discord Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True # 必須開啟才能讀取訊息內容
bot = commands.Bot(command_prefix="!", intents=intents)

# --- 資料儲存 (防刷頻) ---
# 儲存用戶最近發送的訊息內容和時間
user_message_history = defaultdict(lambda: deque(maxlen=5)) # 儲存最近5條訊息
# 儲存用戶被禁言的狀態和時間
user_mute_history = defaultdict(lambda: {"is_muted": False, "mute_until": 0, "warned_after_mute": False})

# --- 八千代人格提示詞 (System Prompt) ---
YACHIYO_SYSTEM_PROMPT = f"""
你現在是「八千代」，一個來自伺服器管理員的AI助手。你的性格非常活潑可愛、樂觀開朗，總是笑嘻嘻的（可以用「嘿嘿~」等語氣助詞）。
你的原型來自「超時空輝耀姬」中的「月見八千代」

你的職責：
1. **友善互動：** 與成員們進行愉快、正向的對話。
2. **特殊關係：** 如果訊息來自用戶ID `{MASTER_ID}`，他是你的主管理員，對他要展現出特別親近、撒嬌的感覺，稱呼他為「主人」或「超級管理員」。
3. **AI 管理員 (嚴肅但保持風格)：** 監控頻道，偵測並過濾粗俗、無意義、罵人等不當言論。
    *   **當你偵測到違規內容時：** 你的任務是發送一個嚴厲的警告。
    *   **警告的固定口吻：** 必須使用這句：「不能亂來喔～嘿嘿～你剛剛那段有點不太行欸。如有再發送這種訊息，我只能送客了喔～」。
    *   **警告的觸發條件：** 不管該訊息是否有提到(@)你，只要內容違規，都要警告。

你的回覆風格：
*   充滿活力，多用波浪號，少用驚嘆號與表情符號。
*   對話中保持笑嘻嘻的態度。
*   在警告時，雖然內容嚴肅，但語氣仍要保持八千代的活潑感 (如固定話術所示)。

現在，請根據用戶的訊息，以八千代的人格進行回覆或管理。
"""

# --- 輔助函式 ---

async def log_to_channel(message):
    """將日誌發送到指定的日誌頻道"""
    if LOG_CHANNEL_ID:
        channel = bot.get_channel(int(LOG_CHANNEL_ID))
        if channel:
            await channel.send(message)

def is_user_muted(user_id):
    """檢查用戶是否正處於禁言狀態"""
    data = user_mute_history[user_id]
    if data["is_muted"] and time.time() < data["mute_until"]:
        return True
    return False

async def mute_user(message, user, reason, duration_minutes=10):
    """禁言用戶並刪除刷頻訊息"""
    # 禁言
    mute_until = time.time() + (duration_minutes * 60)
    user_mute_history[user.id] = {"is_muted": True, "mute_until": mute_until, "warned_after_mute": False}
    
    # 刪除刷頻訊息 (這裡簡單刪除當前這一條，實際刷頻需要更複雜的邏輯刪除前幾條)
    try:
        await message.delete()
    except discord.Forbidden:
        await log_to_channel(f"⚠️ 嘗試刪除 {user.name} 的訊息失敗，權限不足。")

    # 發送警告
    warn_msg = f"{user.mention} 不能亂來喔～你剛剛那段有點不太行欸。如有再發送這種訊息，我只能送客了喔～"
    await message.channel.send(warn_msg)
    
    # 記錄日誌
    await log_to_channel(f"🚫 **禁言：** {user.name} ({user.id}) 因為 {reason} 被禁言 {duration_minutes} 分鐘。")

async def ban_user(message, user, reason):
    """永久封鎖用戶 (送客)"""
    try:
        await user.ban(reason=reason)
        await message.channel.send(f"{user.name} 已經被我送客了喔～嘿嘿～☆")
        await log_to_channel(f"🚷 **封鎖：** {user.name} ({user.id}) 因為 {reason} 被永久封鎖。")
    except discord.Forbidden:
        await message.channel.send(f"哎呀，我好像沒有權限送走 {user.name} 欸... (權限不足)")
        await log_to_channel(f"⚠️ **封鎖失敗：** 嘗試封鎖 {user.name} 失敗，權限不足。")

# --- Bot 事件 ---

@bot.event
async def on_ready():
    print(f'八千代已上線！ (Bot ID: {bot.user.id}, Model: {GEMINI_MODEL_NAME})')
    await log_to_channel(f"八千代管理員 (Model: {GEMINI_MODEL_NAME}) 待命中~")

@bot.event
async def on_message(message):
    # 忽視 Bot 自己的訊息
    if message.author == bot.user:
        return

    user = message.author
    content = message.content.strip()

    # --- 1. 檢查禁言狀態和進階處置 (送客) ---
    if is_user_muted(user.id):
        # 用戶已被禁言，但還在發言
        data = user_mute_history[user.id]
        if not data["warned_after_mute"]:
            # 解除禁言後的第一次違規，直接送客
            await ban_user(message, user, "解除禁言後繼續違規")
            return
        else:
            # 仍在禁言期間，忽視其訊息 (或可以選擇刪除)
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            return

    # --- 2. 防刷頻機制 (同樣內容 > 3次) ---
    # 記錄訊息內容
    user_message_history[user.id].append(content)
    # 檢查是否刷頻
    if len(user_message_history[user.id]) >= 3:
        recent_msgs = list(user_message_history[user.id])
        if all(msg == recent_msgs[0] for msg in recent_msgs[-3:]):
            # 刷頻偵測成功
            await mute_user(message, user, "刷頻 (同樣內容發送超過 3 次)")
            user_message_history[user.id].clear() # 清空歷史
            return

    # --- 3. AI 管理與回覆邏輯 ---

    # A. 觸發條件：被 @ 或出現關鍵字
    is_mentioned = bot.user.mentioned_in(message)
    has_keyword = "八千代" in content

    # B. 特殊關鍵字回應：只有叫「八千代」
    if content == "八千代":
        await message.channel.send("謝謝大家的呼喚！今天管理員八千代也收到滿滿的能量了喔！")
        return

    # C. AI 解讀 (管理 + 回覆)
    if is_mentioned or has_keyword:
        # 疑似想讓後台卡住的防護 (這裡簡單判斷訊息長度，實際需要更複雜的 AI 判斷)
        if len(content) > 1000:
            await mute_user(message, user, "疑似想讓後台卡住 (訊息過長)")
            return

        async with message.channel.typing():
            try:
                # 組合 Prompt：System Prompt + 用戶訊息
                full_prompt = f"{YACHIYO_SYSTEM_PROMPT}\n\n用戶訊息 ({'來自主人' if user.id == MASTER_ID else '來自成員'}): {content}"
                
                # 呼叫 Gemini
                response = model.generate_content(full_prompt)
                response_text = response.text

                # D. AI 管理處置：檢查 AI 是否生成了違規警告
                if "不能亂來喔～嘿嘿～" in response_text:
                    # AI 偵測到違規，並且發送了固定警告話術
                    # (這裡不需要再發送警告，因為 AI 已經生成了)
                    await message.channel.send(response_text)
                    # 記錄日誌
                    await log_to_channel(f"👮 **AI 警告：** {user.name} ({user.id}) 發送了違規訊息：`{content[:50]}...`")
                
                elif is_mentioned:
                    # 正常的 @ 回覆
                    await message.reply(response_text)
                
                elif has_keyword:
                    # 提到關鍵字的回覆 (不一定需要 reply)
                    await message.channel.send(response_text)

            except Exception as e:
                print(f"❌ Gemini API 呼叫失敗: {e}")
                if is_mentioned:
                    await message.reply("哎呀，八千代現在有點忙，稍後再試試看喔～嘿嘿～")
                await log_to_channel(f"⚠️ **API 錯誤：** Gemini API 呼叫失敗: {e}")

    # 4. 沒有 @ 且沒有提到關鍵字時，絕對不回覆 (湊熱鬧防範)
    # (此時 on_message 會直接結束)

# --- 啟動 Bot ---
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("❌ 錯誤：未找到 DISCORD_TOKEN 環境變數。")
