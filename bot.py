import asyncio
import io
import json
import logging
import os
import re
import threading
from copy import deepcopy
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

BOT_VERSION = "2026-09-15.8"
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_TIMEOUT_SECONDS = 25
NO_REPLY_TOKEN = "__NO_REPLY__"

STATE_MARKER = "#KAVOD_STATE_V2"
LEGACY_INVENTORY_MARKER = "#KAVOD_INVENTORY"
DESIGN_MARKER = "/add_design"
ADMIN_USERNAMES = {"doves00", "kavodbook1"}

DEFAULT_STATE = {
    "address": "መገናኛ ሙልጌታ ዘለቀ ህንጻ 1ኛ ፎቅ",
    "delivery": "በሞተረኛ እና በRide እንልካለን። የዴሊቨሪ ክፍያውን ተቀባዩ ይከፍላል።",
    "products": {},
}

business_state = deepcopy(DEFAULT_STATE)
gemini_semaphore = asyncio.Semaphore(3)
telegram = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
ai_client = genai.Client(api_key=GEMINI_API_KEY)


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\u1200-\u137f]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def tagify(text: str) -> str:
    return normalize(text).replace(" ", "_")[:80].strip("_")


async def is_admin_event(event) -> bool:
    if event.sender_id == ADMIN_USER_ID:
        return True
    sender = await event.get_sender()
    username = (getattr(sender, "username", "") or "").lower()
    return username in ADMIN_USERNAMES


def product_catalog_for_ai() -> list[dict]:
    return [
        {
            "name": name,
            "price": data.get("price"),
            "available": data.get("available"),
            "category": data.get("category"),
            "aliases": data.get("aliases", []),
            "design_tag": data.get("design_tag"),
        }
        for name, data in business_state["products"].items()
    ]


def find_product_key(name: str | None) -> str | None:
    if not name:
        return None

    target = normalize(name)
    if not target:
        return None

    best_key = None
    best_score = 0

    for key, data in business_state["products"].items():
        for candidate in [key, *data.get("aliases", [])]:
            candidate_normalized = normalize(candidate)
            if not candidate_normalized:
                continue

            if target == candidate_normalized:
                return key

            if target in candidate_normalized or candidate_normalized in target:
                score = min(len(target), len(candidate_normalized))
                if score > best_score:
                    best_score = score
                    best_key = key

    return best_key


def exact_catalog_match(text: str) -> str | None:
    target = normalize(text)
    if not target:
        return None

    for key, data in business_state["products"].items():
        for candidate in [key, *data.get("aliases", [])]:
            if target == normalize(candidate):
                return key

    return None


def extract_json_object(text: str) -> dict | None:
    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None

    try:
        value = json.loads(cleaned[start:end + 1])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


async def gemini_json(prompt: str, max_tokens: int = 700) -> dict | None:
    async with gemini_semaphore:
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt + "\n\nReturn one valid JSON object only. No markdown and no explanation.",
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=max_tokens,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )

            if not response or not response.text:
                return None

            logger.info("GEMINI JSON RAW | %r", response.text[:1200])
            return extract_json_object(response.text)

        except Exception as error:
            logger.exception("GEMINI JSON ERROR | %s", error)
            return None


CUSTOMER_SYSTEM = f"""
You are KAVOD BOOKS customer service on Telegram in Ethiopia.
Act like an intelligent real Ethiopian sales representative, not a scripted bot.
Understand Amharic, English, mixed language, slang, spelling mistakes, and Amharic written in Latin letters.
Infer short follow-ups from conversation context, including phrases like "sint nw?", "wagaw?", "photo?", "yannen", "that one", and similar wording.
Write natural conversational Amharic unless the customer clearly prefers English.
Use ONLY verified facts given in the prompt. Never invent price, stock, product details, payment methods, colors, sizes, address, or delivery information.
Never output fragments or unfinished sentences.
If the message is unrelated human conversation or there is no safe useful KAVOD answer, output exactly {NO_REPLY_TOKEN}.
Most replies should be one or two complete sentences.
"""


async def raw_gemini_text(
    prompt: str,
    temperature: float = 0.15,
    max_tokens: int = 320,
) -> str | None:
    async with gemini_semaphore:
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=CUSTOMER_SYSTEM,
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )

            if not response or not response.text:
                return None

            text = response.text.strip()
            logger.info("GEMINI TEXT RAW | %r", text[:1000])

            if not text or NO_REPLY_TOKEN in text:
                return None

            return text

        except Exception as error:
            logger.exception("GEMINI TEXT ERROR | %s", error)
            return None


