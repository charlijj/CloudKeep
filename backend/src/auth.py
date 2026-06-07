"""CloudKeep gateway — FastAPI app.

Endpoints:
  GET  /health         -> liveness + VNC reachability
  POST /auth/login     -> validate credentials, issue JWT
  POST /auth/session   -> exchange JWT for a one-time WS session token
  WS   /ws             -> validate session token, run the VNC bridge
"""
import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import bcrypt
from jose import JWTError, jwt
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config import settings, USERS
from vnc_bridge import VNCBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cloudkeep")

# bcrypt only considers the first 72 bytes; >72 raises ValueError in the
# raw library (passlib silently truncated). Truncate consistently here so
# both hashing and verifying agree and an over-long password can't 500.
def _pw_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:72]


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# Fixed dummy hash so an unknown username still runs a full bcrypt verify.
# Equalises timing between "no such user" and "wrong password" -> no
# username enumeration via response time. Generated at startup.
_DUMMY_HASH = bcrypt.hashpw(b"cloudkeep-dummy-password", bcrypt.gensalt()).decode("utf-8")

_START = time.monotonic()


def _client_ip(request: Request) -> str:
    """Real client IP.

    All ingress arrives via NGINX over WireGuard (UFW blocks :8000 from
    everywhere except the EC2 peer), so the peer address is always the
    proxy. NGINX sets X-Forwarded-For with the true client IP, which
    cannot be spoofed from outside the tunnel.
    """
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else get_remote_address(request)


limiter = Limiter(key_func=_client_ip)


def _make_jwt(user: str) -> str:
    """Issue a signed, short-lived JWT for an authenticated user."""
    now = int(time.time())
    claims = {
        "sub": user,
        "iat": now,
        "exp": now + settings.JWT_EXPIRY_MINUTES * 60,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _verify_jwt(token: str) -> str | None:
    """Return the subject if the token is valid, else None."""
    try:
        claims = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
        return claims.get("sub")
    except JWTError:
        return None


class SessionStore:
    """In-memory one-time WS token store. Single-worker only.

    Tokens are popped on use (single-use + atomic in a single-threaded
    event loop). Expired-but-unused tokens are swept on each issue.
    """

    def __init__(self) -> None:
        self._tokens: dict[str, float] = {}

    def issue(self, user: str) -> str:
        now = time.monotonic()
        self._tokens = {t: exp for t, exp in self._tokens.items() if exp >= now}
        token = secrets.token_urlsafe(32)
        self._tokens[token] = now + settings.WS_TOKEN_EXPIRY_SECONDS
        return token

    def consume(self, token: str) -> bool:
        """Validate and atomically invalidate a token."""
        expires_at = self._tokens.pop(token, None)  # pop -> single-use
        return expires_at is not None and expires_at >= time.monotonic()


sessions = SessionStore()


async def _probe_vnc() -> bool:
    """Open and immediately close a TCP connection to the VNC server."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.VNC_HOST, settings.VNC_PORT),
            timeout=2,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Probe VNC at startup but never fail — it may come up after us.
    if await _probe_vnc():
        logger.info("startup: VNC reachable at %s:%s", settings.VNC_HOST, settings.VNC_PORT)
    else:
        logger.warning("startup: VNC NOT reachable yet")
    yield
    logger.info("shutdown")


app = FastAPI(title="CloudKeep Gateway", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.ALLOWED_ORIGIN],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "vnc_reachable": await _probe_vnc(),
        "uptime_seconds": int(time.monotonic() - _START),
    }


@app.post("/auth/login")
@limiter.limit(settings.AUTH_RATE_LIMIT)
async def login(request: Request, body: LoginRequest) -> JSONResponse:
    # Always verify against a hash (real or dummy) -> constant time.
    stored = USERS.get(body.username, _DUMMY_HASH)
    valid = _verify_password(body.password, stored)
    if not valid or body.username not in USERS:
        logger.info("login failed user=%s ip=%s", body.username, _client_ip(request))
        return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})
    logger.info("login ok user=%s ip=%s", body.username, _client_ip(request))
    return JSONResponse(
        {
            "access_token": _make_jwt(body.username),
            "token_type": "bearer",
            "expires_in": settings.JWT_EXPIRY_MINUTES * 60,
        }
    )


@app.post("/auth/session")
async def create_session(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    user = _verify_jwt(token) if token else None
    if not user:
        logger.warning("session denied ip=%s reason=invalid_jwt", _client_ip(request))
        return JSONResponse(status_code=401, content={"detail": "Invalid token"})
    logger.info("session issued user=%s ip=%s", user, _client_ip(request))
    return JSONResponse(
        {"session_token": sessions.issue(user), "expires_in": settings.WS_TOKEN_EXPIRY_SECONDS}
    )


@app.websocket("/ws")
async def ws_vnc(websocket: WebSocket) -> None:
    token = websocket.query_params.get("session_token", "")
    ip = websocket.client.host if websocket.client else "unknown"
    if not sessions.consume(token):
        logger.warning("ws rejected ip=%s reason=invalid_session_token", ip)
        await websocket.close(code=4001)
        return
    await websocket.accept()
    bridge = VNCBridge(settings.VNC_HOST, settings.VNC_PORT)
    try:
        await bridge.connect()
        await bridge.relay(websocket)
    except ConnectionError:
        logger.warning("VNC connect failed")
        await websocket.close(code=1011)  # internal error
    finally:
        await bridge.close()
