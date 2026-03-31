"""
Utility helpers — phone normalization and regex extraction.
"""

import re

# Matches Vietnamese phone numbers: 10 digits starting with 0
_PHONE_RE = re.compile(r"(?<!\d)(0\d{9})(?!\d)")
# Also match +84… or 84… format (11-12 chars)
_PHONE_INTL_RE = re.compile(r"(?<!\d)(\+?84\d{9,10})(?!\d)")


def normalize_phone(raw: str) -> str:
    """
    Normalize a Vietnamese phone string:
      - Strip whitespace, dots, dashes
      - Convert +84 / 84 prefix to 0
    """
    cleaned = re.sub(r"[\s.\-()]", "", raw)
    cleaned = re.sub(r"^\+?84", "0", cleaned)
    return cleaned


def extract_phone(text: str) -> str | None:
    """
    Extract the first Vietnamese phone number found in *text*.
    Returns the normalized 10-digit number or None.
    """
    # Try standard 0… format first
    match = _PHONE_RE.search(text)
    if match:
        return normalize_phone(match.group(1))

    # Try +84 / 84 format
    match = _PHONE_INTL_RE.search(text)
    if match:
        return normalize_phone(match.group(1))

    return None