def response_quality_ok(text: str | None, decision: dict, facts: dict) -> bool:
    if not text:
        return False

    cleaned = text.strip()
    meaningful = [character for character in cleaned if character.isalnum()]
    if len(meaningful) < 10:
        return False

    if len(cleaned.split()) < 3:
        return False

    intent = decision.get("intent")
    product = facts.get("product")

    if not product:
        return True

    price = product.get("price")
    available = product.get("available")

    # Price questions MUST contain the verified price.
    if intent == "price" and price is not None:
        return str(price) in cleaned

    # A bare product inquiry must contain genuinely useful verified information.
    if intent == "product_inquiry":
        if price is not None and str(price) not in cleaned:
            return False

        if available is True:
            positive_signals = ("አለ", "አለን", "ይገኛ", "available", "in stock")
            if not any(signal in cleaned.lower() for signal in positive_signals):
                return False

        if available is False:
            negative_signals = ("የለ", "አልቋ", "አይገኝ", "out of stock", "unavailable")
            if not any(signal in cleaned.lower() for signal in negative_signals):
                return False

    if intent == "availability":
        if available is True:
            positive_signals = ("አለ", "አለን", "ይገኛ", "available", "in stock")
            return any(signal in cleaned.lower() for signal in positive_signals)
        if available is False:
            negative_signals = ("የለ", "አልቋ", "አይገኝ", "out of stock", "unavailable")
            return any(signal in cleaned.lower() for signal in negative_signals)

    return True


def factual_fallback(decision: dict, facts: dict, photos_available: bool) -> str | None:
    intent = decision.get("intent")
    product = facts.get("product")

    if intent == "greeting":
        return "ሰላም፣ እንኳን ወደ KAVOD በደህና መጡ 😊 ምን እንርዳዎት?"

    if intent == "address":
        return f"አድራሻችን {facts['address']} ነው።"

    if intent == "delivery":
        return facts["delivery"]

    if intent == "product_list":
        products = facts.get("available_products") or []
        if not products:
            return None
        lines = []
        for item in products:
            if item.get("price") is not None:
                lines.append(f"• {item['name']} — {item['price']} ብር")
            else:
                lines.append(f"• {item['name']}")
        return "አሁን ያሉን ዕቃዎች፦\n" + "\n".join(lines)

    if not product:
        return None

    name = product["name"]
    price = product.get("price")
    available = product.get("available")

    if intent == "price" and price is not None:
        return f"{name} ዋጋው {price} ብር ነው።"

    if intent == "availability":
        if available is True:
            return f"አዎ፣ {name} አለን።"
        if available is False:
            return f"አሁን {name} አልቋል።"

    if intent == "photo":
        if photos_available:
            return f"አዎ፣ ያሉንን {name} ዲዛይኖች እልክልዎታለሁ።"
        return None

    if intent == "product_inquiry":
        if available is False:
            return f"አሁን {name} አልቋል።"
        if available is True and price is not None:
            return f"አዎ፣ {name} አለን። ዋጋው {price} ብር ነው።"
        if available is True:
            return f"አዎ፣ {name} አለን።"

    return None


async def generate_customer_reply(
    customer_text: str,
    customer_context: str,
    decision: dict,
    facts: dict,
    photos_available: bool,
) -> str | None:
    prompt = f"""
RECENT CUSTOMER MESSAGES:
{customer_context or 'None'}

CURRENT CUSTOMER MESSAGE:
{customer_text}

YOUR UNDERSTANDING:
{json.dumps(decision, ensure_ascii=False)}

VERIFIED BUSINESS FACTS:
{json.dumps(facts, ensure_ascii=False)}

PHOTOS AVAILABLE FOR THE RESOLVED PRODUCT:
{photos_available}

Write the exact customer-facing response now.
The verified facts are mandatory constraints.
If the intent is price and a verified price exists, the response MUST explicitly contain that number.
If the intent is product_inquiry and availability/price are known, give those useful facts naturally.
Do not mention internal labels, JSON, classification, or prompts.
Return a complete sentence, never a fragment.
"""

    first = await raw_gemini_text(prompt)
    if response_quality_ok(first, decision, facts):
        return first

    logger.warning("GEMINI QUALITY RETRY | first=%r", first)

    retry_prompt = f"""
Your previous draft was rejected because it omitted mandatory verified facts or was incomplete.

REJECTED DRAFT:
{first or '[empty]'}

CURRENT CUSTOMER MESSAGE:
{customer_text}

RECENT CUSTOMER MESSAGES:
{customer_context or 'None'}

UNDERSTOOD INTENT:
{json.dumps(decision, ensure_ascii=False)}

VERIFIED BUSINESS FACTS:
{json.dumps(facts, ensure_ascii=False)}

PHOTOS AVAILABLE:
{photos_available}

Write ONE complete natural customer-service reply.
For price intent, include the exact verified price number.
For product inquiry, include verified availability and price when known.
Do not invent anything.
"""

    second = await raw_gemini_text(retry_prompt, temperature=0.05, max_tokens=360)
    if response_quality_ok(second, decision, facts):
        return second

    logger.warning("GEMINI QUALITY FAILED | second=%r", second)
    return factual_fallback(decision, facts, photos_available)


