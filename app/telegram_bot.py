import asyncio
import os
import httpx

WEB_APP_URL = os.getenv("WEB_APP_URL", "https://mytradingbot-production-076e.up.railway.app")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

async def telegram_polling():
    if not TOKEN:
        return
    api = f"https://api.telegram.org/bot{TOKEN}"
    offset = 0
    async with httpx.AsyncClient(timeout=35) as client:
        while True:
            try:
                r = await client.get(f"{api}/getUpdates", params={"timeout": 30, "offset": offset})
                data = r.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message or "text" not in message:
                        continue
                    if message["text"].split()[0].lower() not in ("/start", "/app"):
                        continue
                    chat_id = message["chat"]["id"]
                    keyboard = {
                        "inline_keyboard": [[
                            {"text": "🚀 Открыть MyTradingBot",
                             "web_app": {"url": WEB_APP_URL}}
                        ]]
                    }
                    await client.post(
                        f"{api}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": "MyTradingBot\n\nТерминал работает в PAPER MODE.\nОткрой приложение кнопкой ниже.",
                            "reply_markup": keyboard,
                        },
                    )
            except Exception:
                await asyncio.sleep(3)
