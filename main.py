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
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the Promax Radar Dashboard."""
    return templates.TemplateResponse(request=request, name="index.html")


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
    Receives: {"action": "new_comment", "fb_name": "...", "text": "...", "fb_uid": "..."}
    Broadcasts: augmented JSON with tier, profile data.
    Auth: ?api_key=xxx query param (skipped if no key configured).
    """
    # ── WebSocket authentication ────────────────────────
    server_key = await _get_api_key()
    if server_key:
        if not api_key or not secrets.compare_digest(api_key, server_key):
            await ws.close(code=4001, reason="Invalid API key")
            return

    await manager.connect(ws)
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
