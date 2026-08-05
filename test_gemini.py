import os
from google import genai  # 依你目前套件的用法調整
client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

resp = client.models.generate_content(
    model=model,
    contents="你好！這是機器人測試。只回覆一句話。"
)

print(resp.text)