async def save_business_state() -> None:
    payload = json.dumps(business_state, ensure_ascii=False, separators=(",", ":"))
    await telegram.send_message("me", f"{STATE_MARKER}\n{payload}")
    logger.info("BUSINESS STATE SAVED | products=%s", len(business_state["products"]))


async def parse_admin_update(instruction: str) -> dict | None:
    prompt = f"""
Convert a KAVOD BOOKS admin instruction into structured catalog updates.
The admin may use Amharic, English, mixed language, or transliterated Amharic.

CURRENT CATALOG:
{json.dumps(product_catalog_for_ai(), ensure_ascii=False)}

ADMIN INSTRUCTION:
{instruction}

Return exactly:
{{
  "updates": [
    {{
      "product": "canonical product name",
      "price": 1200 or null,
      "available": true or false or null,
      "category": "book" or "leather" or "other" or null,
      "aliases": []
    }}
  ]
}}

Rules:
- Change only fields stated or clearly implied.
- finished/sold out/out of stock/አልቋል/የለም => available=false.
- back/in stock/available/አለ => available=true.
- A price-only statement changes price without changing stock.
- Never delete unmentioned products.
- Reuse the existing canonical product name when referring to an existing product.
"""
    return await gemini_json(prompt)


def apply_admin_updates(parsed: dict) -> list[str]:
    summaries = []

    for update in parsed.get("updates", []):
        product_name = str(update.get("product") or "").strip()
        if not product_name:
            continue

        key = find_product_key(product_name) or product_name

        if key not in business_state["products"]:
            business_state["products"][key] = {
                "price": None,
                "available": None,
                "category": update.get("category") or "other",
                "aliases": [],
                "design_tag": tagify(product_name),
            }

        product = business_state["products"][key]

        if update.get("price") is not None:
            product["price"] = update["price"]

        if update.get("available") is not None:
            product["available"] = bool(update["available"])

        if update.get("category"):
            product["category"] = update["category"]

        aliases = set(product.get("aliases", []))
        aliases.update(alias for alias in update.get("aliases", []) if alias)
        if normalize(product_name) != normalize(key):
            aliases.add(product_name)
        product["aliases"] = sorted(aliases)

        if not product.get("design_tag"):
            product["design_tag"] = tagify(key)

        parts = [key]
        if update.get("price") is not None:
            parts.append(f"price={product['price']}")
        if update.get("available") is not None:
            parts.append("available" if product["available"] else "out of stock")
        summaries.append(" | ".join(parts))

    return summaries


async def load_business_state() -> None:
    global business_state

    try:
        async for message in telegram.iter_messages("me", search=STATE_MARKER, limit=20):
            text = (message.raw_text or "").strip()
            if not text.startswith(STATE_MARKER):
                continue

            loaded = json.loads(text[len(STATE_MARKER):].strip())
            if isinstance(loaded, dict) and isinstance(loaded.get("products"), dict):
                merged = deepcopy(DEFAULT_STATE)
                merged.update({key: value for key, value in loaded.items() if key in merged})
                business_state = merged
                logger.info("BUSINESS STATE LOADED | products=%s", len(business_state["products"]))
                return

    except Exception as error:
        logger.exception("BUSINESS STATE LOAD FAILED | %s", error)

    # One-time compatibility with the old inventory memory.
    try:
        async for message in telegram.iter_messages("me", search=LEGACY_INVENTORY_MARKER, limit=20):
            text = (message.raw_text or "").strip()
            if not text.startswith(LEGACY_INVENTORY_MARKER):
                continue

            legacy = text[len(LEGACY_INVENTORY_MARKER):].strip()
            if legacy:
                parsed = await parse_admin_update(legacy)
                if parsed and apply_admin_updates(parsed):
                    await save_business_state()
            return

    except Exception as error:
        logger.exception("LEGACY MIGRATION FAILED | %s", error)


