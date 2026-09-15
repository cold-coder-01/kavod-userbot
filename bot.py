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

BOT_VERSION = "2026-09-15.11"
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
    text = (text or "").lower().strip()
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


def find_product_key(text: str | None) -> str | None:
    if not text:
        return None
    target = normalize(text)
    if not target:
        return None

    best = None
    best_score = 0
    for key, data in business_state["products"].items():
        for candidate in [key, *data.get("aliases", [])]:
            c = normalize(candidate)
            if not c:
                continue
            if target == c:
                return key
            if c in target or target in c:
                score = len(c)
                if score > best_score:
                    best = key
                    best_score = score
    return best


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
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


async def gemini_json(prompt: str, max_tokens: int = 450) -> dict | None:
    async with gemini_semaphore:
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt + "\n\nReturn one valid JSON object only. No markdown or explanation.",
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=max_tokens,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
            if not response or not response.text:
                return None
            logger.info("GEMINI JSON RAW | %r", response.text[:900])
            return extract_json_object(response.text)
        except Exception as error:
            logger.exception("GEMINI JSON ERROR | %s", error)
            return None


CUSTOMER_SYSTEM = f"""
You are KAVOD BOOKS customer service on Telegram in Ethiopia.
Act like a smart real Ethiopian sales representative, not a scripted bot.
Understand Amharic, English, mixed language, slang, spelling mistakes and Amharic written with Latin letters.
Use ONLY verified facts supplied in the prompt.
Never invent stock, price, product details, payment, color, size, address or delivery information.
Never output fragments.
If the message is unrelated human conversation or there is no safe useful KAVOD answer, output exactly {NO_REPLY_TOKEN}.
Use short natural conversational Amharic unless the customer clearly prefers English.
"""


async def gemini_text(prompt: str, temperature: float = 0.15, max_tokens: int = 320) -> str | None:
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
            answer = response.text.strip()
            logger.info("GEMINI TEXT RAW | %r", answer[:800])
            if not answer or NO_REPLY_TOKEN in answer:
                return None
            return answer
        except Exception as error:
            logger.exception("GEMINI TEXT ERROR | %s", error)
            return None


async def save_business_state() -> None:
    payload = json.dumps(business_state, ensure_ascii=False, separators=(",", ":"))
    await telegram.send_message("me", f"{STATE_MARKER}\n{payload}")
    logger.info("BUSINESS STATE SAVED | products=%s", len(business_state["products"]))


async def parse_admin_update(instruction: str) -> dict | None:
    prompt = f"""
Convert the KAVOD BOOKS admin instruction into structured catalog updates.
The admin may use Amharic, English, mixed language or transliterated Amharic.

CURRENT CATALOG:
{json.dumps(product_catalog_for_ai(), ensure_ascii=False)}

ADMIN INSTRUCTION:
{instruction}

Return:
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
- finished/sold out/out of stock/አልቋል/የለም => available=false.
- back/in stock/available/አለ => available=true.
- A price-only instruction changes price without changing stock.
- Never delete unmentioned products.
- Reuse existing canonical product names where possible.
"""
    return await gemini_json(prompt)


def apply_admin_updates(parsed: dict) -> list[str]:
    summaries = []
    for update in parsed.get("updates", []):
        name = str(update.get("product") or "").strip()
        if not name:
            continue
        key = find_product_key(name) or name
        if key not in business_state["products"]:
            business_state["products"][key] = {
                "price": None,
                "available": None,
                "category": update.get("category") or "other",
                "aliases": [],
                "design_tag": tagify(name),
            }
        item = business_state["products"][key]
        if update.get("price") is not None:
            item["price"] = update["price"]
        if update.get("available") is not None:
            item["available"] = bool(update["available"])
        if update.get("category"):
            item["category"] = update["category"]
        aliases = set(item.get("aliases", []))
        aliases.update(x for x in update.get("aliases", []) if x)
        if normalize(name) != normalize(key):
            aliases.add(name)
        item["aliases"] = sorted(aliases)
        item.setdefault("design_tag", tagify(key))

        parts = [key]
        if update.get("price") is not None:
            parts.append(f"price={item['price']}")
        if update.get("available") is not None:
            parts.append("available" if item["available"] else "out of stock")
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
                merged.update({k: v for k, v in loaded.items() if k in merged})
                business_state = merged
                logger.info("BUSINESS STATE LOADED | products=%s", len(business_state["products"]))
                return
    except Exception as error:
        logger.exception("BUSINESS STATE LOAD FAILED | %s", error)

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


