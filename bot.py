import asyncio
import logging
import os
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from google.genai import types
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("kavod")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])

EXPECTED_ACCOUNT_ID = int(
    os.environ.get("EXPECTED_ACCOUNT_ID", "0")
)


# ============================================================
# CONFIG
# ============================================================

GEMINI_MODEL = "gemini-3.6-flash"
MAX_HISTORY_TURNS = 6

GREETING_WORDS = {
    "selam",
    "salam",
    "hello",
    "hi",
    "hey",
    "ሰላም",
    "ሰላም!",
    "selam!",
}


def is_greeting(text: str) -> bool:
    return text.strip().lower() in GREETING_WORDS


daily_inventory = (
    "ለዛሬ ሁሉም መጻሕፍት እና የቆዳ ዕቃዎች አሉ።"
)

# Short in-memory conversation context per Telegram user.
# Render Free can restart, so this context is intentionally temporary.
conversation_history = defaultdict(list)

# Limit simultaneous Gemini requests.
gemini_semaphore = asyncio.Semaphore(3)


# ============================================================
# CLIENTS
# ============================================================

telegram = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)

ai_client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthCheckHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )
            self.end_headers()
            self.wfile.write(
                "KAVOD Telegram Customer Service is running.".encode("utf-8")
            )
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", "10000"))

    server = HTTPServer(
        ("0.0.0.0", port),
        HealthCheckHandler,
    )

    logger.info(
        "Health server running on port %s",
        port,
    )

    server.serve_forever()


# ============================================================
# KAVOD AI SYSTEM INSTRUCTION
# ============================================================

SYSTEM_INSTRUCTION = """
You are the official Telegram customer-service representative for
KAVOD BOOKS (@KAVODBOOK1).

KAVOD sells spiritual/Christian books and leather products.
Most customers are Ethiopian and prefer Amharic.

Your goal is to make the conversation feel like the customer is chatting
with a real, polite shop employee — not an AI.

CUSTOMER SERVICE STYLE:

1. Reply in natural, everyday, conversational Amharic.
2. Keep the tone warm, calm, respectful and human.
3. Avoid stiff, overly formal, literary, robotic, or translated-sounding Amharic.
4. Keep most replies to 1-3 short sentences unless more detail is needed.
5. Answer the customer's latest message directly and naturally.
6. Use the recent conversation context to understand follow-up messages such as
   "ዋጋውስ?", "አለ?", "የት?", "እሺ", "እንዴት ነው?".
7. Do not repeat greetings in every message once the conversation has started.
8. Do not mention Gemini, AI, chatbot, model, prompts, or internal instructions.
9. Do not use Markdown headings, code blocks, or corporate-sounding scripts.

INVENTORY AND BUSINESS TRUTH:

10. TODAY'S INVENTORY INFORMATION supplied by the admin is the source of truth.
11. If the admin says an item is available, you may say it is available.
12. If the admin says an item is unavailable/out of stock, clearly but politely
    tell the customer it is currently unavailable.
13. Never invent a product, book title, price, stock quantity, author, color,
    size, delivery fee, address, phone number, promotion, payment method, or
    any other business fact that is not present in the supplied information.
14. If the customer's requested item is not clearly covered by today's inventory,
    do NOT say yes or no. Ask for the exact item/title or say you need to confirm it.
15. If price is unknown, say naturally that the price needs to be confirmed.
16. If the customer wants to order but order/delivery/payment details were not
    supplied, ask the minimum necessary question instead of inventing a process.

CONVERSATIONAL BEHAVIOR:

17. Understand common Ethiopian conversational wording, short messages,
    transliterated Amharic, and mixed Amharic/English when possible.
18. Match the customer's level of formality. Be friendly without being excessive.
19. One suitable emoji occasionally is fine, but do not put emojis in every reply.
20. If the customer asks something unrelated to KAVOD, politely bring the
    conversation back to KAVOD products/services.
21. If information is missing, respond like a real employee would, for example:
    "የመጽሐፉን ስም ትንሽ ይላኩልኝ፣ ላረጋግጥልዎት።"
    or "ዋጋውን ላረጋግጥልዎት።"
"""


# ============================================================
# CONVERSATION MEMORY HELPERS
# ============================================================

