"""Shared input validators for usernames, emails, and names.

Used by the public account-request endpoint (app.py) and the admin review CLI
(review_requests.py) so the rules are identical wherever a name is checked.
Allow-list + length-bounded; anything else is rejected. Each cleaner returns the
normalised value or raises ValueError with a user-safe message.
"""
from __future__ import annotations

import re

# Lowercase handle: a-z, 0-9, underscore, hyphen; 3-32 chars.
USERNAME_RE = re.compile(r"[a-z0-9_-]{3,32}")
# Pragmatic email shape (not full RFC 5322): exactly the obvious structure, no
# spaces/control chars, bounded length. Real verification is an admin approving
# the request — this only rejects obvious garbage.
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Display names: letters/digits, space, dot, apostrophe, hyphen; 1-40 chars.
NAME_RE = re.compile(r"[\w .'\-]{1,40}")

EMAIL_MAX = 254


def clean_username(value: str) -> str:
    v = (value or "").strip().lower()
    if not USERNAME_RE.fullmatch(v):
        raise ValueError("username must be 3-32 chars: a-z, 0-9, '_' or '-'")
    return v


def clean_email(value: str) -> str:
    v = (value or "").strip()
    if len(v) > EMAIL_MAX or not EMAIL_RE.fullmatch(v):
        raise ValueError("invalid email address")
    return v


def clean_name(value: str, field: str = "name") -> str:
    v = (value or "").strip()
    if not NAME_RE.fullmatch(v):
        raise ValueError(f"invalid {field}")
    return v
