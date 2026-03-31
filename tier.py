"""
Tier calculation engine.
Determines customer tier tag and priority score based on:
  1. Pancake POS tags (highest priority — user-defined)
  2. Order history stats (fallback — auto-calculated)
"""


# ── Pancake tag → display badge mapping ──────────────────────────────
# Keys are lowercased tag names from Pancake. Values are (emoji_tag, priority_score).
# This map is intentionally broad to catch common Vietnamese tag naming patterns.
PANCAKE_TAG_MAP = {
    # Boom / problem customers
    "khách boom hàng":     ("☠️ BOM HÀNG", -10),
    "boom hàng":           ("☠️ BOM HÀNG", -10),
    "boom":                ("☠️ BOM HÀNG", -10),
    "khách boom":          ("☠️ BOM HÀNG", -10),
    "bom hàng":            ("☠️ BOM HÀNG", -10),
    # No deposit
    "khách không cọc":     ("⚠️ KHÔNG CỌC", 10),
    "không cọc":           ("⚠️ KHÔNG CỌC", 10),
    # Browse / casual orderers
    "khách chốt đơn dạo":  ("🟡 CHỐT DẠO", 20),
    "chốt dạo":            ("🟡 CHỐT DẠO", 20),
    "chốt đơn dạo":        ("🟡 CHỐT DẠO", 20),
    "khách dạo":            ("🟡 CHỐT DẠO", 20),
    # VIP
    "vip":                 ("💎 KHÁCH VIP", 100),
    "khách vip":           ("💎 KHÁCH VIP", 100),
    # Returning / loyal
    "khách quen":          ("🟢 KHÁCH QUEN", 80),
    "khách cũ":            ("🟢 KHÁCH QUEN", 80),
}


def resolve_tier_from_tags(pancake_tags: list[str]) -> tuple[str, int] | None:
    """
    Given a list of Pancake tag names, resolve to the highest-priority
    display tier. Returns (tier_tag, priority_score) or None if no tags match.

    Priority order: BOM > KHÔNG CỌC > CHỐT DẠO > VIP > QUEN
    (Negative indicators take precedence so staff is warned first.)
    """
    if not pancake_tags:
        return None

    # Check tags in priority order (worst first)
    priority_order = [
        ("☠️ BOM HÀNG", -10),
        ("⚠️ KHÔNG CỌC", 10),
        ("🟡 CHỐT DẠO", 20),
        ("🟢 KHÁCH QUEN", 80),
        ("💎 KHÁCH VIP", 100),
    ]

    resolved = set()
    for tag in pancake_tags:
        key = tag.lower().strip()
        if key in PANCAKE_TAG_MAP:
            resolved.add(PANCAKE_TAG_MAP[key])

    if not resolved:
        return None

    # Return the one with lowest priority score (worst flag wins)
    for tier_tag, score in priority_order:
        if (tier_tag, score) in resolved:
            return (tier_tag, score)

    # Fallback to first resolved
    return next(iter(resolved))


def calculate_tier(
    total: int, success: int, failed: int, spent: float
) -> tuple[str, int]:
    """
    Return (tier_tag, priority_score) based on customer order history.

    Evaluation order (highest priority first):
      1. ☠️ BOM HÀNG   — failed >= 2  OR  cancel rate > 30%
      2. 💎 KHÁCH VIP  — spent >= 2 000 000  OR  success >= 5
      3. 🟢 KHÁCH QUEN — success >= 1  AND  failed == 0
      4. 🟡 CHỐT DẠO   — total >= 3  AND  success == 0
      5. ⚪ KHÁCH MỚI  — default / no data
    """
    # --- 1. BOM HÀNG --------------------------------------------------------
    cancel_rate = (failed / total * 100) if total > 0 else 0.0
    if failed >= 2 or (total > 0 and cancel_rate > 30):
        return ("☠️ BOM HÀNG", -10)

    # --- 2. VIP --------------------------------------------------------------
    if spent >= 2_000_000 or success >= 5:
        return ("💎 KHÁCH VIP", 100)

    # --- 3. KHÁCH QUEN -------------------------------------------------------
    if success >= 1 and failed == 0:
        return ("🟢 KHÁCH QUEN", 80)

    # --- 4. CHỐT DẠO ---------------------------------------------------------
    if total >= 3 and success == 0:
        return ("🟡 CHỐT DẠO", 20)

    # --- 5. Default -----------------------------------------------------------
    return ("⚪ KHÁCH MỚI", 50)


def resolve_tier(
    pancake_tags: list[str] | None,
    total: int,
    success: int,
    failed: int,
    spent: float,
) -> tuple[str, int]:
    """
    Unified tier resolver:
      1. If Pancake tags exist and match → use tag-based tier
      2. Otherwise → fall back to order-stats calculation
    """
    if pancake_tags:
        tag_tier = resolve_tier_from_tags(pancake_tags)
        if tag_tier:
            return tag_tier

    return calculate_tier(total, success, failed, spent)