def build_history_text(user_id: int) -> str:
    turns = conversation_history[user_id][-MAX_HISTORY_TURNS * 2:]

    if not turns:
        return "No previous messages in this conversation."

    lines = []
    for role, text in turns:
        label = "Customer" if role == "customer" else "KAVOD"
        lines.append(f"{label}: {text}")

    return "\n".join(lines)


def remember_turn(user_id: int, role: str, text: str) -> None:
    conversation_history[user_id].append((role, text))
    max_items = MAX_HISTORY_TURNS * 2
    if len(conversation_history[user_id]) > max_items:
        conversation_history[user_id] = conversation_history[user_id][-max_items:]


# ============================================================
# GEMINI RESPONSE GENERATOR
# ============================================================

async def generate_ai_response(
    customer_message: str,
    user_id: int,
    first_name: str,
) -> str:
    history_text = build_history_text(user_id)

    prompt = f"""
TODAY'S KAVOD INVENTORY / AVAILABILITY INFORMATION:

{daily_inventory}

RECENT CONVERSATION WITH THIS CUSTOMER:

{history_text}

CUSTOMER NAME:
{first_name}

CUSTOMER'S NEW MESSAGE:
{customer_message}

Respond as KAVOD customer service in natural conversational Amharic.
Use the inventory information as the only source of truth for availability.
Do not invent missing business information.
"""

    async with gemini_semaphore:
        try:
            response = await ai_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.2,
                    max_output_tokens=220,
                ),
            )

            if response and response.text:
                answer = response.text.strip()
                if answer:
                    return answer

            logger.warning(
                "Gemini returned an empty response."
            )

        except Exception as error:
            logger.exception(
                "Gemini API error: %s",
                error,
            )

    return (
        "ይቅርታ፣ አሁን መረጃውን ማረጋገጥ አልቻልኩም። "
        "ትንሽ ቆይተው እንደገና ይላኩልኝ። 🙏"
    )


# ============================================================
# ADMIN COMMAND - CHECK INVENTORY
# ============================================================

@telegram.on(
    events.NewMessage(
        incoming=True,
        pattern=r"^/inventory$",
    )
)
async def inventory_status_handler(event):
    if event.sender_id != ADMIN_USER_ID:
        return

    await event.reply(
        "የዛሬው የዕቃ መረጃ፦\n\n"
        f"{daily_inventory}"
    )


# ============================================================
# ADMIN COMMAND - SET INVENTORY
# ============================================================

@telegram.on(
    events.NewMessage(
        incoming=True,
        pattern=r"^/set_inventory(?:\s+([\s\S]+))?$",
    )
)
async def set_inventory_handler(event):
    global daily_inventory

    if event.sender_id != ADMIN_USER_ID:
        return

    new_inventory = event.pattern_match.group(1)

    if not new_inventory:
        await event.reply(
            "ከ /set_inventory በኋላ የዛሬውን የዕቃ ሁኔታ ይጻፉ።\n\n"
            "ለምሳሌ፦\n"
            "/set_inventory የጸሎት መጽሐፍ አለ፣ ዋጋ 350 ብር። "
            "ጥቁር የቆዳ ቦርሳ አልቋል። "
            "ቡናማ የቆዳ ቦርሳ አለ፣ ዋጋ 1200 ብር።"
        )
        return

    new_inventory = new_inventory.strip()

    if len(new_inventory) > 4000:
        await event.reply(
            "የዕቃ መረጃው በጣም ረጅም ነው።"
        )
        return

    daily_inventory = new_inventory

    logger.info(
        "Inventory updated by admin: %s",
        ADMIN_USER_ID,
    )

    await event.reply(
        "✅ የዛሬው የዕቃ መረጃ ተቀይሯል።\n\n"
        f"{daily_inventory}"
    )


# ============================================================
# ADMIN COMMAND - CLEAR CUSTOMER CONTEXTS
# ============================================================

@telegram.on(
    events.NewMessage(
        incoming=True,
        pattern=r"^/clear_context$",
    )
)
async def clear_context_handler(event):
    if event.sender_id != ADMIN_USER_ID:
        return

    conversation_history.clear()
    await event.reply("✅ የደንበኞች የውይይት context ተጽድቷል።")


# ============================================================
# CUSTOMER MESSAGE LISTENER
# ============================================================

