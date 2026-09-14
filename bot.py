import asyncio
from google import genai
from telethon import TelegramClient, events

# Credentials from my.telegram.org
API_ID = 37292292  # Your numeric api_id
API_HASH = "a53e3c11637b9378bfe82af1f0678524"  # Your api_hash string

GEMINI_API_KEY = "your_gemini_api_key_here"
ADMIN_USER_ID = 6873889384

daily_inventory = "ለዛሬ ሁሉም መጻሕፍት እና የቆዳ ዕቃዎች አሉ።"

ai_client = genai.Client(api_key=GEMINI_API_KEY)
client = TelegramClient("kavod_session", API_ID, API_HASH)


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


print("Starting Kavod Personal Assistant Userbot...")
client.start()
client.run_until_disconnected()
