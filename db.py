"""
Database module — async SQLite with WAL mode.
Handles schema init, profile lookups, comment saves, bulk upserts, and settings.
"""

import aiosqlite
import json
import os
import unicodedata

DB_PATH = os.path.join(os.path.dirname(__file__), "radar.db")


async def get_db() -> aiosqlite.Connection:
    """Return an aiosqlite connection with WAL mode enabled."""
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    db = await get_db()
    try:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS customer_profiles (
                phone TEXT PRIMARY KEY,
                total_orders INTEGER DEFAULT 0,
                success_orders INTEGER DEFAULT 0,
                failed_orders INTEGER DEFAULT 0,
                total_spent REAL DEFAULT 0.0,
                tier_tag TEXT DEFAULT '⚪ KHÁCH MỚI',
                priority_score INTEGER DEFAULT 50,
                customer_name TEXT,
                fb_uid TEXT,
                pancake_customer_id TEXT,
                pancake_tags TEXT DEFAULT '[]',
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_phone ON customer_profiles(phone);
            CREATE INDEX IF NOT EXISTS idx_fb_uid ON customer_profiles(fb_uid);

            CREATE TABLE IF NOT EXISTS live_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fb_name TEXT,
                phone TEXT,
                text TEXT,
                tier_tag TEXT,
                priority_score INTEGER,
                fb_uid TEXT,
                post_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        await db.commit()

        # Migrate existing tables: add new columns if missing
        for col, default in [
            ("customer_name", "NULL"),
            ("fb_uid", "NULL"),
            ("pancake_customer_id", "NULL"),
            ("pancake_tags", "'[]'"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE customer_profiles ADD COLUMN {col} TEXT DEFAULT {default}"
                )
                await db.commit()
            except Exception:
                pass  # Column already exists

        # Add fb_uid column to live_comments if missing
        try:
            await db.execute(
                "ALTER TABLE live_comments ADD COLUMN fb_uid TEXT"
            )
            await db.commit()
        except Exception:
            pass

        # Add post_id column to live_comments if missing
        try:
            await db.execute(
                "ALTER TABLE live_comments ADD COLUMN post_id TEXT"
            )
            await db.commit()
        except Exception:
            pass

        # Create index on post_id (safe to run after migration)
        try:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_comments_post_id ON live_comments(post_id)"
            )
            await db.commit()
        except Exception:
            pass

        # ── Webhook events table ─────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'unknown',
                summary TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_category ON webhook_events(category)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_webhook_events_created ON webhook_events(created_at)"
        )
        await db.commit()

    finally:
        await db.close()


# ── Settings helpers ─────────────────────────────────────────────────

async def get_settings() -> dict:
    """Return all settings as a dict."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        await db.close()


async def save_setting(key: str, value: str) -> None:
    """Upsert a single setting."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def save_settings(data: dict) -> None:
    """Upsert multiple settings at once."""
    db = await get_db()
    try:
        await db.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in data.items()],
        )
        await db.commit()
    finally:
        await db.close()


# ── Profile lookups ──────────────────────────────────────────────────

def _row_to_profile(row) -> dict:
    """Convert a DB Row to a profile dict."""
    tags_raw = row["pancake_tags"] or "[]"
    try:
        tags = json.loads(tags_raw)
    except (json.JSONDecodeError, TypeError):
        tags = []

    return {
        "phone": row["phone"],
        "total_orders": row["total_orders"],
        "success_orders": row["success_orders"],
        "failed_orders": row["failed_orders"],
        "total_spent": row["total_spent"],
        "tier_tag": row["tier_tag"],
        "priority_score": row["priority_score"],
        "customer_name": row["customer_name"],
        "fb_uid": row["fb_uid"],
        "pancake_customer_id": row["pancake_customer_id"],
        "pancake_tags": tags,
        "last_updated": row["last_updated"],
    }