async def get_customer_context(event, limit: int = 6) -> tuple[str, list[str]]:
    """Customer messages only; old bot mistakes are never fed back to Gemini."""
    rows = []

    try:
        async for message in telegram.iter_messages(event.chat_id, limit=30):
            if message.id == event.message.id or message.out:
                continue

            text = (message.raw_text or "").strip()
            if not text or text.startswith("/"):
                continue

            rows.append((message.id, text))
            if len(rows) >= limit:
                break

    except Exception as error:
        logger.warning("CUSTOMER CONTEXT LOAD FAILED | %s", error)

    rows.reverse()
    messages = [text for _, text in rows]
    context = "\n".join(f"CUSTOMER: {text}" for text in messages)
    return context, messages


def last_discussed_product(customer_messages: list[str]) -> str | None:
    for message in reversed(customer_messages):
        exact = exact_catalog_match(message)
        if exact:
            return exact

        possible = find_product_key(message)
        if possible:
            return possible

    return None


async def classify_customer_message(
    customer_text: str,
    customer_context: str,
    last_product: str | None,
) -> dict | None:
    prompt = f"""
You are the semantic intent router for KAVOD BOOKS Telegram customer service.
Understand Amharic, English, mixed language, slang, typos, and Amharic written using Latin letters.
The Telegram account is also used by real humans, so unrelated personal conversation must be ignored.

KNOWN PRODUCTS:
{json.dumps(product_catalog_for_ai(), ensure_ascii=False)}

LAST CLEARLY DISCUSSED PRODUCT:
{last_product or 'None'}

RECENT CUSTOMER MESSAGES:
{customer_context or 'None'}

NEW MESSAGE:
{customer_text}

Return:
{{
  "should_reply": true,
  "intent": "greeting|product_list|product_inquiry|price|availability|photo|address|delivery|order|other",
  "product": "known canonical product name or explicit customer product wording or null",
  "wants_photo": false,
  "confidence": 0.0
}}

Important:
- Unrelated personal conversation => should_reply=false.
- A known product name alone => product_inquiry.
- If the new message means "how much?", including transliterated phrases such as "sint nw?", resolve it as price for LAST CLEARLY DISCUSSED PRODUCT.
- If it asks for photo/design without repeating the product, resolve it as photo for LAST CLEARLY DISCUSSED PRODUCT.
- Short follow-ups must inherit the relevant product from conversation context.
- Do not drop the product field when last_product clearly resolves the reference.
"""

    decision = await gemini_json(prompt, max_tokens=400)
    if not decision:
        return None

    try:
        decision["confidence"] = float(decision.get("confidence", 0))
    except Exception:
        decision["confidence"] = 0.0

    # Generic contextual repair: Gemini understands intent, Python preserves the resolved entity.
    if not decision.get("product") and last_product:
        if decision.get("intent") in {
            "price",
            "availability",
            "photo",
            "order",
            "product_inquiry",
        }:
            decision["product"] = last_product

    return decision


def build_verified_facts(decision: dict) -> dict:
    facts = {
        "address": business_state["address"],
        "delivery": business_state["delivery"],
        "product": None,
        "available_products": None,
    }

    product_key = find_product_key(decision.get("product"))
    if product_key:
        product = business_state["products"][product_key]
        facts["product"] = {
            "name": product_key,
            "price": product.get("price"),
            "available": product.get("available"),
            "category": product.get("category"),
            "design_tag": product.get("design_tag"),
        }

    if decision.get("intent") == "product_list":
        facts["available_products"] = [
            {
                "name": name,
                "price": data.get("price"),
                "category": data.get("category"),
            }
            for name, data in business_state["products"].items()
            if data.get("available") is True
        ]

    return facts


async def saved_design_messages(tag: str, limit: int = 6):
    found = []

    async for message in telegram.iter_messages(
        "me",
        search=f"{DESIGN_MARKER} {tag}",
        limit=20,
    ):
        if message.media:
            found.append(message)
            if len(found) >= limit:
                break

    found.reverse()
    return found