async def get_customer_context(event, limit: int = 8) -> tuple[str, list[str]]:
    rows = []
    try:
        async for message in telegram.iter_messages(event.chat_id, limit=40):
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
    return "\n".join(f"CUSTOMER: {text}" for text in messages), messages


def last_discussed_product(messages: list[str]) -> str | None:
    for text in reversed(messages):
        product = find_product_key(text)
        if product:
            return product
    return None


def contextual_followup(text: str, last_product: str | None) -> dict | None:
    """Preserve the subject of short obvious follow-ups before asking Gemini to route again."""
    if not last_product:
        return None

    cleaned = normalize(text)
    words = cleaned.split()
    if not cleaned or len(words) > 7:
        return None

    price_signals = (
        "price", "cost", "how much", "sint", "sent", "wagaw", "waga",
        "ዋጋ", "ስንት", "ብር",
    )
    photo_signals = (
        "photo", "picture", "image", "design", "pic", "foto",
        "ፎቶ", "ዲዛይን", "ምስል",
    )
    availability_signals = (
        "available", "stock", "alachew", "alachu", "ale", "alu",
        "አለ", "አላችሁ", "አሉ", "አለን", "ይገኛ",
    )

    if any(signal in cleaned for signal in price_signals):
        return {
            "should_reply": True,
            "intent": "price",
            "product": last_product,
            "wants_photo": False,
            "confidence": 1.0,
        }

    if any(signal in cleaned for signal in photo_signals):
        return {
            "should_reply": True,
            "intent": "photo",
            "product": last_product,
            "wants_photo": True,
            "confidence": 1.0,
        }

    if any(signal in cleaned for signal in availability_signals):
        return {
            "should_reply": True,
            "intent": "availability",
            "product": last_product,
            "wants_photo": False,
            "confidence": 1.0,
        }

    return None


def normalize_router_decision(result: dict | None, last_product: str | None) -> dict | None:
    if not isinstance(result, dict):
        return None

    allowed = {
        "greeting", "product_list", "product_inquiry", "price", "availability",
        "photo", "address", "delivery", "order", "other",
    }
    intent = str(result.get("intent") or "other").lower().strip()
    if intent not in allowed:
        intent = "other"

    should_reply = result.get("should_reply")
    if isinstance(should_reply, str):
        should_reply = should_reply.lower().strip() in {"true", "yes", "reply", "1"}
    else:
        should_reply = bool(should_reply)

    wants_photo = result.get("wants_photo")
    if isinstance(wants_photo, str):
        wants_photo = wants_photo.lower().strip() in {"true", "yes", "1", "photo"}
    else:
        wants_photo = bool(wants_photo)

    try:
        confidence = float(result.get("confidence", 0) or 0)
    except Exception:
        confidence = 0.0

    product = result.get("product")
    if product in {"null", "none", "NONE", "None", ""}:
        product = None
    if product:
        product = find_product_key(str(product)) or str(product)
    if not product and last_product and intent in {"price", "availability", "photo", "order", "product_inquiry"}:
        product = last_product

    return {
        "should_reply": should_reply,
        "intent": intent,
        "product": product,
        "wants_photo": wants_photo or intent == "photo",
        "confidence": confidence,
    }


