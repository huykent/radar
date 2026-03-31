"""
Livestream Radar — FastAPI backend.
WebSocket hub for real-time comment enrichment + Pancake POS sync.
"""

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
import csv
import io
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from db import (
    init_db,
    get_profile,
    get_profile_by_fb_uid,
    get_profile_by_name,
    save_comment,
    get_settings,
    save_settings,
    get_all_unique_tags,
    get_grouped_comments,
    get_distinct_post_ids,
)
from sync_worker import pancake_sync_loop, trigger_sync, lookup_phone_on_demand, lookup_by_fb_uid
from tier import resolve_tier
from utils import extract_phone

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)-14s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("radar")

# ── Junk comment filter (server-side backup) ────────────────
JUNK_NAMES = {
    "quảng cáo", "điều khoản", "quyền riêng tư", "trung tâm quảng cáo",
    "trình quản lý quảng cáo", "công cụ chuyên nghiệp", "được tài trợ",
    "facebook", "meta ai", "manus ai", "bảng feed", "nhóm",
    "marketplace", "watch", "gaming", "messenger", "instagram",
    "threads", "thước phim", "kỷ niệm", "trang", "sự kiện",
    "bestselling", "bestseelling",
}


def is_junk_comment(fb_name: str, text: str) -> bool:
    name = (fb_name or "").lower().strip()
    if name in JUNK_NAMES or len(name) < 2:
        return True
    if text and name and text.strip().lower() == name:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
# CONNECTION MANAGER
# ═══════════════════════════════════════════════════════════════════
class ConnectionManager:
    """Manages active WebSocket connections and broadcasts."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("WS connected — total: %d", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        try:
            self._connections.remove(websocket)
        except ValueError:
            pass
        logger.info("WS disconnected — total: %d", len(self._connections))

    async def broadcast(self, data: dict) -> None:
        """Send JSON payload to every connected client, remove stale ones."""
        payload = json.dumps(data, ensure_ascii=False)
        stale: list[WebSocket] = []
        for conn in self._connections:
            try:
                await conn.send_text(payload)
            except Exception:
                stale.append(conn)
        for s in stale:
            try:
                self._connections.remove(s)
            except ValueError:
                pass


manager = ConnectionManager()


# ═══════════════════════════════════════════════════════════════════
# APP LIFESPAN
# ═══════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB + launch Pancake sync background task."""
    await init_db()
    logger.info("Database initialised.")

    sync_task = asyncio.create_task(pancake_sync_loop())
    logger.info("Pancake sync worker launched.")

    yield  # app is running

    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutdown complete.")


