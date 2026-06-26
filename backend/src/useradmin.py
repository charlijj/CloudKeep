"""Shared user create/update logic for the admin CLIs.

Both seed_user.py and review_requests.py funnel through upsert_user, so there is
exactly ONE place that mints a CloudKeep user (bcrypt hash + DB row). The public
account-request endpoint can NOT reach this — it only enqueues an inert request;
turning that request into a user is a host-side admin action that lands here.
"""
from __future__ import annotations

from auth_core import AuthService
from config import Settings
from db import Database


def upsert_user(db: Database, settings: Settings, *, username: str,
                password: str, role: str, max_vms: int, max_vcpus: int,
                max_mem_mb: int, max_disk_gb: int, email: str | None = None,
                first_name: str | None = None,
                last_name: str | None = None) -> str:
    """Create the user, or update password+role+quotas if they already exist.
    Returns 'created' or 'updated'. Contact details are only stored on create
    (update keeps the existing row's email/name)."""
    pw_hash = AuthService(settings).hash_password(password)
    if db.get_user(username):
        db.update_user(username, pw_hash, role, max_vms, max_vcpus,
                       max_mem_mb, max_disk_gb)
        return "updated"
    db.create_user(username, pw_hash, role, max_vms, max_vcpus, max_mem_mb,
                   max_disk_gb, email, first_name, last_name)
    return "created"