async def classify_customer_message(text: str, context: str, last_product: str | None) -> dict | None:
    product_hint = find_product_key(text)

    prompt = f"""
You are the semantic intent router for KAVOD BOOKS customer service.
Understand Amharic, English, mixed language, slang, typos and transliterated Amharic.
The account is also used by real humans, so unrelated personal conversation must be ignored.

KNOWN PRODUCTS:
{json.dumps(product_catalog_for_ai(), ensure_ascii=False)}
DIRECT PRODUCT IN NEW MESSAGE: {product_hint or 'None'}
LAST DISCUSSED PRODUCT: {last_product or 'None'}
RECENT CUSTOMER MESSAGES:
{context or 'None'}
NEW MESSAGE: {text}

Return:
{{
  "should_reply": true,
  "intent": "greeting|product_list|product_inquiry|price|availability|photo|address|delivery|order|other",
  "product": "known canonical product name or null",
  "wants_photo": false,
  "confidence": 0.0
}}

Rules:
- greetings => greeting.
- unrelated personal talk => should_reply=false.
- known product alone => product_inquiry.
- short follow-ups inherit LAST DISCUSSED PRODUCT.
- photo/design questions => photo.
- price/how much/sint/wagaw questions => price.
"""

    first = normalize_router_decision(await gemini_json(prompt), last_product)
    if first and first.get("should_reply") and first.get("confidence", 0) >= 0.5:
        return first

    fallback_prompt = f"""
Classify this KAVOD BOOKS message. Do not answer it.
Known products: {json.dumps([p['name'] for p in product_catalog_for_ai()], ensure_ascii=False)}
Direct product: {product_hint or 'NONE'}
Last product: {last_product or 'NONE'}
Context: {context or 'NONE'}
Message: {text}

Return one line exactly:
REPLY_OR_IGNORE|INTENT|PRODUCT_OR_NONE|PHOTO_TRUE_OR_FALSE|CONFIDENCE
Examples:
REPLY|price|Leather Bible|false|0.98
REPLY|photo|Leather Bible|true|0.98
IGNORE|other|NONE|false|0.95
"""
    raw = await gemini_text(fallback_prompt, temperature=0.0, max_tokens=100)
    if raw:
        parts = [x.strip() for x in raw.splitlines()[0].split("|")]
        if len(parts) == 5:
            result = {
                "should_reply": parts[0].upper() == "REPLY",
                "intent": parts[1].lower(),
                "product": None if parts[2].upper() == "NONE" else parts[2],
                "wants_photo": parts[3].lower() == "true",
                "confidence": parts[4],
            }
            second = normalize_router_decision(result, last_product)
            if second:
                return second

    cleaned = normalize(text)
    if cleaned in {"selam", "salam", "hello", "hi", "hey", "ሰላም"}:
        return {
            "should_reply": True,
            "intent": "greeting",
            "product": None,
            "wants_photo": False,
            "confidence": 1.0,
        }

    if product_hint:
        return {
            "should_reply": True,
            "intent": "product_inquiry",
            "product": product_hint,
            "wants_photo": False,
            "confidence": 0.95,
        }

    return first


def build_verified_facts(decision: dict) -> dict:
    facts = {
        "address": business_state["address"],
        "delivery": business_state["delivery"],
        "product": None,
        "available_products": None,
    }
    key = find_product_key(decision.get("product"))
    if key:
        item = business_state["products"][key]
        facts["product"] = {
            "name": key,
            "price": item.get("price"),
            "available": item.get("available"),
            "category": item.get("category"),
            "design_tag": item.get("design_tag") or tagify(key),
        }
    if decision.get("intent") == "product_list":
        facts["available_products"] = [
            {"name": name, "price": data.get("price")}
            for name, data in business_state["products"].items()
            if data.get("available") is True
        ]
    return facts


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
        items = facts.get("available_products") or []
        if not items:
            return None
        lines = [f"• {x['name']} — {x['price']} ብር" if x.get("price") is not None else f"• {x['name']}" for x in items]
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
    if intent == "photo" and photos_available:
        return f"አዎ፣ ያሉንን {name} ዲዛይኖች እልክልዎታለሁ።"
    if intent == "product_inquiry":
        if available is False:
            return f"አሁን {name} አልቋል።"
        if available is True and price is not None:
            return f"አዎ፣ {name} አለን። ዋጋው {price} ብር ነው።"
        if available is True:
            return f"አዎ፣ {name} አለን።"
    return None


