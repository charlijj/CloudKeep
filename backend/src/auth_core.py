"""Authentication services for cloudkeep-controld.

Same proven scheme as v1 (bcrypt -> JWT -> one-time WS token), structured as
injectable services rather than module functions:

  AuthService   — password hashing/verification + JWT issue/verify.
  SessionStore  — one-time WebSocket tokens, each bound to a VM target.

Both are owned by the ControlPlane composition root (see app.py); nothing in
this module touches global state.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from config import Settings


@dataclass(frozen=True)
class Binding:
    """What a one-time WS token authorises: this user, this VM, this target.

    The browser never names a VNC address — it presents a token, and the
    bridge dials whatever this binding says. That makes 'point the bridge at
    an arbitrary host' impossible by construction.
    """
    username: str
    vm_id: int
    host: str
    port: int


class AuthService:
    """Password + JWT primitives, configured once from Settings."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Fixed dummy hash so an unknown username still runs a full bcrypt
        # verify -> constant-time login, no username enumeration via timing.
        self._dummy_hash = bcrypt.hashpw(
            b"cloudkeep-dummy-password", bcrypt.gensalt()).decode()

    @staticmethod
    def _pw_bytes(password: str) -> bytes:
        # bcrypt reads only the first 72 bytes; truncate consistently so an
        # over-long password can't raise and 500 the endpoint.
        return password.encode("utf-8")[:72]

    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(self._pw_bytes(password), bcrypt.gensalt()).decode()

    def verify_password(self, password: str, hashed: Optional[str]) -> bool:
        # Always run a real bcrypt verify (against the dummy hash if the user
        # doesn't exist) so response timing never reveals valid usernames.
        try:
            return bcrypt.checkpw(self._pw_bytes(password),
                                  (hashed or self._dummy_hash).encode())
        except (ValueError, TypeError):
            return False

    def make_jwt(self, username: str) -> str:
        now = int(time.time())
        claims = {
            "sub": username,
            "iat": now,
            "exp": now + self._settings.JWT_EXPIRY_MINUTES * 60,
            "jti": secrets.token_urlsafe(16),
        }
        return jwt.encode(claims, self._settings.JWT_SECRET,
                          algorithm=self._settings.JWT_ALGORITHM)

    def verify_jwt(self, token: str) -> Optional[str]:
        """Return the subject if valid, else None. Never raises."""
        try:
            claims = jwt.decode(token, self._settings.JWT_SECRET,
                                algorithms=[self._settings.JWT_ALGORITHM])
            return claims.get("sub")
        except JWTError:
            return None


class SessionStore:
    """In-memory one-time WS tokens, each bound to a VM target.

    Single-worker only by design: pop-on-use is atomic in a single-threaded
    event loop, which is what makes the tokens genuinely single-use without
    needing external storage. Expired-but-unused tokens are swept on each
    issue so the dict can't grow unboundedly.
    """

    def __init__(self, settings: Settings) -> None:
        self._ttl = settings.WS_TOKEN_EXPIRY_SECONDS
        self._tokens: dict[str, tuple[Binding, float]] = {}

    def issue(self, binding: Binding) -> str:
        now = time.monotonic()
        self._tokens = {t: v for t, v in self._tokens.items() if v[1] >= now}
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (binding, now + self._ttl)
        return token

    def consume(self, token: str) -> Optional[Binding]:
        """Validate and atomically invalidate; returns the binding or None."""
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        binding, expires_at = entry
        return binding if expires_at >= time.monotonic() else None