async def get_profile(phone: str) -> dict | None:
    """Look up a customer profile by phone number."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM customer_profiles WHERE phone = ?", (phone,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_profile(row)
    finally:
        await db.close()


async def get_profile_by_fb_uid(fb_uid: str) -> dict | None:
    """Look up a customer profile by Facebook UID."""
    if not fb_uid:
        return None
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM customer_profiles WHERE fb_uid = ?", (fb_uid,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_profile(row)
    finally:
        await db.close()


def _normalize_name(name: str) -> str:
    """Normalize a Vietnamese name for fuzzy matching: lowercase, remove accents."""
    if not name:
        return ""
    # Remove accents
    nfkd = unicodedata.normalize("NFKD", name.lower().strip())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


async def get_profile_by_name(fb_name: str) -> dict | None:
    """Look up a customer profile by fuzzy name matching."""
    if not fb_name or len(fb_name) < 2:
        return None
    normalized = _normalize_name(fb_name)
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM customer_profiles WHERE customer_name IS NOT NULL"
        )
        rows = await cursor.fetchall()
        for row in rows:
            db_name = _normalize_name(row["customer_name"] or "")
            if not db_name:
                continue
            # Exact normalized match or one contains the other
            if db_name == normalized or normalized in db_name or db_name in normalized:
                return _row_to_profile(row)
        return None
    finally:
        await db.close()


# ── Comment save ─────────────────────────────────────────────────────

async def save_comment(
    fb_name: str,
    phone: str | None,
    text: str,
    tier_tag: str,
    priority_score: int,
    fb_uid: str | None = None,
    post_id: str | None = None,
) -> None:
    """Insert a live comment record."""
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO live_comments (fb_name, phone, text, tier_tag, priority_score, fb_uid, post_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fb_name, phone, text, tier_tag, priority_score, fb_uid, post_id),
        )
        await db.commit()
    finally:
        await db.close()


# ── Bulk upsert ──────────────────────────────────────────────────────

async def bulk_upsert_profiles(profiles: list[dict]) -> None:
    """
    Bulk upsert aggregated customer profiles.
    Each dict must contain: phone, total_orders, success_orders,
    failed_orders, total_spent, tier_tag, priority_score.
    Optional: customer_name, fb_uid, pancake_customer_id, pancake_tags.
    """
    if not profiles:
        return
    db = await get_db()
    try:
        await db.executemany(
            """
            INSERT INTO customer_profiles
                (phone, total_orders, success_orders, failed_orders,
                 total_spent, tier_tag, priority_score,
                 customer_name, fb_uid, pancake_customer_id, pancake_tags,
                 last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(phone) DO UPDATE SET
                total_orders       = excluded.total_orders,
                success_orders     = excluded.success_orders,
                failed_orders      = excluded.failed_orders,
                total_spent        = excluded.total_spent,
                tier_tag           = excluded.tier_tag,
                priority_score     = excluded.priority_score,
                customer_name      = COALESCE(excluded.customer_name, customer_profiles.customer_name),
                fb_uid             = COALESCE(excluded.fb_uid, customer_profiles.fb_uid),
                pancake_customer_id = COALESCE(excluded.pancake_customer_id, customer_profiles.pancake_customer_id),
                pancake_tags       = COALESCE(excluded.pancake_tags, customer_profiles.pancake_tags),
                last_updated       = CURRENT_TIMESTAMP
            """,
            [
                (
                    p["phone"],
                    p["total_orders"],
                    p["success_orders"],
                    p["failed_orders"],
                    p["total_spent"],
                    p["tier_tag"],
                    p["priority_score"],
                    p.get("customer_name"),
                    p.get("fb_uid"),
                    p.get("pancake_customer_id"),
                    json.dumps(p.get("pancake_tags", []), ensure_ascii=False),
                )
                for p in profiles
            ],
        )
        await db.commit()
    finally:
        await db.close()

async def get_all_unique_tags() -> list[str]:
    """Extract all unique tags from local customer profiles."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT pancake_tags FROM customer_profiles WHERE pancake_tags IS NOT NULL AND pancake_tags != '[]'")
        rows = await cursor.fetchall()
        
        unique_tags = set()
        for row in rows:
            try:
                tags = json.loads(row["pancake_tags"])
                for t in tags:
                    if isinstance(t, str) and t.strip():
                        unique_tags.add(t.strip())
            except Exception:
                pass
        return sorted(list(unique_tags))
    finally:
        await db.close()

async def get_grouped_comments(since: str | None = None, post_id: str | None = None) -> list[dict]:
    """Return live comments grouped by user for CSV export."""
    db = await get_db()
    try:
        conditions = []
        if since:
            conditions.append(f"created_at >= '{since}'")
        if post_id:
            conditions.append(f"post_id = '{post_id}'")
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f'''
            SELECT 
                fb_name,
                MAX(phone) as phone,
                MAX(tier_tag) as tier_tag,
                MAX(priority_score) as priority_score,
                fb_uid,
                GROUP_CONCAT(text, ' | ') as all_texts,
                MAX(created_at) as last_comment_time
            FROM (SELECT * FROM live_comments {where_clause} ORDER BY created_at ASC)
            GROUP BY COALESCE(fb_uid, fb_name)
            ORDER BY last_comment_time DESC
        '''
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_distinct_post_ids() -> list[dict]:
    """Return list of distinct post_ids with comment counts."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT post_id, COUNT(*) as comment_count, 
                   MIN(created_at) as first_comment, MAX(created_at) as last_comment
            FROM live_comments 
            WHERE post_id IS NOT NULL AND post_id != ''
            GROUP BY post_id
            ORDER BY last_comment DESC
        """)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_raw_comments(post_id: str | None = None, limit: int = 500) -> list[dict]:
    """Return individual comments (not grouped) for a session, ordered by time."""
    db = await get_db()
    try:
        if post_id:
            cursor = await db.execute(
                """
                SELECT fb_name, phone, text, tier_tag, priority_score, fb_uid, post_id, created_at
                FROM live_comments
                WHERE post_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (post_id, limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT fb_name, phone, text, tier_tag, priority_score, fb_uid, post_id, created_at
                FROM live_comments
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def save_webhook_event(
    event_type: str, category: str, summary: str, payload: dict
) -> None:
    """Save a webhook event for the monitor page."""
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO webhook_events (event_type, category, summary, payload)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, category, summary, json.dumps(payload, ensure_ascii=False)),
        )
        await db.commit()
    finally:
        await db.close()


async def get_webhook_events(
    category: str | None = None, limit: int = 200
) -> list[dict]:
    """Return webhook events, optionally filtered by category."""
    db = await get_db()
    try:
        if category:
            cursor = await db.execute(
                """
                SELECT id, event_type, category, summary, payload, created_at
                FROM webhook_events
                WHERE category = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (category, limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT id, event_type, category, summary, payload, created_at
                FROM webhook_events
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_webhook_stats() -> dict:
    """Return count of webhook events by category."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT category, COUNT(*) as cnt
            FROM webhook_events
            GROUP BY category
        """)
        rows = await cursor.fetchall()
        stats = {row["category"]: row["cnt"] for row in rows}
        cursor2 = await db.execute("SELECT COUNT(*) as total FROM webhook_events")
        total_row = await cursor2.fetchone()
        stats["total"] = total_row["total"] if total_row else 0
        return stats
    finally:
        await db.close()
