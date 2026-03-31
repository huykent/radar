"""
Pancake POS background sync worker.
Periodically fetches order + customer data from Pancake API, aggregates
customer profiles with tags, and bulk-upserts into the local SQLite database.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx

from db import bulk_upsert_profiles, get_settings, save_settings
from tier import resolve_tier
from utils import normalize_phone

logger = logging.getLogger("sync_worker")

# ── Defaults (used when settings DB is empty) ────────────────────────
DEFAULT_SHOP_ID = "1942356641"
DEFAULT_API_KEY = "0bc77e30c57b43a3bdbfc14a6fd30e9f"
DEFAULT_CHAT_PAGE_ID = "101763712250113"
DEFAULT_CHAT_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjEwMTc2MzcxMjI1MDExMyIsInRpbWVzdGFtcCI6MTc3NDQwOTMyN30._KE2RR_V3NWKC9s7YV9ac0qNZGSKnbRAKGBqI6pmAv4"
DEFAULT_SYNC_INTERVAL = 30 * 60  # 30 minutes

PAGE_SIZE = 100
RATE_LIMIT_SLEEP = 1.5  # seconds between paginated requests

SUCCESS_STATUSES = {"success", "delivered", "done", "collected_money"}
FAILED_STATUSES = {"returned", "canceled", "failed", "customer_cancel"}

BASE_URL = "https://pos.pages.fm/api/v1/shops"
CHAT_BASE_URL = "https://pages.fm/api/public_api/v1/pages"

# ── Sync trigger event ───────────────────────────────────────────────
_sync_event = asyncio.Event()


async def trigger_sync() -> int:
    """Trigger an immediate sync and wait for it to complete. Returns count."""
    result = await _sync_once()
    return result


# ═══════════════════════════════════════════════════════════════════
# ON-DEMAND LOOKUP (PER-COMMENTER)
# ═══════════════════════════════════════════════════════════════════
async def lookup_phone_on_demand(phone: str) -> dict | None:
    """
    Query POS API for a single phone's orders, aggregate stats,
    resolve tier, upsert into DB, and return the profile dict.
    Returns None if no orders found for this phone.
    """
    if not phone or len(phone) < 10:
        return None

    shop_id, api_key, _, chat_page_id, chat_token = await _get_config()

    async with httpx.AsyncClient() as client:
        # 1) Search orders by phone (single API call, fast)
        try:
            resp = await client.get(
                f"{BASE_URL}/{shop_id}/orders",
                params={
                    "api_key": api_key,
                    "search": phone,
                    "page_number": 1,
                    "page_size": PAGE_SIZE,
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            orders = data.get("data", data.get("orders", []))
        except Exception as exc:
            logger.error("On-demand POS lookup failed for %s: %s", phone, exc)
            return None

        # 2) Fetch chat tags map (cached per cycle, but okay for single lookups)
        chat_tags_map = await _fetch_all_chat_tags(client, chat_page_id, chat_token)

    if not orders:
        logger.debug("On-demand: no orders for phone %s", phone)
        return None

    # 3) Aggregate this phone's orders
    agg = {"total": 0, "success": 0, "failed": 0, "spent": 0.0}
    customer_name = None
    for order in orders:
        raw = order.get("bill_phone_number") or ""
        if normalize_phone(str(raw)) != phone:
            continue  # search may return fuzzy matches
        status = str(order.get("status", "")).lower().strip()
        price = float(order.get("total_price", 0) or 0)
        agg["total"] += 1
        if status in SUCCESS_STATUSES or str(order.get("status")) in {"5", "6"}:
            agg["success"] += 1
            agg["spent"] += price
        elif status in FAILED_STATUSES or str(order.get("status")) in {"3", "4"}:
            agg["failed"] += 1
        if not customer_name:
            customer_name = order.get("bill_full_name") or order.get("customer_name")

    if agg["total"] == 0:
        return None

    # 4) Resolve tier
    tier_tag, priority_score = resolve_tier(
        pancake_tags=[],
        total=agg["total"],
        success=agg["success"],
        failed=agg["failed"],
        spent=agg["spent"],
    )

    # 5) Upsert single profile
    profile_data = {
        "phone": phone,
        "total_orders": agg["total"],
        "success_orders": agg["success"],
        "failed_orders": agg["failed"],
        "total_spent": agg["spent"],
        "tier_tag": tier_tag,
        "priority_score": priority_score,
        "customer_name": customer_name,
    }
    await bulk_upsert_profiles([profile_data])
    logger.info(
        "On-demand lookup: %s → %d orders, %s",
        phone, agg["total"], tier_tag,
    )

    return {
        **profile_data,
        "pancake_tags": [],
        "fb_uid": None,
        "pancake_customer_id": None,
        "last_updated": datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════
# ON-DEMAND LOOKUP BY FB UID / NAME (VIA PANCAKE CHAT)
# ═══════════════════════════════════════════════════════════════════
async def lookup_by_fb_uid(fb_uid: str | None, fb_name: str | None) -> dict | None:
    """
    Search Pancake Chat conversations by name to find customer tags + phone.
    If phone found, cross-reference with POS orders for spending data.
    Returns enriched profile dict or None.
    """
    if not fb_name or len(fb_name.strip()) < 2:
        return None

    shop_id, api_key, _, chat_page_id, chat_token = await _get_config()
    if not chat_page_id or not chat_token:
        return None

    async with httpx.AsyncClient() as client:
        # 1) Fetch tag map
        chat_tags_map = await _fetch_all_chat_tags(client, chat_page_id, chat_token)

        # 2) Search conversations by customer name
        try:
            resp = await client.get(
                f"{CHAT_BASE_URL}/{chat_page_id}/conversations",
                params={
                    "page_access_token": chat_token,
                    "type": "INBOX",
                    "search": fb_name.strip(),
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Chat conversation search failed for '%s': %s", fb_name, exc)
            return None

        conversations = data.get("conversations", data.get("data", []))
        if not conversations:
            logger.debug("No chat conversation found for '%s'", fb_name)
            return None

        # 3) Extract tags + phone from first matching conversation
        conv = conversations[0]
        raw_tag_ids = conv.get("tags", [])
        pancake_tags = []
        for tid in raw_tag_ids:
            if isinstance(tid, int) and tid in chat_tags_map:
                pancake_tags.append(chat_tags_map[tid])
            elif isinstance(tid, str):
                pancake_tags.append(tid)

        # Extract phone from participants
        phone = None
        customer_name = None
        participants = conv.get("participants", [])
        for p in participants:
            ph = p.get("phone_number") or p.get("phone") or ""
            if ph:
                phone = normalize_phone(str(ph))
                if len(phone) < 10:
                    phone = None
            nm = p.get("name") or p.get("full_name") or ""
            if nm and not customer_name:
                customer_name = nm.strip()

        # 4) If phone found, cross-reference with POS orders
        agg = {"total": 0, "success": 0, "failed": 0, "spent": 0.0}
        if phone:
            try:
                resp2 = await client.get(
                    f"{BASE_URL}/{shop_id}/orders",
                    params={
                        "api_key": api_key,
                        "search": phone,
                        "page_number": 1,
                        "page_size": PAGE_SIZE,
                    },
                    timeout=15.0,
                )
                resp2.raise_for_status()
                orders = resp2.json().get("data", resp2.json().get("orders", []))
                for order in orders:
                    raw_ph = order.get("bill_phone_number") or ""
                    if normalize_phone(str(raw_ph)) != phone:
                        continue
                    status = str(order.get("status", ""))
                    price = float(order.get("total_price", 0) or 0)
                    agg["total"] += 1
                    if status in SUCCESS_STATUSES or status in {"5", "6"}:
                        agg["success"] += 1
                        agg["spent"] += price
                    elif status in FAILED_STATUSES or status in {"3", "4"}:
                        agg["failed"] += 1
            except Exception as exc:
                logger.error("POS cross-reference failed for %s: %s", phone, exc)

    # 5) Resolve tier using chat tags + order stats
    tier_tag, priority_score = resolve_tier(
        pancake_tags=pancake_tags,
        total=agg["total"],
        success=agg["success"],
        failed=agg["failed"],
        spent=agg["spent"],
    )

    # 6) Upsert profile
    profile_data = {
        "phone": phone or f"uid:{fb_uid or fb_name}",
        "total_orders": agg["total"],
        "success_orders": agg["success"],
        "failed_orders": agg["failed"],
        "total_spent": agg["spent"],
        "tier_tag": tier_tag,
        "priority_score": priority_score,
        "customer_name": customer_name or fb_name,
        "fb_uid": fb_uid,
        "pancake_tags": pancake_tags,
    }
    await bulk_upsert_profiles([profile_data])
    logger.info(
        "Chat lookup: '%s' → tags=%s, phone=%s, tier=%s",
        fb_name, pancake_tags, phone, tier_tag,
    )

    return {
        **profile_data,
        "pancake_customer_id": None,
        "last_updated": datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════
# FETCH ORDERS
# ═══════════════════════════════════════════════════════════════════
async def _fetch_all_orders(
    client: httpx.AsyncClient, shop_id: str, api_key: str
) -> list[dict]:
    """Paginate through Pancake POS orders and return raw order list."""
    all_orders: list[dict] = []
    page = 1

    while True:
        try:
            resp = await client.get(
                f"{BASE_URL}/{shop_id}/orders",
                params={
                    "api_key": api_key,
                    "page_number": page,
                    "page_size": PAGE_SIZE,
                },
                timeout=30.0,
            )

            if resp.status_code == 429:
                logger.warning("Rate limited (429). Sleeping 30s before retry…")
                await asyncio.sleep(30)
                continue

            resp.raise_for_status()
            data = resp.json()

            orders = data.get("data", data.get("orders", []))
            if not orders:
                break

            all_orders.extend(orders)
            logger.info("Fetched page %d — %d orders", page, len(orders))

            page += 1
            await asyncio.sleep(RATE_LIMIT_SLEEP)

        except httpx.HTTPStatusError as exc:
            logger.error("HTTP error on orders page %d: %s", page, exc)
            break
        except Exception as exc:
            logger.error("Unexpected error on orders page %d: %s", page, exc)
            break

    return all_orders


# ═══════════════════════════════════════════════════════════════════
# FETCH CUSTOMERS AND TAGS (FROM PANCAKE CHAT API)
# ═══════════════════════════════════════════════════════════════════
CHAT_BASE_URL = "https://pages.fm/api/public_api/v1/pages"


async def _fetch_all_chat_tags(
    client: httpx.AsyncClient, page_id: str, token: str
) -> dict[int, str]:
    """Fetch all tags from Pancake Chat and return {tag_id: tag_text} mapping."""
    if not page_id or not token:
        return {}
    try:
        resp = await client.get(
            f"{CHAT_BASE_URL}/{page_id}/tags",
            params={"page_access_token": token},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        tags = data.get("tags", [])
        logger.info("Fetched %d tags from Pancake Chat.", len(tags))
        return {t.get("id"): t.get("text") for t in tags if "id" in t and "text" in t}
    except Exception as exc:
        logger.error("Failed to fetch chat tags: %s", exc)
        return {}


async def _fetch_all_customers(
    client: httpx.AsyncClient, page_id: str, token: str
) -> list[dict]:
    """Paginate through Pancake Chat API to return customers in the last 6 months."""
    if not page_id or not token:
        logger.warning("No pancake_chat_page_id or pancake_chat_token found, skipping chat customers sync.")
        return []

    all_customers: list[dict] = []
    page = 1
    
    # Sync window: Last 6 months
    now = datetime.now()
    since_date = (now - timedelta(days=180)).strftime("%Y-%m-%d")
    until_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    while True:
        try:
            resp = await client.get(
                f"{CHAT_BASE_URL}/{page_id}/page_customers",
                params={
                    "page_access_token": token,
                    "since": since_date,
                    "until": until_date,
                    "page_number": page,
                    "page_size": PAGE_SIZE,
                },
                timeout=45.0,
            )

            if resp.status_code == 429:
                logger.warning("Rate limited (429). Sleeping 30s before retry…")
                await asyncio.sleep(30)
                continue

            resp.raise_for_status()
            data = resp.json()

            customers = data.get("data", data.get("customers", []))
            if not customers:
                break

            all_customers.extend(customers)
            logger.info("Fetched chat customers page %d — %d customers", page, len(customers))

            page += 1
            await asyncio.sleep(RATE_LIMIT_SLEEP)

        except httpx.HTTPStatusError as exc:
            logger.error("HTTP error on chat customers page %d: %s", page, exc)
            break
        except Exception as exc:
            logger.error("Unexpected error on chat customers page %d: %s", page, exc)
            break

    return all_customers

# ═══════════════════════════════════════════════════════════════════
# AGGREGATE & MERGE
# ═══════════════════════════════════════════════════════════════════
def _aggregate_orders(orders: list[dict]) -> dict[str, dict]:
    """
    Group orders by normalized phone number and compute aggregates.
    Returns {phone: {total, success, failed, spent}}.
    """
    profiles: dict[str, dict] = {}

    for order in orders:
        raw_phone = (
            order.get("bill_phone_number")
            or order.get("customer_phone")
            or ""
        )
        if not raw_phone:
            continue

        phone = normalize_phone(str(raw_phone))
        if len(phone) < 10:
            continue

        if phone not in profiles:
            profiles[phone] = {
                "total": 0,
                "success": 0,
                "failed": 0,
                "spent": 0.0,
            }

        p = profiles[phone]
        status = str(order.get("status", "")).lower().strip()
        price = float(order.get("total_price", 0) or 0)

        p["total"] += 1

        if status in SUCCESS_STATUSES:
            p["success"] += 1
            p["spent"] += price
        elif status in FAILED_STATUSES:
            p["failed"] += 1

    return profiles


def _build_customer_index(customers: list[dict], chat_tags_map: dict[int, str]) -> dict[str, dict]:
    """
    Build a phone → customer_info index from Pancake customer data.
    Each entry contains: name, fb_uid, pancake_customer_id, tags.
    """
    index: dict[str, dict] = {}

    for cust in customers:
        # Try multiple phone field names
        raw_phone = (
            cust.get("phone_number")
            or cust.get("phone")
            or cust.get("tel")
            or ""
        )
        if not raw_phone:
            continue

        phone = normalize_phone(str(raw_phone))
        if len(phone) < 10:
            continue

        # Extract FB UID — try multiple field names
        fb_uid = (
            cust.get("facebook_id")
            or cust.get("fb_uid")
            or cust.get("fb_id")
            or cust.get("social_id")
            or cust.get("psid")
        )
        if fb_uid:
            fb_uid = str(fb_uid)

        # Extract customer name
        name = cust.get("name") or cust.get("full_name") or cust.get("customer_name") or ""

        # Extract tags — could be a list of strings or list of objects, or list of tag IDs
        raw_tags = cust.get("tags") or cust.get("customer_tags") or []
        tags = []
        if isinstance(raw_tags, list):
            for t in raw_tags:
                if isinstance(t, str):
                    tags.append(t)
                elif isinstance(t, int):
                    mapped_tag = chat_tags_map.get(t)
                    if mapped_tag:
                        tags.append(mapped_tag)
                elif isinstance(t, dict):
                    tag_name = t.get("name") or t.get("tag_name") or t.get("label") or ""
                    if tag_name:
                        tags.append(tag_name)

        # Extract Pancake customer ID
        pancake_id = str(cust.get("id") or cust.get("customer_id") or "")

        index[phone] = {
            "name": name.strip(),
            "fb_uid": fb_uid,
            "pancake_customer_id": pancake_id,
            "tags": tags,
        }

    return index


# ═══════════════════════════════════════════════════════════════════
# SYNC LOGIC
# ═══════════════════════════════════════════════════════════════════
async def _get_config() -> tuple[str, str, int, str, str]:
    """Read Pancake config from DB settings, with defaults."""
    settings = await get_settings()
    shop_id = settings.get("pancake_shop_id", DEFAULT_SHOP_ID)
    api_key = settings.get("pancake_api_key", DEFAULT_API_KEY)
    interval = int(settings.get("sync_interval", str(DEFAULT_SYNC_INTERVAL)))
    chat_page_id = settings.get("pancake_chat_page_id", DEFAULT_CHAT_PAGE_ID)
    chat_token = settings.get("pancake_chat_token", DEFAULT_CHAT_TOKEN)
    return shop_id, api_key, interval, chat_page_id, chat_token


async def _sync_once() -> int:
    """Run one full sync cycle. Returns number of profiles upserted."""
    shop_id, api_key, _, chat_page_id, chat_token = await _get_config()

    async with httpx.AsyncClient() as client:
        # Fetch orders and customers in parallel
        raw_orders, chat_tags_map, raw_customers = await asyncio.gather(
            _fetch_all_orders(client, shop_id, api_key),
            _fetch_all_chat_tags(client, chat_page_id, chat_token),
            _fetch_all_customers(client, chat_page_id, chat_token),
        )

    logger.info(
        "Fetched %d orders, %d customers, %d tags from Pancake",
        len(raw_orders),
        len(raw_customers),
        len(chat_tags_map),
    )

    if not raw_orders and not raw_customers:
        logger.info("No data fetched — skipping upsert.")
        return 0

    # Aggregate orders by phone
    order_aggregates = _aggregate_orders(raw_orders)

    # Build customer index by phone
    customer_index = _build_customer_index(raw_customers, chat_tags_map)

    # Log some customer data for debugging
    customers_with_uid = sum(1 for c in customer_index.values() if c.get("fb_uid"))
    customers_with_tags = sum(1 for c in customer_index.values() if c.get("tags"))
    logger.info(
        "Customer index: %d total, %d with fb_uid, %d with tags",
        len(customer_index),
        customers_with_uid,
        customers_with_tags,
    )

    # Merge: all phones from both orders and customers
    all_phones = set(order_aggregates.keys()) | set(customer_index.keys())

    upsert_data: list[dict] = []
    for phone in all_phones:
        agg = order_aggregates.get(phone, {"total": 0, "success": 0, "failed": 0, "spent": 0.0})
        cust = customer_index.get(phone, {})

        pancake_tags = cust.get("tags", [])

        tier_tag, priority_score = resolve_tier(
            pancake_tags=pancake_tags,
            total=agg["total"],
            success=agg["success"],
            failed=agg["failed"],
            spent=agg["spent"],
        )

        upsert_data.append(
            {
                "phone": phone,
                "total_orders": agg["total"],
                "success_orders": agg["success"],
                "failed_orders": agg["failed"],
                "total_spent": agg["spent"],
                "tier_tag": tier_tag,
                "priority_score": priority_score,
                "customer_name": cust.get("name"),
                "fb_uid": cust.get("fb_uid"),
                "pancake_customer_id": cust.get("pancake_customer_id"),
                "pancake_tags": pancake_tags,
            }
        )

    await bulk_upsert_profiles(upsert_data)
    logger.info("Upserted %d customer profiles.", len(upsert_data))

    # Save sync metadata for dashboard display
    await save_settings({
        "last_sync": datetime.now().isoformat(),
        "last_sync_count": str(len(upsert_data)),
    })

    return len(upsert_data)


async def pancake_sync_loop() -> None:
    """
    Infinite loop that syncs Pancake orders + customers into the local DB.
    Designed to be launched via asyncio.create_task() at app startup.
    """
    _, _, interval, _, _ = await _get_config()
    logger.info("Pancake sync worker started (interval=%ds)", interval)

    while True:
        try:
            count = await _sync_once()
            logger.info("Sync cycle complete — %d profiles updated.", count)
        except Exception as exc:
            logger.exception("Sync cycle failed: %s", exc)

        # Re-read interval in case it changed via Settings
        _, _, interval, _, _ = await _get_config()
        await asyncio.sleep(interval)
