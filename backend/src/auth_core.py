"""Authentication primitives for cloudkeep-controld.

Same proven scheme as v1 (bcrypt -> JWT -> one-time WS token), with two
changes for the fleet model:
  * users are looked up in the DB, not a dict;
  * WS session tokens are bound at mint time to a specific VM's VNC target,
    so a token can only ever open the bridge to the VM it was issued for.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from config import settings

# Fixed dummy hash so an unknown username still runs a full bcrypt verify ->
# constant-time login, no username enumeration via response timing.
_DUMMY_HASH = bcrypt.hashpw(b"cloudkeep-dummy-password", bcrypt.gensalt()).decode()


def _pw_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:72]  # bcrypt only reads 72 bytes


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: Optional[str]) -> bool:
    # Always run a real bcrypt verify (against the dummy hash if no user) so
    # timing does not reveal whether the username exists.
    try:
        return bcrypt.checkpw(_pw_bytes(password), (hashed or _DUMMY_HASH).encode())
    except (ValueError, TypeError):
        return False


def make_jwt(username: str) -> str:
    now = int(time.time())
    claims = {
        "sub": username,
        "iat": now,
        "exp": now + settings.JWT_EXPIRY_MINUTES * 60,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_jwt(token: str) -> Optional[str]:
    try:
        claims = jwt.decode(token, settings.JWT_SECRET,
                            algorithms=[settings.JWT_ALGORITHM])
        return claims.get("sub")
    except JWTError:
        return None


@dataclass(frozen=True)
class Binding:
    """What a one-time WS token authorises: this user, this VM, this target."""
    username: str
    vm_id: int
    host: str
    port: int


class SessionStore:
    """In-memory one-time WS tokens, each bound to a VM target.

    Single-worker only. Tokens are popped on use (single-use + atomic in a
    single-threaded event loop); expired tokens are swept on each issue.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[Binding, float]] = {}

    def issue(self, binding: Binding) -> str:
        now = time.monotonic()
        self._tokens = {t: v for t, v in self._tokens.items() if v[1] >= now}
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (binding, now + settings.WS_TOKEN_EXPIRY_SECONDS)
        return token

    def consume(self, token: str) -> Optional[Binding]:
        entry = self._tokens.pop(token, None)  # pop -> single-use
        if entry is None:
            return None
        binding, expires_at = entry
        return binding if expires_at >= time.monotonic() else None