async def send_saved_designs(event, tag: str) -> int:
    sent = 0

    for index, message in enumerate(await saved_design_messages(tag), start=1):
        try:
            data = await telegram.download_media(message.media, file=bytes)
            if not data:
                continue

            mime = getattr(getattr(message, "document", None), "mime_type", "") or ""
            buffer = io.BytesIO(data)
            buffer.name = f"{tag}_{index}{'.png' if 'png' in mime else '.jpg'}"

            await telegram.send_file(
                event.chat_id,
                buffer,
                caption=f"ዲዛይን {index}",
            )
            sent += 1

        except Exception as error:
            logger.exception("DESIGN SEND FAILED | tag=%s | %s", tag, error)

    return sent


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"KAVOD online | version {BOT_VERSION}".encode("utf-8"))
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
    logger.info("Health server running on port %s", port)
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


@telegram.on(events.NewMessage(pattern=r"^/version$"))
async def version_handler(event):
    if await is_admin_event(event):
        await event.reply(f"KAVOD bot version: {BOT_VERSION}")


@telegram.on(events.NewMessage(pattern=r"^/(?:catalog|inventory)$"))
async def catalog_handler(event):
    if not await is_admin_event(event):
        return

    if not business_state["products"]:
        await event.reply("Catalog ገና ባዶ ነው።")
        return

    lines = []
    for name, data in business_state["products"].items():
        if data.get("available") is True:
            stock = "✅ አለ"
        elif data.get("available") is False:
            stock = "❌ አልቋል"
        else:
            stock = "❔ stock unknown"

        price = (
            f"{data['price']} ብር"
            if data.get("price") is not None
            else "price unknown"
        )
        lines.append(f"• {name} — {price} — {stock}")

    await event.reply("KAVOD persistent catalog:\n\n" + "\n".join(lines))


async def handle_admin_memory_update(event, instruction: str):
    instruction = instruction.strip()

    if not instruction:
        await event.reply(
            "Examples:\n"
            "/remember Leather Bible is 1200 and available\n"
            "/remember Leather Bible is finished\n"
            "/remember Leather Bible is back in stock\n"
            "/remember የጸሎት መጽሐፍ 350 ብር ነው"
        )
        return

    parsed = await parse_admin_update(instruction)
    if not parsed:
        await event.reply("Update መረዳት አልቻልኩም፤ ምንም ነገር አልቀየርኩም።")
        return

    before = deepcopy(business_state)
    summaries = apply_admin_updates(parsed)

    if not summaries:
        business_state.clear()
        business_state.update(before)
        await event.reply("ምንም የሚቀየር የproduct መረጃ አላገኘሁም።")
        return

    try:
        await save_business_state()
    except Exception as error:
        business_state.clear()
        business_state.update(before)
        logger.exception("STATE SAVE FAILED | %s", error)
        await event.reply("Update ማስቀመጥ አልተቻለም፤ ለውጡ rollback ተደርጓል።")
        return

    await event.reply(
        "✅ አስታውሻለሁ፦\n"
        + "\n".join(f"• {summary}" for summary in summaries)
    )


@telegram.on(events.NewMessage(pattern=r"^/(?:remember|update)(?:\s+([\s\S]+))?$"))
async def remember_handler(event):
    if await is_admin_event(event):
        await handle_admin_memory_update(
            event,
            event.pattern_match.group(1) or "",
        )


@telegram.on(events.NewMessage(pattern=r"^/set_inventory(?:\s+([\s\S]+))?$"))
async def set_inventory_handler(event):
    if await is_admin_event(event):
        await handle_admin_memory_update(
            event,
            event.pattern_match.group(1) or "",
        )


@telegram.on(events.NewMessage(pattern=r"^/add_design(?:\s+([a-zA-Z0-9_-]+))?$"))
async def add_design_handler(event):
    if not await is_admin_event(event):
        return

    tag = (event.pattern_match.group(1) or "").lower().strip()

    if not tag:
        await event.reply(
            "ፎቶ attach አድርገው caption ላይ /add_design leather_bible ይጻፉ።"
        )
        return

    if not event.message.media:
        await event.reply("ይህ command ከPNG/JPG ፎቶ ጋር መላክ አለበት።")
        return

    try:
        await event.forward_to("me")
        await event.reply(f"✅ {tag} ዲዛይን ተቀምጧል።")
        logger.info("DESIGN SAVED | tag=%s", tag)

    except Exception as error:
        logger.exception("DESIGN SAVE FAILED | %s", error)
        await event.reply("ዲዛይኑን ማስቀመጥ አልተቻለም።")