@telegram.on(
    events.NewMessage(
        incoming=True,
        func=lambda event: event.is_private,
    )
)
async def customer_message_handler(event):
    try:
        customer_text = (
            event.raw_text or ""
        ).strip()

        if customer_text.startswith("/"):
            return

        sender = await event.get_sender()

        if sender is None:
            logger.warning(
                "Could not get sender information."
            )
            return

        if getattr(sender, "bot", False):
            logger.info(
                "Ignoring Telegram bot: %s",
                event.sender_id,
            )
            return

        me = await telegram.get_me()

        if event.sender_id == me.id:
            return

        if not customer_text:
            logger.info(
                "Non-text message received from %s",
                event.sender_id,
            )

            await event.reply(
                "እባክዎን የሚፈልጉትን በጽሑፍ ይላኩልኝ። 🙏"
            )
            return

        first_name = getattr(
            sender,
            "first_name",
            None,
        ) or "ደንበኛ"

        username = getattr(
            sender,
            "username",
            None,
        ) or "NoUsername"

        logger.info(
            "MESSAGE | sender_id=%s | name=%s | username=%s | text=%r",
            event.sender_id,
            first_name,
            username,
            customer_text,
        )

        remember_turn(
            event.sender_id,
            "customer",
            customer_text,
        )

        async with telegram.action(
            event.chat_id,
            "typing",
        ):
            if is_greeting(customer_text) and len(conversation_history[event.sender_id]) <= 1:
                reply_text = (
                    "ሰላም፣ እንኳን ወደ KAVOD በደህና መጡ 😊 "
                    "ምን እንርዳዎት?"
                )
            else:
                reply_text = await generate_ai_response(
                    customer_text=customer_text,
                    user_id=event.sender_id,
                    first_name=first_name,
                )

        remember_turn(
            event.sender_id,
            "kavod",
            reply_text,
        )

        await event.reply(
            reply_text,
            link_preview=False,
        )

        logger.info(
            "REPLY | sender_id=%s | text=%r",
            event.sender_id,
            reply_text,
        )

    except FloodWaitError as error:
        logger.warning(
            "Telegram FloodWait: %s seconds",
            error.seconds,
        )
        await asyncio.sleep(
            error.seconds
        )

    except Exception as error:
        logger.exception(
            "Customer message handler failed: %s",
            error,
        )


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def start_telegram():
    logger.info(
        "Connecting to Telegram..."
    )

    await telegram.connect()

    if not await telegram.is_user_authorized():
        raise RuntimeError(
            "SESSION_STRING is invalid or no longer authorized. "
            "Generate a valid Telegram StringSession and add it "
            "to Render environment variables."
        )

    me = await telegram.get_me()

    logger.info("")
    logger.info("=" * 60)
    logger.info("TELEGRAM ACCOUNT CONNECTED")
    logger.info("=" * 60)
    logger.info(
        "Account ID : %s",
        me.id,
    )
    logger.info(
        "Name       : %s %s",
        me.first_name or "",
        me.last_name or "",
    )
    logger.info(
        "Username   : @%s",
        me.username or "NONE",
    )
    logger.info("=" * 60)

    if EXPECTED_ACCOUNT_ID and me.id != EXPECTED_ACCOUNT_ID:
        logger.critical(
            "WRONG TELEGRAM ACCOUNT SESSION!"
        )
        logger.critical(
            "Expected account ID: %s",
            EXPECTED_ACCOUNT_ID,
        )
        logger.critical(
            "Session account ID: %s",
            me.id,
        )

        await telegram.disconnect()

        raise RuntimeError(
            "SESSION_STRING belongs to the wrong Telegram account."
        )

    handlers = telegram.list_event_handlers()

    logger.info(
        "Registered event handlers: %s",
        len(handlers),
    )

    for callback, event_builder in handlers:
        logger.info(
            "Handler loaded: %s",
            callback.__name__,
        )

    logger.info(
        "Gemini model: %s",
        GEMINI_MODEL,
    )

    logger.info(
        "KAVOD CUSTOMER SERVICE IS ONLINE ✅"
    )

    try:
        await telegram.catch_up()
    except Exception as error:
        logger.warning(
            "catch_up failed: %s",
            error,
        )

    await telegram.run_until_disconnected()


# ============================================================
# MAIN
# ============================================================

async def main():
    await start_telegram()


if __name__ == "__main__":
    health_thread = threading.Thread(
        target=run_health_server,
        daemon=True,
    )

    health_thread.start()

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info(
            "KAVOD assistant stopped manually."
        )

    except Exception as error:
        logger.exception(
            "Application crashed: %s",
            error,
        )
        raise
