from groq import Groq
import os

api_key = os.getenv("GROQ_API_KEY")  # або прямо вставь ключ
client = Groq(api_key=api_key)

print("Тестую llama-3.3-70b-versatile...")
try:
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=10
    )
    print("✅ Вона працює!")
except Exception as e:
    print(f"❌ Помилка: {e}")
