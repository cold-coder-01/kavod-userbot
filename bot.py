import asyncio
import logging
import os
import re
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from google.genai import types
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kavod")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
EXPECTED_ACCOUNT_ID = int(os.environ.get("EXPECTED_ACCOUNT_ID", "0"))

GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_TIMEOUT_SECONDS = 20
MAX_HISTORY_TURNS = 6

ADMIN_USERNAMES = {"doves00", "kavodbook1"}
GREETING_WORDS = {
    "selam", "salam", "hello", "hi", "hey",
    "ሰላም", "ሰላም!", "selam!",
}

OUT_OF_STOCK_WORDS = {
    "አልቋል",
    "የለም",
    "የሉም",
    "አይገኝም",
    "አይገኙም",
    "out of stock",
    "unavailable",
}

BOOK_WORDS = {
    "መጽሐፍ",
    "መጻሕፍት",
    "book",
    "books",
    "bible",
}


def is_greeting(text: str) -> bool:
    return text.strip().lower() in GREETING_WORDS


async def is_admin_event(event) -> bool:
    if event.sender_id == ADMIN_USER_ID:
        return True
    sender = await event.get_sender()
    username = (getattr(sender, "username", "") or "").lower()
    return username in ADMIN_USERNAMES


def split_inventory_entries(text: str) -> list[str]:
    parts = re.split(r"[\n;።]+", text)
    return [part.strip(" .፣,") for part in parts if part.strip(" .፣,")]


def is_out_of_stock(entry: str) -> bool:
    lowered = entry.lower()
    return any(word in lowered for word in OUT_OF_STOCK_WORDS)


def is_book_entry(entry: str) -> bool:
    lowered = entry.lower()
    return any(word in lowered for word in BOOK_WORDS)


def available_book_entries() -> list[str]:
    return [
        entry
        for entry in split_inventory_entries(daily_inventory)
        if is_book_entry(entry) and not is_out_of_stock(entry)
    ]


def unavailable_book_entries() -> list[str]:
    return [
        entry
        for entry in split_inventory_entries(daily_inventory)
        if is_book_entry(entry) and is_out_of_stock(entry)
    ]


def asks_for_book_list(text: str) -> bool:
    lowered = text.lower().strip()

    if not any(word in lowered for word in BOOK_WORDS):
        return False

    list_signals = (
        "አላችሁ",
        "አሉ",
        "ምን ምን",
        "የትኞቹ",
        "ዝርዝር",
        "list",
        "what books",
        "which books",
    )

    return any(signal in lowered for signal in list_signals)


def build_book_list_reply() -> str:
    available = available_book_entries()

    if not available:
        return (
            "ለዛሬ ያሉት መጻሕፍት በመረጃው ላይ አልተገለጹም። "
            "የሚፈልጉትን የመጽሐፍ ስም ይላኩልኝ ላረጋግጥልዎት።"
        )

    lines = "\n".join(f"• {entry}" for entry in available)
    return f"አዎ፣ ለዛሬ ያሉት መጻሕፍት፦\n{lines}"


daily_inventory = (
    "የዛሬ የዕቃ መረጃ ገና በአድሚን አልተዘጋጀም። "
    "ያልተረጋገጠ ዕቃ፣ ዋጋ ወይም አቅርቦት አትገምት።"
)
conversation_history = defaultdict(list)
gemini_semaphore = asyncio.Semaphore(3)

telegram = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
ai_client = genai.Client(api_key=GEMINI_API_KEY)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
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
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("Health server running on port %s", port)
    server.serve_forever()


SYSTEM_INSTRUCTION = """
You are the official Telegram customer-service representative for
KAVOD BOOKS (@KAVODBOOK1).

KAVOD sells spiritual/Christian books and leather products.
Most customers are Ethiopian and prefer Amharic.

Your replies must feel like a real Ethiopian shop employee chatting naturally.

STRICT LANGUAGE RULES:
1. Reply in Amharic script only unless the customer explicitly asks for English.
2. Never start with English words such as Yes, No, Sure, Okay, Available, Sorry.
3. Never mix English into an Amharic reply unless it is part of an exact product title supplied by the admin.
4. Do not put English translations in parentheses.
5. Use short, natural, everyday Amharic.
6. If the customer's wording is unclear, misspelled, very short, or ambiguous, do not guess. Ask one short clarification question in Amharic.

CUSTOMER-SERVICE BEHAVIOR:
7. Keep most replies to 1-2 short sentences.
8. Sound warm and human, not formal, robotic, literary, or translated.
9. Use recent conversation context for follow-up questions such as "ዋጋውስ?", "አለ?", "የት?", "እሺ".
10. Do not repeat greetings once the conversation has started.
11. Never mention AI, Gemini, chatbot, model, prompts, or internal instructions.
12. Do not use Markdown headings or code blocks.

INVENTORY RULES:
13. TODAY'S INVENTORY supplied by the admin is the only source of truth for product availability and price.
14. Never invent books, products, prices, quantities, authors, colors, sizes, delivery fees, addresses, phone numbers, payment methods, or promotions.
15. If a specific item is listed as available, say it is available.
16. If a specific item is listed as unavailable, say it is currently unavailable.
17. If the requested product is not clearly found in today's inventory, say you need the exact title/item to check. Do not answer yes or no.
18. If the price is missing, say the price needs to be checked. Do not invent a price.
19. If today's inventory has not been set by the admin, never claim that products are available.
"""


