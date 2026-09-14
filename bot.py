import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import threading
from google import genai
from telethon import TelegramClient, events

# Credentials from my.telegram.org
API_ID = 37292292
API_HASH = "a53e3c11637b9378bfe82af1f0678524"  # Replace with your api_hash string

GEMINI_API_KEY = "AQ.Ab8RN6JwVwUGPnHOtO8hUU-zpAQe0jtt_aGN2fqbWnrjCH-Ftg"  # Replace with your Gemini API key
ADMIN_USER_ID = 6873889384

daily_inventory = "ለዛሬ ሁሉም መጻሕፍት እና የቆዳ ዕቃዎች አሉ።"

ai_client = genai.Client(api_key=GEMINI_API_KEY)
client = TelegramClient("kavod_session", API_ID, API_HASH)


# Simple HTTP server to satisfy Render Web Service health checks
class HealthCheckHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Kavod Userbot is running.")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


@client.on(events.NewMessage(pattern=r"^/set_inventory(?:\s+(.*))?"))
async def set_inventory_handler(event):
    global daily_inventory
    if event.sender_id != ADMIN_USER_ID:
        return

    new_status = event.pattern_match.group(1)
    if not new_status:
        await event.reply(
            f"እባክዎን የዕቃውን ሁኔታ ያስገቡ።\nየአሁኑ ሁኔታ፦ {daily_inventory}"
        )
        return

    daily_inventory = new_status.strip()
    await event.reply(
        f"የዕቃው ሁኔታ በተሳካ ሁኔታ ተቀይሯል! ✅\nአዲሱ ሁኔታ፦ {daily_inventory}"
    )


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handle_customer_message(event):
    if event.text and event.text.startswith("/"):
        return

    sender = await event.get_sender()
    if sender.bot or sender.is_self:
        return

    user_text = event.raw_text

    prompt = f"""
    You are a polite, helpful Amharic-speaking customer service assistant for a spiritual books and leather goods business (@KAVODBOOK1).
    
    Current daily inventory and stock status: {daily_inventory}
    
    Customer question: {user_text}
    
    Instructions:
    - Respond strictly in warm, natural Amharic.
    - Be clear, concise, and professional.
    - Answer based on the current stock status provided above.
    """

    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        if response.text:
            await event.reply(response.text)
    except Exception as e:
        print(f"Error calling Gemini API: {e}")


if __name__ == "__main__":
    # Start background thread for Render port binding
    threading.Thread(target=run_health_server, daemon=True).start()

    print("Starting Kavod Personal Assistant Userbot...")
    client.start()
    client.run_until_disconnected()
