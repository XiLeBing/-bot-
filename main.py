import discord
from discord.ext import commands
from discord import app_commands # 新增斜線指令支援
import os
import google.generativeai as genai
import asyncio
from collections import defaultdict, deque
import time
import datetime
import json # 新增 JSON 支援，用於儲存伺服器設定

# --- 基礎設定 (從 Railway 環境變數讀取) ---
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
GEMINI_MODEL_NAME = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite') 
GLOBAL_LOG_CHANNEL_ID = os.environ.get('LOG_CHANNEL_ID') # 全域日誌頻道 (原本的變數)

# 您的主帳號 ID (988605090711601153)
MASTER_ID = 988605090711601153

# --- 初始化 Gemini ---
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL_NAME)

# --- 資料儲存檔案 ---
SERVER_LOGS_FILE = "server_logs.json"

# --- Discord Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- 資料儲存 (防刷頻與管理) ---
# 儲存用戶最近發送的訊息
user_message_history = defaultdict(lambda: deque(maxlen=10)) 
# 儲存用戶的處置紀錄
user_penalty_history = defaultdict(lambda: {"mute_count": 0, "last_mute_time": 0})
# 儲存伺服器日誌設定：{guild_id: channel_id}
server_logs = {}

# --- 載入/儲存伺服器日誌設定 ---
def load_server_logs():
    global server_logs
    if os.path.exists(SERVER_LOGS_FILE):
        try:
            with open(SERVER_LOGS_FILE, 'r') as f:
                # JSON 不支援 int key，載入時轉回 int
                loaded_data = json.load(f)
                server_logs = {int(k): v for k, v in loaded_data.items()}
        except Exception as e:
            print(f"❌ 載入伺服器日誌設定失敗: {e}")
            server_logs = {}
    else:
        server_logs = {}

def save_server_logs():
    try:
        with open(SERVER_LOGS_FILE, 'w') as f:
            # JSON 不支援 int key，儲存時轉為 str
            json_data = {str(k): v for k, v in server_logs.items()}
            json.dump(json_data, f)
    except Exception as e:
        print(f"❌ 儲存伺服器日誌設定失敗: {e}")

# --- 刷頻防護參數 ---
SPAM_THRESHOLD_MUTE = 3
SPAM_THRESHOLD_BAN = 5
SPAM_INTERVAL = 10
MUTE_DURATION_MINUTES = 10

# --- 八千代人格提示詞 ---
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