async def generate_customer_reply(text: str, context: str, decision: dict, facts: dict, photos_available: bool) -> str | None:
    prompt = f"""
RECENT CUSTOMER MESSAGES:
{context or 'None'}
CURRENT MESSAGE: {text}
UNDERSTOOD INTENT: {json.dumps(decision, ensure_ascii=False)}
VERIFIED FACTS: {json.dumps(facts, ensure_ascii=False)}
PHOTOS AVAILABLE: {photos_available}

Write exactly what KAVOD should send now.
Use one or two complete natural sentences and only verified facts.
For price intent include the exact verified price.
For product inquiry mention known availability and price.
For photo intent with photos available say you will show/send them.
"""
    answer = await gemini_text(prompt)
    product = facts.get("product")
    intent = decision.get("intent")
    valid = bool(answer and len(answer.split()) >= 3)

    if valid and product and intent == "price" and product.get("price") is not None:
        valid = str(product["price"]) in answer
    if valid and product and intent == "product_inquiry" and product.get("price") is not None:
        valid = str(product["price"]) in answer

    if valid:
        return answer

    logger.warning("GEMINI RESPONSE REJECTED | %r", answer)
    return factual_fallback(decision, facts, photos_available)


async def saved_design_messages(tag: str, limit: int = 6) -> list:
    if not tag:
        return []
    wanted = normalize(f"{DESIGN_MARKER} {tag}")
    found = []
    checked = 0
    async for message in telegram.iter_messages("me", limit=500):
        checked += 1
        if not message.media:
            continue
        caption = normalize(message.raw_text or "")
        if caption == wanted or wanted in caption:
            found.append(message)
            if len(found) >= limit:
                break
    found.reverse()
    logger.info("DESIGN LOOKUP | tag=%s | checked=%s | found=%s", tag, checked, len(found))
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
            await telegram.send_file(event.chat_id, buffer, caption=f"ዲዛይን {index}")
            sent += 1
        except Exception as error:
            logger.exception("DESIGN SEND FAILED | %s", error)
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
        stock = "✅ አለ" if data.get("available") is True else "❌ አልቋል" if data.get("available") is False else "❔ unknown"
        price = f"{data['price']} ብር" if data.get("price") is not None else "price unknown"
        lines.append(f"• {name} — {price} — {stock}")
    await event.reply("KAVOD persistent catalog:\n\n" + "\n".join(lines))


@telegram.on(events.NewMessage(pattern=r"^/designs(?:\s+([a-zA-Z0-9_-]+))?$"))
async def designs_handler(event):
    if not await is_admin_event(event):
        return
    tag = (event.pattern_match.group(1) or "").lower().strip()
    if not tag:
        await event.reply("Usage: /designs leather_bible")
        return
    messages = await saved_design_messages(tag, limit=20)
    if not messages:
        await event.reply(f"❌ `{tag}`: 0 saved designs found.")
        return
    ids = ", ".join(str(x.id) for x in messages)
    await event.reply(f"✅ `{tag}`: {len(messages)} saved design(s) found.\nSaved Message IDs: {ids}")