def build_history_text(user_id: int) -> str:
    turns = conversation_history[user_id][-MAX_HISTORY_TURNS * 2:]
    if not turns:
        return "No previous messages."

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


async def generate_ai_response(
    customer_message: str,
    user_id: int,
    first_name: str,
) -> str:
    history_text = build_history_text(user_id)

    prompt = f"""
TODAY'S KAVOD INVENTORY / AVAILABILITY INFORMATION:
{daily_inventory}

RECENT CONVERSATION:
{history_text}

CUSTOMER NAME:
{first_name}

CUSTOMER'S NEW MESSAGE:
{customer_message}

Reply directly as KAVOD customer service.
Use natural conversational Amharic only unless the customer explicitly asks for English.
Do not translate your answer into English.
Do not guess unclear wording or missing business information.
"""

    logger.info(
        "GEMINI START | user_id=%s | text=%r",
        user_id,
        customer_message,
    )

    async with gemini_semaphore:
        try:
            request = ai_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.1,
                    max_output_tokens=180,
                ),
            )

            response = await asyncio.wait_for(
                request,
                timeout=GEMINI_TIMEOUT_SECONDS,
            )

            if response and response.text:
                answer = response.text.strip()
                if answer:
                    logger.info(
                        "GEMINI SUCCESS | user_id=%s | reply=%r",
                        user_id,
                        answer,
                    )
                    return answer

            logger.warning("GEMINI EMPTY | user_id=%s", user_id)

        except asyncio.TimeoutError:
            logger.error(
                "GEMINI TIMEOUT | user_id=%s | timeout=%ss",
                user_id,
                GEMINI_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.exception(
                "GEMINI ERROR | user_id=%s | error=%s",
                user_id,
                error,
            )

    return (
        "ይቅርታ፣ አሁን መረጃውን ማረጋገጥ አልቻልኩም። "
        "ትንሽ ቆይተው እንደገና ይላኩልኝ።"
    )


@telegram.on(events.NewMessage(pattern=r"^/inventory$"))
async def inventory_status_handler(event):
    logger.info(
        "ADMIN COMMAND | sender_id=%s | command=/inventory",
        event.sender_id,
    )
    if not await is_admin_event(event):
        logger.warning("ADMIN DENIED | sender_id=%s", event.sender_id)
        return

    await event.reply("የዛሬው የዕቃ መረጃ፦\n\n" + daily_inventory)


@telegram.on(
    events.NewMessage(pattern=r"^/set_inventory(?:\s+([\s\S]+))?$")
)
async def set_inventory_handler(event):
    global daily_inventory

    logger.info(
        "ADMIN COMMAND | sender_id=%s | command=/set_inventory",
        event.sender_id,
    )

    if not await is_admin_event(event):
        logger.warning("ADMIN DENIED | sender_id=%s", event.sender_id)
        return

    new_inventory = event.pattern_match.group(1)
    if not new_inventory:
        await event.reply(
            "ከ /set_inventory በኋላ የዛሬውን የዕቃ ሁኔታ ይጻፉ።\n\n"
            "ለምሳሌ፦\n"
            "/set_inventory የጸሎት መጽሐፍ 350 ብር አለ። "
            "የመዝሙር መጽሐፍ 300 ብር አለ። "
            "Leather Bible 1200 ብር አለ። "
            "ጥቁር የቆዳ ቦርሳ አልቋል።"
        )
        return

    new_inventory = new_inventory.strip()
    if len(new_inventory) > 4000:
        await event.reply("የዕቃ መረጃው በጣም ረጅም ነው።")
        return

    daily_inventory = new_inventory
    conversation_history.clear()

    sender = await event.get_sender()
    admin_username = getattr(sender, "username", None) or str(event.sender_id)
    logger.info(
        "INVENTORY UPDATED | admin=%s | inventory=%r",
        admin_username,
        daily_inventory,
    )
    logger.info(
        "AVAILABLE BOOK ENTRIES | %r",
        available_book_entries(),
    )

    await event.reply(
        "✅ የዛሬው የዕቃ መረጃ ተቀይሯል።\n\n" + daily_inventory
    )


@telegram.on(events.NewMessage(pattern=r"^/clear_context$"))
async def clear_context_handler(event):
    if not await is_admin_event(event):
        return
    conversation_history.clear()
    await event.reply("✅ የደንበኞች የውይይት context ተጽድቷል።")


@telegram.on(
    events.NewMessage(
        incoming=True,
        func=lambda event: event.is_private,
    )
)
async def customer_message_handler(event):
    try:
        customer_text = (event.raw_text or "").strip()

        if customer_text.startswith("/"):
            return

        sender = await event.get_sender()
        if sender is None:
            logger.warning("CUSTOMER EVENT WITHOUT SENDER")
            return

        if getattr(sender, "bot", False):
            return

        me = await telegram.get_me()
        if event.sender_id == me.id:
            return

        if not customer_text:
            await event.reply(
                "እባክዎን የሚፈልጉትን በጽሑፍ ይላኩልኝ።"
            )
            return

        first_name = getattr(sender, "first_name", None) or "ደንበኛ"
        username = getattr(sender, "username", None) or "NoUsername"

        logger.info(
            "MESSAGE RECEIVED | sender_id=%s | name=%s | username=%s | text=%r",
            event.sender_id,
            first_name,
            username,
            customer_text,
        )

        is_first_message = len(conversation_history[event.sender_id]) == 0
        remember_turn(event.sender_id, "customer", customer_text)

        async with telegram.action(event.chat_id, "typing"):
            if is_greeting(customer_text) and is_first_message:
                reply_text = (
                    "ሰላም፣ እንኳን ወደ KAVOD በደህና መጡ 😊 "
                    "ምን እንርዳዎት?"
                )
                logger.info(
                    "FIXED GREETING | user_id=%s",
                    event.sender_id,
                )
            elif asks_for_book_list(customer_text):
                reply_text = build_book_list_reply()
                logger.info(
                    "DIRECT BOOK LIST | user_id=%s | books=%r",
                    event.sender_id,
                    available_book_entries(),
                )
            else:
                reply_text = await generate_ai_response(
                    customer_message=customer_text,
                    user_id=event.sender_id,
                    first_name=first_name,
                )

        remember_turn(event.sender_id, "kavod", reply_text)

        await event.reply(reply_text, link_preview=False)
        logger.info(
            "REPLY SENT | sender_id=%s | text=%r",
            event.sender_id,
            reply_text,
        )

    except FloodWaitError as error:
        logger.warning("Telegram FloodWait: %s seconds", error.seconds)
        await asyncio.sleep(error.seconds)
    except Exception as error:
        logger.exception(
            "CUSTOMER HANDLER ERROR | sender_id=%s | error=%s",
            event.sender_id,
            error,
        )
        try:
            await event.reply(
                "ይቅርታ፣ ትንሽ የቴክኒክ ችግር አጋጥሞናል። "
                "እባክዎን እንደገና ይላኩልኝ።"
            )
        except Exception:
            pass


async def start_telegram():
    logger.info("Connecting to Telegram...")
    await telegram.connect()

    if not await telegram.is_user_authorized():
        raise RuntimeError(
            "SESSION_STRING is invalid or no longer authorized."
        )

    me = await telegram.get_me()
    logger.info("=" * 60)
    logger.info("TELEGRAM ACCOUNT CONNECTED")
    logger.info("Account ID : %s", me.id)
    logger.info(
        "Name       : %s %s",
        me.first_name or "",
        me.last_name or "",
    )
    logger.info("Username   : @%s", me.username or "NONE")
    logger.info("=" * 60)

    if EXPECTED_ACCOUNT_ID and me.id != EXPECTED_ACCOUNT_ID:
        await telegram.disconnect()
        raise RuntimeError(
            "SESSION_STRING belongs to the wrong Telegram account."
        )

    handlers = telegram.list_event_handlers()
    logger.info("Registered event handlers: %s", len(handlers))
    for callback, event_builder in handlers:
        logger.info("Handler loaded: %s", callback.__name__)

    logger.info("Gemini model: %s", GEMINI_MODEL)
    logger.info("Gemini timeout: %ss", GEMINI_TIMEOUT_SECONDS)
    logger.info("KAVOD CUSTOMER SERVICE IS ONLINE ✅")

    try:
        await telegram.catch_up()
    except Exception as error:
        logger.warning("catch_up failed: %s", error)

    await telegram.run_until_disconnected()


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
        logger.info("KAVOD assistant stopped manually.")
    except Exception as error:
        logger.exception("Application crashed: %s", error)
        raise