現在，請根據用戶的訊息，以八千代人格進行回覆或管理。
"""

# --- 輔助函式 ---

async def log_to_channel(guild, message):
    """將日誌發送到全域日誌頻道，以及伺服器特定的日誌頻道"""
    # 1. 發送到全域日誌
    if GLOBAL_LOG_CHANNEL_ID:
        global_channel = bot.get_channel(int(GLOBAL_LOG_CHANNEL_ID))
        if global_channel:
            await global_channel.send(f"🌍 **[全域日誌]** {message}")

    # 2. 發送到伺服器日誌 (如果有的話)
    if guild and guild.id in server_logs:
        server_channel_id = server_logs[guild.id]
        server_channel = bot.get_channel(server_channel_id)
        if server_channel:
            await server_channel.send(f"格 **[伺服器日誌]** {message}")

def is_user_timed_out(member: discord.Member):
    """檢查用戶是否正處於 Discord 官方的 Timeout 狀態"""
    if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
        return True
    return False

async def mute_user(message, user: discord.Member, reason, duration_minutes=MUTE_DURATION_MINUTES):
    """執行 Discord 的 Timeout (禁言) 並刪除訊息"""
    try:
        timeout_until = discord.utils.utcnow() + datetime.timedelta(minutes=duration_minutes)
        await user.timeout(timeout_until, reason=reason)
        await message.delete() 
        user_penalty_history[user.id]["mute_count"] += 1
        user_penalty_history[user.id]["last_mute_time"] = time.time()
        warn_msg = f"不能亂來喔～嘿嘿～{user.mention}，你剛剛那段有點不太行欸。我先讓你休息 {duration_minutes} 分鐘喔～"
        await message.channel.send(warn_msg)
        await log_to_channel(message.guild, f"🚫 **禁言 (Timeout)：** {user.name} ({user.id}) 因為 {reason} 被禁言 {duration_minutes} 分鐘。")
    except discord.Forbidden:
        await log_to_channel(message.guild, f"⚠️ **管理失敗：** 嘗試禁言 {user.name} 失敗，權限不足。")
    except Exception as e:
        await log_to_channel(message.guild, f"❌ **管理出錯：** 禁言 {user.name} 時出錯: {e}")

async def ban_user(message, user: discord.Member, reason):
    """永久封鎖用戶 (送客)"""
    try:
        await user.ban(reason=reason)
        await message.channel.send(f"{user.name} 已經被我送客了喔～嘿嘿～☆")
        await log_to_channel(message.guild, f"🚷 **封鎖 (Ban)：** {user.name} ({user.id}) 因為 {reason} 被永久封鎖。")
    except discord.Forbidden:
        await message.channel.send(f"哎呀，我好像沒有權限送走 {user.name} 欸... (權限不足)")
        await log_to_channel(message.guild, f"⚠️ **封鎖失敗：** 嘗試封鎖 {user.name} 失敗，權限不足。")

# --- Bot 事件 ---

@bot.event
async def on_ready():
    load_server_logs() # 載入設定
    # 同步斜線指令
    try:
        synced = await bot.tree.sync()
        print(f"成功同步 {len(synced)} 個斜線指令！")
    except Exception as e:
        print(f"❌ 同步斜線指令失敗: {e}")
        
    print(f'八千代已上線！ (Bot ID: {bot.user.id}, Model: {GEMINI_MODEL_NAME})')
    await log_to_channel(None, f"八千代管理員 (Model: {GEMINI_MODEL_NAME}) 待命中~")

@bot.event
async def on_message(message):
    if message.author == bot.user or not isinstance(message.author, discord.Member):
        return

    user = message.author
    content = message.content.strip()
    current_time = time.time()

    if is_user_timed_out(user):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        return

    penalty_data = user_penalty_history[user.id]
    is_after_mute = penalty_data["mute_count"] > 0
    user_message_history[user.id].append((current_time, content))
    recent_messages = user_message_history[user.id]
    identical_messages_in_interval = [
        msg_content for msg_time, msg_content in recent_messages
        if current_time - msg_time <= SPAM_INTERVAL and msg_content == content
    ]
    num_identical = len(identical_messages_in_interval)

    if is_after_mute:
        if num_identical >= SPAM_THRESHOLD_BAN:
            await ban_user(message, user, f"解除禁言後繼續刷頻 ({SPAM_INTERVAL}秒內同樣內容 {num_identical} 次)")
            user_message_history[user.id].clear()
            return
    else:
        if num_identical >= SPAM_THRESHOLD_MUTE:
            await mute_user(message, user, f"刷頻 ({SPAM_INTERVAL}秒內同樣內容 {num_identical} 次)")
            user_message_history[user.id].clear()
            return

    is_mentioned = bot.user.mentioned_in(message)
    has_keyword = "八千代" in content

    if content == "八千代":
        await message.channel.send("謝謝大家的呼喚！今天管理員八千代也收到滿滿的能量了喔！")
        return

    if is_mentioned or has_keyword:
        if len(content) > 1000:
            await mute_user(message, user, "疑似想讓後台卡住 (訊息過長)")
            return

        async with message.channel.typing():
            try:
                full_prompt = f"{YACHIYO_SYSTEM_PROMPT}\n\n用戶訊息 ({'來自主人' if user.id == MASTER_ID else '來自成員'}): {content}"
                response = model.generate_content(full_prompt)
                response_text = response.text

                if "不能亂來喔～嘿嘿～" in response_text:
                    if is_after_mute:
                        await ban_user(message, user, f"解除禁言後 AI 判定違規: `{content[:50]}...`")
                    else:
                        await message.channel.send(response_text)
                        await log_to_channel(message.guild, f"👮 **AI 警告：** {user.name} ({user.id}) 發送了違規訊息：`{content[:50]}...`")
                elif is_mentioned:
                    await message.reply(response_text)
                elif has_keyword:
                    await message.channel.send(response_text)
            except Exception as e:
                print(f"❌ Gemini API 呼叫失敗: {e}")
                if is_mentioned:
                    await message.reply("哎呀，八千代現在有點忙，稍後再試試看喔～嘿嘿～")
                await log_to_channel(message.guild, f"⚠️ **API 錯誤：** Gemini API 呼叫失敗: {e}")

# --- 斜線指令功能 (/log) ---

@bot.tree.command(name="log", description="設定或刪除當前伺服器的日誌頻道 (僅限主人使用)")
@app_commands.describe(action="選擇操作: set (設定當前頻道) 或 del (刪除設定)")
async def log_command(interaction: discord.Interaction, action: str):
    """/log 指令的實作"""
    
    # 1. 權限檢查：只能由主人使用
    if interaction.user.id != MASTER_ID:
        await interaction.response.send_message("嘿嘿～這個指令是主人的專屬特權喔！你不能亂動～☆", ephemeral=True)
        return

    guild = interaction.guild
    channel = interaction.channel

    if action.lower() == "set":
        # 2. 設定日誌頻道
        server_logs[guild.id] = channel.id
        save_server_logs() # 儲存設定
        await interaction.response.send_message(f"好喔，主人！八千代已經將 {channel.mention} 設定為這個伺服器的日誌頻道了！嘿嘿~☆")
        await log_to_channel(guild, f"⚙️ **設定更新：** {interaction.user.name} 將 {channel.mention} 設定為伺服器日誌頻道。")

    elif action.lower() == "del":
        # 3. 刪除日誌頻道設定
        if guild.id in server_logs:
            del server_logs[guild.id]
            save_server_logs() # 儲存設定
            await interaction.response.send_message(f"沒問題，主人！八千代已經刪除這個伺服器的日誌頻道設定了！嘿嘿~☆")
            await log_to_channel(guild, f"🗑️ **設定更新：** {interaction.user.name} 刪除了伺服器日誌頻道設定。")
        else:
            await interaction.response.send_message(f"主人，這個伺服器本來就沒有設定日誌頻道喔～嘿嘿~☆", ephemeral=True)
    
    else:
        # 4. 錯誤的操作
        await interaction.response.send_message(f"哎呀，主人，操作只能是 `set` 或 `del` 喔！", ephemeral=True)

# --- 啟動 Bot ---
if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
else:
    print("❌ 錯誤：未找到 DISCORD_TOKEN 環境變數。")