@telegram.on(events.NewMessage(incoming=True, func=lambda event: event.is_private))
async def customer_message_handler(event):
    try:
        customer_text = (event.raw_text or "").strip()

        if not customer_text or customer_text.startswith("/"):
            return

        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return

        me = await telegram.get_me()
        if event.sender_id == me.id:
            return

        logger.info(
            "MESSAGE RECEIVED | sender_id=%s | text=%r",
            event.sender_id,
            customer_text,
        )

        customer_context, previous_customer_messages = await get_customer_context(event)
        last_product = last_discussed_product(previous_customer_messages)

        direct_product = exact_catalog_match(customer_text)

        if direct_product:
            decision = {
                "should_reply": True,
                "intent": "product_inquiry",
                "product": direct_product,
                "wants_photo": False,
                "confidence": 1.0,
            }
        else:
            decision = await classify_customer_message(
                customer_text,
                customer_context,
                last_product,
            )

        if not decision:
            logger.info("PASSIVE ROUTER | no decision | sender_id=%s", event.sender_id)
            return

        if not decision.get("should_reply"):
            logger.info(
                "PASSIVE ROUTER | sender_id=%s | decision=%r",
                event.sender_id,
                decision,
            )
            return

        confidence = float(decision.get("confidence", 0) or 0)
        if confidence < 0.45:
            logger.info(
                "PASSIVE LOW CONFIDENCE | sender_id=%s | decision=%r",
                event.sender_id,
                decision,
            )
            return

        facts = build_verified_facts(decision)
        product = facts.get("product")

        # A product-specific intent without a verified product is not safe to answer.
        if decision.get("intent") in {
            "price",
            "availability",
            "photo",
            "product_inquiry",
        } and not product:
            logger.info(
                "PASSIVE UNVERIFIED PRODUCT | sender_id=%s | decision=%r",
                event.sender_id,
                decision,
            )
            return

        wants_photo = bool(
            decision.get("wants_photo")
            or decision.get("intent") == "photo"
        )

        design_tag = product.get("design_tag") if product else None
        photos_available = bool(
            design_tag
            and wants_photo
            and await saved_design_messages(design_tag, limit=1)
        )

        async with telegram.action(event.chat_id, "typing"):
            reply_text = await generate_customer_reply(
                customer_text,
                customer_context,
                decision,
                facts,
                photos_available,
            )

            if reply_text:
                await event.reply(reply_text, link_preview=False)

            sent_photos = 0
            if wants_photo and photos_available and design_tag:
                sent_photos = await send_saved_designs(event, design_tag)

        if not reply_text and sent_photos == 0:
            logger.info("PASSIVE NO SAFE RESPONSE | sender_id=%s", event.sender_id)
            return

        logger.info(
            "CUSTOMER HANDLED | sender_id=%s | intent=%s | product=%s | photos=%s",
            event.sender_id,
            decision.get("intent"),
            decision.get("product"),
            sent_photos,
        )

    except FloodWaitError as error:
        logger.warning("FloodWait %s seconds", error.seconds)
        await asyncio.sleep(error.seconds)

    except Exception as error:
        logger.exception("CUSTOMER HANDLER ERROR | %s", error)


async def start_telegram():
    logger.info("Connecting to Telegram...")
    await telegram.connect()

    if not await telegram.is_user_authorized():
        raise RuntimeError("SESSION_STRING is invalid or no longer authorized.")

    me = await telegram.get_me()

    if EXPECTED_ACCOUNT_ID and me.id != EXPECTED_ACCOUNT_ID:
        await telegram.disconnect()
        raise RuntimeError("SESSION_STRING belongs to the wrong Telegram account.")

    await load_business_state()

    logger.info("=" * 60)
    logger.info("Account ID: %s", me.id)
    logger.info("Username: @%s", me.username or "NONE")
    logger.info("Bot version: %s", BOT_VERSION)
    logger.info("Persistent products: %s", len(business_state["products"]))
    logger.info("Gemini model: %s", GEMINI_MODEL)
    logger.info("KAVOD CUSTOMER SERVICE IS ONLINE ✅")
    logger.info("=" * 60)

    try:
        await telegram.catch_up()
    except Exception as error:
        logger.warning("catch_up failed: %s", error)

    await telegram.run_until_disconnected()


async def main():
    await start_telegram()


if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KAVOD assistant stopped manually.")
    except Exception as error:
        logger.exception("Application crashed: %s", error)
        raise
