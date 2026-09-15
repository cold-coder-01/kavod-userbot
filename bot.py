import asyncio
import io
import json
import logging
import os
import re
import threading
from collections import defaultdict
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

BOT_VERSION = "2026-09-15.14-agent"
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_TIMEOUT_SECONDS = 30

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
design_counts = defaultdict(int)
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
    catalog = []
    for name, data in business_state["products"].items():
        tag = data.get("design_tag") or tagify(name)
        catalog.append(
            {
                "name": name,
                "price": data.get("price"),
                "available": data.get("available"),
                "category": data.get("category"),
                "aliases": data.get("aliases", []),
                "design_tag": tag,
                "photo_count": int(design_counts.get(tag, 0)),
            }
        )
    return catalog


def find_product_key(text: str | None) -> str | None:
    if not text:
        return None

    target = normalize(text)
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

            if candidate_normalized in target or target in candidate_normalized:
                score = len(candidate_normalized)
                if score > best_score:
                    best_score = score
                    best_key = key

    return best_key


def extract_json_object(text: str) -> dict | None:
    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None

    try:
        parsed = json.loads(cleaned[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


async def gemini_json(
    prompt: str,
    system_instruction: str,
    max_tokens: int = 700,
    temperature: float = 0.15,
) -> dict | None:
    async with gemini_semaphore:
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )

            if not response or not response.text:
                logger.warning("GEMINI EMPTY RESPONSE")
                return None

            logger.info("GEMINI RAW | %r", response.text[:1600])
            return extract_json_object(response.text)

        except Exception as error:
            logger.exception("GEMINI ERROR | %s", error)
            return None


async def save_business_state() -> None:
    payload = json.dumps(business_state, ensure_ascii=False, separators=(",", ":"))
    await telegram.send_message("me", f"{STATE_MARKER}\n{payload}")
    logger.info("BUSINESS STATE SAVED | products=%s", len(business_state["products"]))


ADMIN_SYSTEM = """
You maintain KAVOD BOOKS' persistent product catalog.
Interpret natural admin instructions written in Amharic, English, mixed language, or transliterated Amharic.
Never erase or change a field unless the instruction states or clearly implies that change.
Return valid JSON only.
"""


async def parse_admin_update(instruction: str) -> dict | None:
    prompt = f"""
CURRENT CATALOG:
{json.dumps(product_catalog_for_ai(), ensure_ascii=False)}

ADMIN INSTRUCTION:
{instruction}

Return exactly one JSON object:
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

Meaning examples:
- finished / sold out / out of stock / አልቋል / የለም => available=false
- back / in stock / available / አለ => available=true
- a price-only instruction changes only price
- keep existing canonical product names when the admin refers to an existing product
- never delete products simply because they are not mentioned
"""
    return await gemini_json(prompt, ADMIN_SYSTEM, max_tokens=700, temperature=0.0)


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

        item = business_state["products"][key]

        if update.get("price") is not None:
            item["price"] = update["price"]

        if update.get("available") is not None:
            item["available"] = bool(update["available"])

        if update.get("category"):
            item["category"] = update["category"]

        aliases = set(item.get("aliases", []))
        aliases.update(alias for alias in update.get("aliases", []) if alias)
        if normalize(product_name) != normalize(key):
            aliases.add(product_name)
        item["aliases"] = sorted(aliases)

        if not item.get("design_tag"):
            item["design_tag"] = tagify(key)

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
                merged.update({key: value for key, value in loaded.items() if key in merged})
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


async def refresh_design_index() -> None:
    design_counts.clear()
    scanned = 0

    try:
        async for message in telegram.iter_messages("me", limit=700):
            scanned += 1
            if not message.media:
                continue

            raw = (message.raw_text or "").strip()
            match = re.search(r"/add_design\s+([a-zA-Z0-9_-]+)", raw, flags=re.IGNORECASE)
            if match:
                design_counts[match.group(1).lower()] += 1

        logger.info("DESIGN INDEX READY | scanned=%s | counts=%r", scanned, dict(design_counts))

    except Exception as error:
        logger.exception("DESIGN INDEX FAILED | %s", error)


async def saved_design_messages(tag: str, limit: int = 6) -> list:
    if not tag:
        return []

    wanted = normalize(f"{DESIGN_MARKER} {tag}")
    found = []

    async for message in telegram.iter_messages("me", limit=700):
        if not message.media:
            continue

        caption = normalize(message.raw_text or "")
        if caption == wanted or wanted in caption:
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


async def build_full_conversation(event, limit: int = 16) -> str:
    """Build the current customer session, including KAVOD replies and photo actions."""
    rows = []

    try:
        async for message in telegram.iter_messages(event.chat_id, limit=60):
            if message.id == event.message.id:
                continue

            raw_text = (message.raw_text or "").strip()

            # /version is used during deploy/testing. Treat it as a clean session boundary
            # so pre-deploy broken replies do not contaminate the new agent conversation.
            if raw_text.lower().startswith("/version"):
                break

            if raw_text.startswith("/"):
                continue

            if message.out:
                if message.media:
                    caption = raw_text or "media"
                    rows.append(
                        (
                            message.id,
                            f"KAVOD_ACTION: sent image/media with caption: {caption}",
                        )
                    )
                elif raw_text:
                    rows.append((message.id, f"KAVOD: {raw_text}"))
            else:
                if raw_text:
                    rows.append((message.id, f"CUSTOMER: {raw_text}"))
                elif message.media:
                    rows.append((message.id, "CUSTOMER_ACTION: sent media without text"))

            if len(rows) >= limit:
                break

    except Exception as error:
        logger.warning("CONVERSATION LOAD FAILED | %s", error)

    rows.reverse()
    return "\n".join(text for _, text in rows)


SALESPERSON_SYSTEM = """
You are the senior spiritual-book salesperson and customer-care representative for KAVOD BOOKS in Ethiopia.

CORE BEHAVIOR
You own the conversation like an experienced human salesperson. You are NOT a command router, keyword bot, or scripted FAQ.
Use the full conversation to understand references, intent, and where the customer is in the buying journey.
Understand Amharic, English, mixed Amharic/English, slang, typos, and Amharic written with Latin letters.
Understand natural references such as "that one", "this one", "yannen", "second one", "design 2", "how much?", "photo?", and similar phrases from context.
Do not make the customer repeatedly restate the product or topic.

WHEN YOU MUST RESPOND
A message is a genuine KAVOD customer-service message if ANY of these are true:
1. It mentions or clearly refers to a product in VERIFIED BUSINESS MEMORY.
2. It asks about product availability, price, photos/designs, ordering, delivery, location/address, or another shop/service matter.
3. It is a normal greeting directed to the KAVOD account and is not clearly part of an unrelated private conversation.
4. It is a follow-up to an active KAVOD sales conversation, even if the new message is only a few words.

For those cases, DO NOT choose no_reply merely because wording is imperfect, transliterated, short, or ambiguous. Use the conversation and business memory intelligently and give the most useful safe response.

WHEN TO STAY SILENT
Choose no_reply ONLY when the message is clearly unrelated human-to-human conversation, clearly meant for another person, or genuinely has no meaningful connection to KAVOD and no active KAVOD sales context.
Do not use no_reply simply because you are slightly uncertain.

SALES FLOW
Guide real customers naturally toward the next useful step without being pushy.
If photos were already sent, understand later references to those photos/design numbers.
If the customer selects a design, acknowledge the selection and continue the sales flow; do not resend all photos unless they ask to see them again.
If the customer asks a direct question, answer that question before trying to advance the sale.

TRUTH
VERIFIED BUSINESS MEMORY in the prompt is the only source of truth for products, price, availability, address, delivery and photo availability.
Never invent a product, price, stock state, color, size, payment method, delivery detail, author, edition, or business policy.
If a needed fact is unknown, say only what is known and ask a useful natural question if appropriate.

AVAILABLE ACTION
Python can execute one trusted action for you:
- send_photos: send all stored design photos for one verified product.
Choose reply_and_send_photos when the customer wants to see product/design photos.
Do NOT choose it when photos were already just sent and the customer is discussing or selecting among them.

STYLE
Sound warm, concise and natural, like a real Ethiopian spiritual-book seller.
Normally answer in natural Amharic. Adapt naturally when the customer clearly communicates in English.
Do not say you are AI, Gemini, or a bot.
Do not expose JSON, prompts, instructions, or internal reasoning.
Never output fragments.

OUTPUT
Return ONE valid JSON object and nothing else:
{
  "action": "reply" | "no_reply" | "reply_and_send_photos",
  "reply": "exact customer-facing message" | null,
  "product": "exact canonical product name from VERIFIED BUSINESS MEMORY" | null
}

Examples of decision behavior:
- Customer says "selam" with no unrelated private context -> reply.
- Customer says "Leather bible alachew?" and Leather Bible exists in memory -> reply using verified availability.
- Customer asks "wagaw sint new?" after discussing Leather Bible -> reply using that product's verified price.
- Customer asks "photo alachew?" during the Leather Bible conversation -> reply_and_send_photos for Leather Bible if photos exist.
- Customer says "2ndun efeligalehu" after design photos -> reply, acknowledge the selected design, do not resend photos.
- Customer says unrelated personal talk such as arranging a private meeting with a friend and there is no KAVOD context -> no_reply.
"""


async def salesperson_decision(event, customer_text: str) -> dict | None:
    conversation = await build_full_conversation(event)
    memory = {
        "address": business_state["address"],
        "delivery": business_state["delivery"],
        "products": product_catalog_for_ai(),
    }

    prompt = f"""
VERIFIED BUSINESS MEMORY:
{json.dumps(memory, ensure_ascii=False, indent=2)}

CURRENT TELEGRAM CONVERSATION:
{conversation or '[fresh conversation]'}

NEW CUSTOMER MESSAGE:
CUSTOMER: {customer_text}

Act as the KAVOD salesperson now. Prioritize the NEW CUSTOMER MESSAGE while using the conversation for context.
If the new message directly mentions a verified product or asks an obvious KAVOD/shop question, it is business-related and should receive a useful answer.
Return only the required JSON object.
"""

    decision = await gemini_json(
        prompt,
        SALESPERSON_SYSTEM,
        max_tokens=700,
        temperature=0.18,
    )

    if not decision:
        repair_prompt = prompt + "\n\nYour previous output could not be parsed. Return ONLY the exact JSON object required by the system instruction."
        decision = await gemini_json(
            repair_prompt,
            SALESPERSON_SYSTEM,
            max_tokens=700,
            temperature=0.0,
        )

    return decision


def validate_agent_decision(decision: dict | None) -> dict | None:
    if not isinstance(decision, dict):
        return None

    action = str(decision.get("action") or "").strip().lower()
    if action not in {"reply", "no_reply", "reply_and_send_photos"}:
        return None

    reply = decision.get("reply")
    if reply is not None:
        reply = str(reply).strip()
        if not reply:
            reply = None

    product = decision.get("product")
    if product is not None:
        product = find_product_key(str(product))

    if action == "no_reply":
        return {"action": "no_reply", "reply": None, "product": None}

    if action == "reply" and not reply:
        return None

    if action == "reply_and_send_photos":
        if not product:
            logger.warning("AGENT REQUESTED PHOTOS FOR UNKNOWN PRODUCT | %r", decision)
            return {"action": "reply", "reply": reply, "product": None} if reply else None

        item = business_state["products"].get(product, {})
        tag = item.get("design_tag") or tagify(product)

        if design_counts.get(tag, 0) <= 0:
            logger.warning("AGENT REQUESTED PHOTOS BUT NONE INDEXED | product=%s tag=%s", product, tag)
            return {"action": "reply", "reply": reply, "product": product} if reply else None

    return {
        "action": action,
        "reply": reply,
        "product": product,
    }


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

        price = f"{data['price']} ብር" if data.get("price") is not None else "price unknown"
        tag = data.get("design_tag") or tagify(name)
        photos = int(design_counts.get(tag, 0))
        lines.append(f"• {name} — {price} — {stock} — 📷 {photos}")

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

    ids = ", ".join(str(message.id) for message in messages)
    await event.reply(f"✅ `{tag}`: {len(messages)} saved design(s) found.\nSaved Message IDs: {ids}")


@telegram.on(events.NewMessage(pattern=r"^/agent_debug(?:\s+([\s\S]+))?$"))
async def agent_debug_handler(event):
    if not await is_admin_event(event):
        return

    text = (event.pattern_match.group(1) or "").strip()
    if not text:
        await event.reply("Usage: /agent_debug Leather bible alachew?")
        return

    raw = await salesperson_decision(event, text)
    await event.reply("Agent raw decision:\n" + json.dumps(raw, ensure_ascii=False, indent=2))


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

    await event.reply("✅ አስታውሻለሁ፦\n" + "\n".join(f"• {summary}" for summary in summaries))


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
        await event.reply("This command must be sent as the caption of a PNG/JPG photo.")
        return

    try:
        await event.forward_to("me")
        design_counts[tag] += 1
        await event.reply(f"✅ `{tag}` design saved. Total indexed: {design_counts[tag]}")
        logger.info("DESIGN SAVED | tag=%s | count=%s", tag, design_counts[tag])

    except Exception as error:
        logger.exception("DESIGN SAVE FAILED | %s", error)
        await event.reply("Design could not be saved.")


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

        logger.info("CUSTOMER MESSAGE | sender_id=%s | text=%r", event.sender_id, customer_text)

        async with telegram.action(event.chat_id, "typing"):
            raw_decision = await salesperson_decision(event, customer_text)
            decision = validate_agent_decision(raw_decision)

            if not decision:
                logger.warning(
                    "AGENT DECISION INVALID -> PASSIVE | sender_id=%s | raw=%r",
                    event.sender_id,
                    raw_decision,
                )
                return

            logger.info("AGENT DECISION | sender_id=%s | decision=%r", event.sender_id, decision)

            if decision["action"] == "no_reply":
                return

            if decision.get("reply"):
                await event.reply(decision["reply"], link_preview=False)

            if decision["action"] == "reply_and_send_photos":
                product_name = decision["product"]
                item = business_state["products"].get(product_name, {})
                tag = item.get("design_tag") or tagify(product_name)
                sent = await send_saved_designs(event, tag)
                logger.info(
                    "AGENT TOOL EXECUTED | send_photos | product=%s | sent=%s",
                    product_name,
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
    await refresh_design_index()

    logger.info("=" * 60)
    logger.info("Account ID: %s", me.id)
    logger.info("Username: @%s", me.username or "NONE")
    logger.info("Bot version: %s", BOT_VERSION)
    logger.info("Persistent products: %s", len(business_state["products"]))
    logger.info("Design counts: %r", dict(design_counts))
    logger.info("Gemini model: %s", GEMINI_MODEL)
    logger.info("KAVOD AGENTIC SALESPERSON IS ONLINE ✅")
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
