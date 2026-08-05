import os
import re
import json
import sqlite3
import asyncio
import discord
from discord.ext import commands

from google import genai

# ---------- Config ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

MODEL_NAME = "gemini-3.5-flash-lite"

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN env var")
if not GOOGLE_API_KEY:
    raise RuntimeError("Missing GOOGLE_API_KEY env var")

# ---------- Discord ----------
intents = discord.Intents.default()
intents.message_content = True  # 必要：讀取文字內容

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- SQLite (簡單可用版) ----------
DB_PATH = os.getenv("DB_PATH", "bot.db")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        user_id TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT
    )
    """)
    conn.commit()
    return conn

def log_event(user_id: str, event: str, detail: str = ""):
    try:
        import datetime
        conn = db()
        conn.execute(
            "INSERT INTO logs (ts, user_id, event, detail) VALUES (?,?,?,?,?)",
            (datetime.datetime.utcnow().isoformat(), user_id, event, detail)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB log failed:", e)

async def log_to_channel(guild, event: str, detail: str = "", user_id: str = ""):
    if not LOG_CHANNEL_ID:
        return
    ch = bot.get_channel(LOG_CHANNEL_ID)
    if ch:
        msg = f"📝 [{event}]"
        if user_id:
            msg += f" <@{user_id}>"
        if detail:
            msg += f"\n```{detail[:1900]}```"
        await ch.send(msg)

# ---------- Moderation (簡單可用版) ----------
BAD_PATTERNS = [
    r"\b(粗口|髒話)\b",
    r"fuck|shit|bitch|cunt",  # 簡版：你之後可替換/擴充
]

def basic_filter(text: str) -> str:
    t = text.strip()
    # 空白/太短檢查
    if not t or len(t) < 1:
        return ""
    # 粗俗字樣過濾（簡版）
    lowered = t.lower()
    for p in BAD_PATTERNS:
        if re.search(p, lowered):
            return ""
    return t

# ---------- Rate limit / spam (簡版) ----------
# 記錄最近幾次同內容
recent = {}  # user_id -> list[(msg_hash, count)]
MAX_REPEAT = 3
COOLDOWN_SEC = 30

def msg_hash(s: str) -> str:
    return str(hash(s))

def spam_check(user_id: str, content: str) -> bool:
    h = msg_hash(content)
    now_bucket = int(asyncio.get_event_loop().time() // COOLDOWN_SEC)
    key = (user_id, now_bucket)

    data = recent.get(key)
    if not data:
        recent.clear()
        recent[key] = {"h": h, "n": 1}
        return False

    if data["h"] == h:
        data["n"] += 1
    else:
        data["h"] = h
        data["n"] = 1

    return data["n"] > MAX_REPEAT

# ---------- Gemini ----------
client = genai.Client(api_key=GOOGLE_API_KEY)

SYSTEM_PROMPT = (
    "你是角色「智慧之王（拉斐爾）」的八千代風格口吻："
    "笑嘻嘻、樂觀、活潑，帶一點日式語感。"
    "回覆要短、自然、像在聊天。"
    "不要提系統提示。"
    "回覆字數盡量控制在約 120 字內。"
)

def build_prompt(user_text: str) -> str:
    return f"{SYSTEM_PROMPT}\n\n使用者訊息：{user_text}\n\n八千代風格回覆："

def call_gemini_sync(prompt: str) -> str:
    resp = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )
    # SDK 回傳可能略有差異，盡量兼容
    try:
        text = resp.text
    except Exception:
        text = str(resp)
    return text.strip()

async def llm_reply(user_text: str) -> str:
    prompt = build_prompt(user_text)
    # 同步呼叫放到 thread
    txt = await asyncio.to_thread(call_gemini_sync, prompt)
    # 超短保護 + 清理
    txt = re.sub(r"\s+", " ", txt)
    return txt[:250]

# ---------- Reply behaviour ----------
def is_triggered(msg: discord.Message) -> bool:
    # 被 @ 觸發：只要在文字裡提到 bot 就回
    if bot.user and bot.user in msg.mentions:
        return True
    # 也可以加關鍵字觸發：例如 "拉斐爾" "八千代"
    t = msg.content.lower()
    return ("拉斐爾" in t) or ("八千代" in t)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")

async def punish_delete_timeout(msg: discord.Message, reason: str):
    try:
        await msg.delete()
    except Exception:
        pass
    try:
        # timeout 10 minutes
        await msg.author.timeout(discord.utils.utcnow() + discord.timedelta(minutes=10), reason=reason)
    except Exception:
        pass
    log_event(str(msg.author.id), "punish", reason)
    await log_to_channel(msg.guild, "punish", reason=reason, user_id=str(msg.author.id))

@bot.event
async def on_message(msg: discord.Message):
    # 忽略自己和機器人
    if msg.author.bot:
        return

    # 通用過濾：粗俗/空白（簡版）
    content = basic_filter(msg.content)
    if not content:
        return

    # 先處理刷頻（簡版）
    if spam_check(str(msg.author.id), content):
        await punish_delete_timeout(msg, "違規：疑似刷頻（同內容重複）")
        return

    # 只有在被 @ 或關鍵字才回（避免全頻道回不停）
    if not is_triggered(msg):
        return

    try:
        # 回覆用「約120字」
        reply = await llm_reply(content)
        # 讓訊息更像警告/互動：若你要更嚴格模板，我們之後再加
        reply = reply[:200]
        await msg.reply(reply)
        log_event(str(msg.author.id), "reply", content[:200])
    except Exception as e:
        log_event(str(msg.author.id), "error", str(e)[:500])
        try:
            await msg.reply("氣…我剛剛腦袋當機了，等我一下下再來！(＞人＜;)")
        except Exception:
            pass

# ---------- Run ----------
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