# ═══════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════
app = FastAPI(title="Livestream Radar", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")


# ── API Key Authentication ────────────────────────────────────────
async def _get_api_key() -> str:
    """Get API key from env var or DB settings."""
    env_key = os.environ.get("RADAR_API_KEY", "").strip()
    if env_key:
        return env_key
    settings = await get_settings()
    return settings.get("radar_api_key", "")


async def verify_api_key(request: Request) -> None:
    """Dependency: verify X-API-Key header on API routes."""
    server_key = await _get_api_key()
    if not server_key:
        return  # No key configured = open access (dev mode)
    client_key = request.headers.get("X-API-Key", "") or request.query_params.get("api_key", "")
    if not secrets.compare_digest(client_key, server_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Routes ────────────────────────────────────────────────────────

# Login page HTML (inline to avoid extra template file)
LOGIN_HTML = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>🔐 Radar Login</title>
<script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-gray-950 flex items-center justify-center min-h-screen">
<div class="bg-gray-900 border border-gray-800 rounded-2xl p-8 w-full max-w-sm shadow-2xl">
  <div class="text-center mb-6"><span class="text-4xl">📡</span>
    <h1 class="text-xl font-bold text-white mt-2">Livestream Radar</h1>
    <p class="text-xs text-gray-500 mt-1">Nhập mật khẩu để truy cập Dashboard</p>
  </div>
  {error}
  <form method="POST" action="/login">
    <input name="password" type="password" placeholder="Mật khẩu…"
      class="w-full px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm mb-4 focus:outline-none focus:border-purple-500" autofocus>
    <button type="submit"
      class="w-full py-3 bg-purple-600 hover:bg-purple-500 text-white font-bold rounded-lg transition-colors">
      🔓 Đăng nhập
    </button>
  </form>
</div></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_HTML.format(error=""))


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    password = form.get("password", "")
    server_key = await _get_api_key()

    if not server_key or secrets.compare_digest(password, server_key):
        # Set session cookie
        import hashlib
        token = hashlib.sha256((server_key or "open").encode()).hexdigest()[:32]
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie("radar_session", token, max_age=86400, httponly=True, samesite="lax")
        return response
    else:
        error_html = '<p class="text-red-400 text-xs text-center mb-4">❌ Sai mật khẩu</p>'
        return HTMLResponse(LOGIN_HTML.format(error=error_html))


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("radar_session")
    return response


@app.get("/")
async def index(request: Request):
    """Serve the Promax Radar Dashboard (protected by login)."""
    import hashlib
    server_key = await _get_api_key()
    if server_key:
        expected = hashlib.sha256(server_key.encode()).hexdigest()[:32]
        session = request.cookies.get("radar_session", "")
        if session != expected:
            return RedirectResponse(url="/login", status_code=303)
    api_key = server_key or ""
    return templates.TemplateResponse(
        "index.html", {"request": request, "radar_api_key": api_key}
    )


# ═══════════════════════════════════════════════════════════════════
# EXPORT API
# ═══════════════════════════════════════════════════════════════════
@app.get("/api/export/comments", dependencies=[Depends(verify_api_key)])
async def export_comments(since: str | None = None, post_id: str | None = None):
    """Export live comments grouped by user to an Excel-compatible CSV."""
    rows = await get_grouped_comments(since, post_id)
    
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow([
        "Họ Tên FB", 
        "Số Điện Thoại", 
        "Phân Loại", 
        "Bình Luận", 
        "Thời Gian Gần Nhất", 
        "UID"
    ])
    
    for r in rows:
        writer.writerow([
            r.get("fb_name", ""),
            r.get("phone", ""),
            r.get("tier_tag", ""),
            r.get("all_texts", ""),
            r.get("last_comment_time", ""),
            r.get("fb_uid", "")
        ])
        
    filename = f"live_comments_{post_id or 'all'}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.get("/api/sessions", dependencies=[Depends(verify_api_key)])
async def api_sessions():
    """Return list of livestream sessions (distinct post_ids)."""
    try:
        sessions = await get_distinct_post_ids()
        return JSONResponse({"status": "ok", "sessions": sessions})
    except Exception as exc:
        logger.error("Failed to get sessions: %s", exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@app.get("/api/debug/stats", dependencies=[Depends(verify_api_key)])
async def api_debug_stats():
    """Debug: show database stats for troubleshooting."""
    from db import get_db
    db = await get_db()
    try:
        # Count profiles
        c1 = await db.execute("SELECT COUNT(*) FROM customer_profiles")
        total = (await c1.fetchone())[0]

        c2 = await db.execute("SELECT COUNT(*) FROM customer_profiles WHERE customer_name IS NOT NULL AND customer_name != ''")
        with_name = (await c2.fetchone())[0]

        c3 = await db.execute("SELECT COUNT(*) FROM customer_profiles WHERE fb_uid IS NOT NULL AND fb_uid != ''")
        with_uid = (await c3.fetchone())[0]

        # Sample names
        c4 = await db.execute("SELECT customer_name, phone, tier_tag, total_orders FROM customer_profiles WHERE customer_name IS NOT NULL LIMIT 10")
        samples = [dict(r) for r in await c4.fetchall()]

        # Comments count
        c5 = await db.execute("SELECT COUNT(*) FROM live_comments")
        comments = (await c5.fetchone())[0]

        # Settings
        settings = await get_settings()

        return JSONResponse({
            "profiles_total": total,
            "profiles_with_name": with_name,
            "profiles_with_uid": with_uid,
            "live_comments_total": comments,
            "sample_profiles": samples,
            "settings_keys": list(settings.keys()),
            "has_chat_token": bool(settings.get("pancake_chat_token")),
            "has_pos_key": bool(settings.get("pancake_api_key")),
            "last_sync": settings.get("last_sync"),
            "last_sync_count": settings.get("last_sync_count"),
        })
    finally:
        await db.close()


# ═══════════════════════════════════════════════════════════════════
# PANCAKE WEBHOOK — REAL-TIME DATA RECEIVER
# ═══════════════════════════════════════════════════════════════════
@app.post("/webhook/pancake")
async def webhook_pancake(request: Request):
    """
    Receive real-time events from Pancake (orders, messages, customers).
    Configure this URL in Pancake Settings → Webhooks.
    URL: https://radar.kiwibebe.shop/webhook/pancake
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "detail": "Invalid JSON"}, status_code=400)

    event_type = body.get("event") or body.get("type") or body.get("action") or "unknown"
    logger.info("📩 Webhook received: %s", event_type)
    logger.debug("Webhook payload: %s", json.dumps(body, ensure_ascii=False)[:500])

    try:
        # ── Order events ──────────────────────────────────
        if event_type in ("order.created", "order.updated", "order_created", "order_updated", "new_order", "update_order"):
            order = body.get("data") or body.get("order") or body
            await _handle_webhook_order(order)

        # ── Conversation / Message events ─────────────────
        elif event_type in ("message.created", "new_message", "message_created", "conversation.updated"):
            msg_data = body.get("data") or body.get("message") or body
            await _handle_webhook_message(msg_data)

        # ── Customer tag events ───────────────────────────
        elif event_type in ("conversation.tag_added", "conversation.tag_removed", "tag_added", "tag_removed"):
            tag_data = body.get("data") or body
            await _handle_webhook_tag(tag_data, event_type)

        # ── Customer update events ────────────────────────
        elif event_type in ("customer.updated", "customer_updated"):
            cust_data = body.get("data") or body.get("customer") or body
            await _handle_webhook_customer(cust_data)

        # ── Generic / unknown — log for analysis ──────────
        else:
            logger.info("📩 Unhandled webhook event '%s'. Keys: %s", event_type, list(body.keys()))

    except Exception as exc:
        logger.exception("Webhook processing error: %s", exc)

    return JSONResponse({"status": "ok", "event": event_type})


async def _handle_webhook_order(order: dict):
    """Process a single order from webhook."""
    from utils import normalize_phone
    from tier import resolve_tier

    phone_raw = order.get("bill_phone_number") or order.get("customer_phone") or ""
    phone = normalize_phone(str(phone_raw))
    if not phone or len(phone) < 10:
        return

    customer_name = order.get("bill_full_name") or order.get("customer_name") or ""
    status = str(order.get("status", "")).lower().strip()
    price = float(order.get("total_price", 0) or 0)

    # Get existing profile or create new
    profile = await get_profile(phone)
    if profile:
        total = profile["total_orders"] + 1
        success = profile["success_orders"]
        failed = profile["failed_orders"]
        spent = profile["total_spent"]
    else:
        total = 1
        success = 0
        failed = 0
        spent = 0.0

    SUCCESS = {"success", "delivered", "done", "collected_money", "5", "6"}
    FAILED = {"returned", "canceled", "failed", "customer_cancel", "3", "4"}

    if status in SUCCESS:
        success += 1
        spent += price
    elif status in FAILED:
        failed += 1

    tier_tag, priority_score = resolve_tier(
        pancake_tags=profile.get("pancake_tags", []) if profile else [],
        total=total, success=success, failed=failed, spent=spent,
    )

    await bulk_upsert_profiles([{
        "phone": phone,
        "total_orders": total,
        "success_orders": success,
        "failed_orders": failed,
        "total_spent": spent,
        "tier_tag": tier_tag,
        "priority_score": priority_score,
        "customer_name": customer_name,
    }])

    logger.info("📦 Webhook order: %s → %s (%s)", phone, tier_tag, status)


async def _handle_webhook_message(msg_data: dict):
    """Process incoming message — update customer last interaction."""
    sender = msg_data.get("from") or msg_data.get("sender") or {}
    sender_name = sender.get("name") or sender.get("full_name") or ""
    sender_id = str(sender.get("id") or sender.get("psid") or "")

    if sender_name and sender_id:
        logger.info("💬 Webhook message from: %s (id: %s)", sender_name, sender_id)


async def _handle_webhook_tag(tag_data: dict, event_type: str):
    """Process tag added/removed on a conversation."""
    from tier import resolve_tier

    conversation_id = tag_data.get("conversation_id") or ""
    tag_name = tag_data.get("tag_name") or tag_data.get("tag", {}).get("text") or ""
    customer_name = tag_data.get("customer_name") or ""

    if tag_name:
        logger.info("🏷️ Webhook %s: '%s' on conversation %s (%s)",
                     event_type, tag_name, conversation_id, customer_name)


async def _handle_webhook_customer(cust_data: dict):
    """Process customer profile update from Pancake."""
    from utils import normalize_phone
    from tier import resolve_tier

    phone_raw = cust_data.get("phone_number") or cust_data.get("phone") or ""
    phone = normalize_phone(str(phone_raw))
    name = cust_data.get("name") or cust_data.get("full_name") or ""
    fb_uid = str(cust_data.get("facebook_id") or cust_data.get("psid") or "")

    if phone and len(phone) >= 10:
        profile = await get_profile(phone)
        if profile:
            # Update name/uid if available
            updates = {"phone": phone}
            if name:
                updates["customer_name"] = name
            if fb_uid:
                updates["fb_uid"] = fb_uid
            updates["total_orders"] = profile["total_orders"]
            updates["success_orders"] = profile["success_orders"]
            updates["failed_orders"] = profile["failed_orders"]
            updates["total_spent"] = profile["total_spent"]
            updates["tier_tag"] = profile["tier_tag"]
            updates["priority_score"] = profile["priority_score"]
            await bulk_upsert_profiles([updates])
            logger.info("👤 Webhook customer update: %s (%s)", name, phone)


# ═══════════════════════════════════════════════════════════════════
# SETTINGS API
# ═══════════════════════════════════════════════════════════════════
class SettingsPayload(BaseModel):
    pancake_shop_id: str | None = None
    pancake_api_key: str | None = None
    sync_interval: str | None = None
    selected_tags: str | None = None  # JSON array string
    pancake_chat_page_id: str | None = None
    pancake_chat_token: str | None = None


@app.get("/api/settings", dependencies=[Depends(verify_api_key)])
async def api_get_settings():
    """Return all settings. Mask API key for security."""
    settings = await get_settings()
    
    # Mask the API key
    if "pancake_api_key" in settings and settings["pancake_api_key"]:
        key = settings["pancake_api_key"]
        settings["pancake_api_key_masked"] = (
            key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
        )
        settings["pancake_api_key_set"] = True
    else:
        settings["pancake_api_key_masked"] = ""
        settings["pancake_api_key_set"] = False
    settings.pop("pancake_api_key", None)

    # Mask Chat token
    if "pancake_chat_token" in settings and settings["pancake_chat_token"]:
        token = settings["pancake_chat_token"]
        settings["pancake_chat_token_masked"] = (
            token[:4] + "****" + token[-4:] if len(token) > 8 else "****"
        )
        settings["pancake_chat_token_set"] = True
    else:
        settings["pancake_chat_token_masked"] = ""
        settings["pancake_chat_token_set"] = False
    settings.pop("pancake_chat_token", None)

    return JSONResponse(settings)


@app.post("/api/settings", dependencies=[Depends(verify_api_key)])
async def api_save_settings(payload: SettingsPayload):
    """Save Pancake settings."""
    data = {}
    if payload.pancake_shop_id is not None:
        data["pancake_shop_id"] = payload.pancake_shop_id
    if payload.pancake_api_key is not None and payload.pancake_api_key != "":
        data["pancake_api_key"] = payload.pancake_api_key
    if payload.pancake_chat_page_id is not None:
        data["pancake_chat_page_id"] = payload.pancake_chat_page_id
    if payload.pancake_chat_token is not None and payload.pancake_chat_token != "":
        data["pancake_chat_token"] = payload.pancake_chat_token
    if payload.sync_interval is not None:
        data["sync_interval"] = payload.sync_interval
    if payload.selected_tags is not None:
        data["selected_tags"] = payload.selected_tags
    
    if data:
        await save_settings(data)
    return JSONResponse({"status": "ok", "saved_keys": list(data.keys())})


@app.post("/api/settings/sync-now", dependencies=[Depends(verify_api_key)])
async def api_sync_now():
    """Trigger an immediate Pancake sync."""
    try:
        count = await trigger_sync()
        return JSONResponse({"status": "ok", "profiles_synced": count})
    except Exception as exc:
        logger.exception("Manual sync failed: %s", exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@app.get("/api/settings/tags", dependencies=[Depends(verify_api_key)])
async def api_get_tags():
    """Fetch available tags from locally synced customer profiles."""
    try:
        tags = await get_all_unique_tags()
        return JSONResponse({"status": "ok", "tags": tags})
    except Exception as exc:
        logger.error("Failed to extract local tags: %s", exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@app.post("/api/generate-key")
async def api_generate_key():
    """Generate a new random Radar API key and save to DB."""
    new_key = secrets.token_urlsafe(32)
    await save_settings({"radar_api_key": new_key})
    logger.info("New Radar API key generated.")
    return JSONResponse({"status": "ok", "api_key": new_key})


# ═══════════════════════════════════════════════════════════════════
# WEBSOCKET — REAL-TIME COMMENT ENRICHMENT
# ═══════════════════════════════════════════════════════════════════
@app.websocket("/ws/radar")
async def websocket_radar(ws: WebSocket, api_key: str | None = Query(None)):
    """
    WebSocket endpoint.
    Auth: query param ?api_key=xxx OR first message {"action":"auth","api_key":"xxx"}.
    Skipped if no key configured on server.
    """
    await manager.connect(ws)

    # ── WebSocket authentication ────────────────────────
    server_key = await _get_api_key()
    if server_key:
        authenticated = False
        # Try query param first
        if api_key and secrets.compare_digest(api_key, server_key):
            authenticated = True
        else:
            # Wait for auth message (first message within 10s)
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
                data = json.loads(raw)
                if data.get("action") == "auth" and data.get("api_key"):
                    if secrets.compare_digest(data["api_key"], server_key):
                        authenticated = True
                        await ws.send_json({"action": "auth_ok"})
                    else:
                        await ws.send_json({"action": "auth_fail", "detail": "Invalid API key"})
                else:
                    # Not an auth message — might be a comment, reject
                    await ws.send_json({"action": "auth_fail", "detail": "Auth required"})
            except (asyncio.TimeoutError, Exception):
                pass

        if not authenticated:
            await ws.send_json({"action": "auth_fail", "detail": "Authentication required"})
            await ws.close(code=4001, reason="Invalid API key")
            manager.disconnect(ws)
            return

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = data.get("action")
            if action != "new_comment":
                continue

            fb_name = data.get("fb_name", "Unknown")
            text = data.get("text", "")
            fb_uid = data.get("fb_uid")  # NEW: from Chrome Extension

            # ── Skip junk comments ───────────────────────────
            if is_junk_comment(fb_name, text):
                continue

            # ── Multi-strategy matching ──────────────────────
            profile = None
            match_method = "none"

            # Strategy 1: Match by FB UID (most reliable)
            if fb_uid and not profile:
                profile = await get_profile_by_fb_uid(fb_uid)
                if profile:
                    match_method = "fb_uid"

            # Strategy 2: Match by phone extracted from comment text
            phone = extract_phone(text)
            if phone and not profile:
                profile = await get_profile(phone)
                if profile:
                    match_method = "phone"

            # Strategy 3: Match by name (fuzzy)
            if not profile:
                profile = await get_profile_by_name(fb_name)
                if profile:
                    match_method = "name"

            # Strategy 4: On-demand POS lookup (only if phone found but no local profile)
            if not profile and phone:
                try:
                    profile = await lookup_phone_on_demand(phone)
                    if profile:
                        match_method = "on_demand"
                except Exception as exc:
                    logger.error("On-demand lookup error: %s", exc)

            # Strategy 5: Pancake Chat conversation search (by fb_name)
            if not profile and fb_name and fb_name != "Unknown":
                try:
                    profile = await lookup_by_fb_uid(fb_uid, fb_name)
                    if profile:
                        match_method = "chat"
                except Exception as exc:
                    logger.error("Chat lookup error: %s", exc)

            # ── Resolve tier ─────────────────────────────────
            if profile:
                tier_tag, priority_score = resolve_tier(
                    pancake_tags=profile.get("pancake_tags", []),
                    total=profile["total_orders"],
                    success=profile["success_orders"],
                    failed=profile["failed_orders"],
                    spent=profile["total_spent"],
                )
                total_spent = profile["total_spent"]
                success_orders = profile["success_orders"]
                failed_orders = profile["failed_orders"]
                pancake_tags = profile.get("pancake_tags", [])
            else:
                tier_tag = "⚪ KHÁCH MỚI"
                priority_score = 50
                total_spent = 0.0
                success_orders = 0
                failed_orders = 0
                pancake_tags = []

            if match_method != "none":
                logger.info(
                    "Matched [%s] via %s → %s", fb_name, match_method, tier_tag
                )

            # ── Save comment ─────────────────────────────────
            post_id = data.get("post_id")
            try:
                await save_comment(
                    fb_name, phone, text, tier_tag, priority_score, fb_uid, post_id
                )
            except Exception as exc:
                logger.error("Failed to save comment: %s", exc)

            # ── Broadcast augmented event ────────────────────
            augmented = {
                "action": "comment_augmented",
                "fb_name": fb_name,
                "text": text,
                "phone": phone,
                "fb_uid": fb_uid,
                "tier_tag": tier_tag,
                "priority_score": priority_score,
                "total_spent": total_spent,
                "success_orders": success_orders,
                "failed_orders": failed_orders,
                "pancake_tags": pancake_tags,
                "match_method": match_method,
                "post_id": post_id,
            }
            await manager.broadcast(augmented)

    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as exc:
        logger.error("WS error: %s", exc)
        manager.disconnect(ws)