async def handle_admin_memory_update(event, instruction: str):
    instruction = instruction.strip()
    if not instruction:
        await event.reply(
            "Examples:\n"
            "/remember Leather Bible is 1200 and available\n"
            "/remember Leather Bible is finished\n"
            "/remember Leather Bible is back in stock"
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
        await event.reply("ምንም የሚቀየር መረጃ አላገኘሁም።")
        return
    try:
        await save_business_state()
    except Exception as error:
        business_state.clear()
        business_state.update(before)
        logger.exception("STATE SAVE FAILED | %s", error)
        await event.reply("Update ማስቀመጥ አልተቻለም።")
        return
    await event.reply("✅ አስታውሻለሁ፦\n" + "\n".join(f"• {x}" for x in summaries))


@telegram.on(events.NewMessage(pattern=r"^/(?:remember|update)(?:\s+([\s\S]+))?$"))
async def remember_handler(event):
    if await is_admin_event(event):
        await handle_admin_memory_update(event, event.pattern_match.group(1) or "")


@telegram.on(events.NewMessage(pattern=r"^/set_inventory(?:\s+([\s\S]+))?$"))
async def set_inventory_handler(event):
    if await is_admin_event(event):
        await handle_admin_memory_update(event, event.pattern_match.group(1) or "")


@telegram.on(events.NewMessage(pattern=r"^/add_design(?:\s+([a-zA-Z0-9_-]+))?$"))
async def add_design_handler(event):
    if not await is_admin_event(event):
        return
    tag = (event.pattern_match.group(1) or "").lower().strip()
    if not tag:
        await event.reply("Attach a photo and use caption: /add_design leather_bible")
        return
    if not event.message.media:
        await event.reply("This command must be sent as a PNG/JPG photo caption.")
        return
    try:
        await event.forward_to("me")
        await event.reply(f"✅ `{tag}` design saved.")
    except Exception as error:
        logger.exception("DESIGN SAVE FAILED | %s", error)
        await event.reply("Design could not be saved.")


@telegram.on(events.NewMessage(incoming=True, func=lambda event: event.is_private))
async def customer_message_handler(event):
    try:
        text = (event.raw_text or "").strip()
        if not text or text.startswith("/"):
            return

        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return

        me = await telegram.get_me()
        if event.sender_id == me.id:
            return

        logger.info("MESSAGE RECEIVED | sender_id=%s | text=%r", event.sender_id, text)

        context, previous_messages = await get_customer_context(event)
        last_product = last_discussed_product(previous_messages)
        direct_product = exact_catalog_match(text)
        mentioned_product = find_product_key(text)

        if direct_product:
            decision = {
                "should_reply": True,
                "intent": "product_inquiry",
                "product": direct_product,
                "wants_photo": False,
                "confidence": 1.0,
            }
        elif mentioned_product and any(x in normalize(text) for x in ("photo", "picture", "image", "design", "foto", "ፎቶ", "ዲዛይን")):
            decision = {
                "should_reply": True,
                "intent": "photo",
                "product": mentioned_product,
                "wants_photo": True,
                "confidence": 1.0,
            }
        else:
            decision = contextual_followup(text, last_product)
            if not decision:
                decision = await classify_customer_message(text, context, last_product)

        if not decision or not decision.get("should_reply"):
            logger.info("PASSIVE ROUTER | %r", decision)
            return

        if float(decision.get("confidence", 0) or 0) < 0.45:
            logger.info("PASSIVE LOW CONFIDENCE | %r", decision)
            return

        facts = build_verified_facts(decision)
        product = facts.get("product")

        if decision.get("intent") in {"price", "availability", "photo", "product_inquiry"} and not product:
            logger.info("PASSIVE UNVERIFIED PRODUCT | %r", decision)
            return

        wants_photo = bool(decision.get("wants_photo") or decision.get("intent") == "photo")
        design_tag = product.get("design_tag") if product else None
        photos_available = bool(
            wants_photo
            and design_tag
            and await saved_design_messages(design_tag, limit=1)
        )

        async with telegram.action(event.chat_id, "typing"):
            reply = await generate_customer_reply(text, context, decision, facts, photos_available)
            if reply:
                await event.reply(reply, link_preview=False)

            sent = 0
            if wants_photo and photos_available and design_tag:
                sent = await send_saved_designs(event, design_tag)

        if not reply and sent == 0:
            logger.info("PASSIVE NO SAFE RESPONSE | sender_id=%s", event.sender_id)
            return

        logger.info(
            "CUSTOMER HANDLED | sender_id=%s | intent=%s | product=%s | photos=%s",
            event.sender_id,
            decision.get("intent"),
            decision.get("product"),
            sent,
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
