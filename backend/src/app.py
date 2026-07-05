"""cloudkeep-controld — FastAPI control plane (runs on the KVM host).

Structure: ControlPlane is the composition root — it owns every service (DB,
auth, provisioner, tracker, manager) and their lifecycle. The FastAPI app holds
exactly one ControlPlane on app.state; route handlers receive it through
dependencies. No module-level mutable state.

Endpoints
  POST /auth/login            credentials -> JWT
  GET  /resources             host pool + caller quota/usage + sizing bounds
  GET  /vms                   caller's VMs (admin: all)
  POST /vms                   build a VM (validated against quota + host free)
  GET  /vms/{id}              one VM
  POST /vms/{id}/start|stop   lifecycle
  DELETE /vms/{id}            tear down + reclaim
  POST /vms/{id}/session      one-time WS token bound to that VM's VNC
  POST /vms/{id}/console-session  one-time WS token bound to that VM's console
  WS   /ws                    consume token -> relay bytes to the bound VM
  WS   /console               consume token -> stream the VM's serial boot log
  GET  /health                liveness + libvirt/pool status
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from auth_core import AuthService, SessionStore
from config import settings
from console import serve_console
from db import Database
from notify import notify_email_verification
from provisioner import build_provisioner
from resources import ResourceTracker, QuotaError
from validation import clean_username, clean_email, clean_name
from vmmanager import VMManager, NotFound, Forbidden, Conflict
from vnc_bridge import VNCBridge

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cloudkeep")


class ControlPlane:
    """Composition root: builds the service graph and manages its lifecycle.

    Construction order matters only in that everything shares one Database and
    one Provisioner; the manager additionally needs the tracker (admission
    checks) and the session store (minting VM-bound WS tokens).
    """

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.db = Database(settings.DB_PATH)
        self.auth = AuthService(settings)
        self.sessions = SessionStore(settings)
        self.provisioner = build_provisioner()
        self.tracker = ResourceTracker(self.db, self.provisioner)
        self.manager = VMManager(self.db, self.provisioner, self.tracker,
                                 self.sessions)

    async def start(self) -> None:
        await self.manager.start()
        logger.info("controld up: libvirt=%s db=%s",
                    settings.LIBVIRT_URI, settings.DB_PATH)

    async def stop(self) -> None:
        await self.manager.stop()

    async def health(self) -> dict:
        """Liveness summary; degrades (not fails) if libvirt is unreachable."""
        libvirt_ok, pool_free = True, None
        try:
            pool_free = (await self.provisioner.capacity()).disk_gb_free
        except Exception:
            libvirt_ok = False
        return {
            "status": "ok" if libvirt_ok else "degraded",
            "libvirt_ok": libvirt_ok,
            "pool_free_gb": pool_free,
            "uptime_seconds": int(time.monotonic() - self.started_at),
        }


# --------------------------------------------------------------------------
# App assembly
# --------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    # Read X-Real-IP, which the edge sets to $remote_addr and OVERWRITES on every
    # proxied request — so it is the true peer and a client cannot forge it.
    # We deliberately do NOT trust X-Forwarded-For: the edge builds it with
    # $proxy_add_x_forwarded_for, which APPENDS the peer to whatever the client
    # sent, leaving the client-controlled value as the first element. Trusting
    # that would let a caller spoof their IP to evade the per-IP rate limits
    # (login brute-force, signup) and the per-IP pending cap.
    return request.headers.get("x-real-ip") or get_remote_address(request)


limiter = Limiter(key_func=_client_ip)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cp = ControlPlane()
    await app.state.cp.start()
    yield
    await app.state.cp.stop()


app = FastAPI(title="CloudCrypt controld", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.ALLOWED_ORIGIN],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": "Too many requests"})


# Domain exceptions -> HTTP, registered once instead of wrapping every endpoint.
@app.exception_handler(NotFound)
async def _h_not_found(request: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "No such VM"})


@app.exception_handler(Forbidden)
async def _h_forbidden(request: Request, exc: Forbidden) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": "Not your VM"})


@app.exception_handler(Conflict)
async def _h_conflict(request: Request, exc: Conflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(QuotaError)
async def _h_quota(request: Request, exc: QuotaError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---- dependencies ----------------------------------------------------------
def get_cp(request: Request) -> ControlPlane:
    return request.app.state.cp


async def current_user(request: Request,
                       cp: ControlPlane = Depends(get_cp)) -> dict:
    """Resolve the Bearer JWT to a DB user row, or 401."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    username = cp.auth.verify_jwt(token) if token else None
    user = cp.db.get_user(username) if username else None
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


# ---- request models --------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class CreateVMRequest(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    vcpus: int
    mem_mb: int
    disk_gb: int


# ---- auth ------------------------------------------------------------------
@app.post("/auth/login")
@limiter.limit(settings.AUTH_RATE_LIMIT)
async def login(request: Request, body: LoginRequest,
                cp: ControlPlane = Depends(get_cp)) -> JSONResponse:
    user = cp.db.get_user(body.username)
    ok = cp.auth.verify_password(body.password, user["pw_hash"] if user else None)
    if not ok or not user:
        logger.info("login failed user=%s ip=%s", body.username, _client_ip(request))
        return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})
    logger.info("login ok user=%s ip=%s", body.username, _client_ip(request))
    return JSONResponse({
        "access_token": cp.auth.make_jwt(body.username),
        "token_type": "bearer",
        "expires_in": settings.JWT_EXPIRY_MINUTES * 60,
    })


# ---- self-service account requests -----------------------------------------
def _signup_redirect(anchor: str) -> RedirectResponse:
    # 303 so the browser re-GETs the landing page; the #account-<anchor>
    # fragment drives a pure-CSS :target modal (the landing page runs no JS).
    return RedirectResponse(f"/#account-{anchor}", status_code=303)


def _invite_ok(code: str, valid: set[str]) -> bool:
    # Constant-time across every configured code; an empty set is always False
    # (fail-closed — no codes configured means signups are closed). Compare on
    # bytes: hmac.compare_digest raises TypeError on non-ASCII str, which on a
    # raw POST would surface as an unhandled 500 instead of a clean rejection.
    ok = False
    code_b = code.encode("utf-8", "ignore")
    for c in valid:
        if hmac.compare_digest(code_b, c.encode("utf-8", "ignore")):
            ok = True
    return ok


@app.post("/account-requests")
@limiter.limit(settings.SIGNUP_RATE_LIMIT)
async def request_account(request: Request,
                          username: str = Form(""),
                          email: str = Form(""),
                          first_name: str = Form(""),
                          last_name: str = Form(""),
                          invite_code: str = Form(""),
                          hp_check: str = Form(""),      # honeypot — must stay empty
                          cp: ControlPlane = Depends(get_cp)) -> RedirectResponse:
    """Public, UNAUTHENTICATED signup intake. A request is INERT: it only
    enqueues a row for an admin to approve via review_requests.py — it creates
    no user and grants nothing. Defense in depth: master switch, invite-code
    gate (fail-closed), origin check, honeypot, strict validation, per-IP +
    global queue caps (the edge adds TLS + its own rate limit). When email
    verification is active the row is held 'unverified' until the owner clicks
    a tokenised link, so admins only ever see requests with a proven address."""
    ip = _client_ip(request)

    if not settings.ACCOUNT_REQUESTS_ENABLED:
        return _signup_redirect("closed")

    # Drop a PRESENT, real, cross-site Origin. Browsers legitimately send
    # `Origin: null` on form-POST navigations under a strict Referrer-Policy
    # (e.g. no-referrer) or from sandboxed contexts, and non-browser clients omit
    # it entirely — so treat null/absent as "can't tell" and rely on the invite
    # gate + honeypot + caps rather than hard-failing our own users.
    origin = request.headers.get("origin")
    if origin and origin not in ("null", settings.ALLOWED_ORIGIN):
        logger.warning("account request bad origin ip=%s origin=%s", ip, origin)
        return _signup_redirect("error")

    # Honeypot: a real user never sees this field; a bot fills it. Feign success
    # (so the bot learns nothing) but store nothing.
    if hp_check.strip():
        logger.info("account request honeypot tripped ip=%s", ip)
        return _signup_redirect("requested")

    # Invite-code gate (fail-closed when no codes are configured).
    if not _invite_ok(invite_code, settings.invite_codes):
        logger.info("account request invalid invite code ip=%s", ip)
        return _signup_redirect("error")

    # Strict validation; one generic error anchor => no field-level oracle.
    try:
        username = clean_username(username)
        email = clean_email(email)
        first_name = clean_name(first_name, "first name")
        last_name = clean_name(last_name, "last name")
    except ValueError:
        return _signup_redirect("error")

    # Abuse caps: bound the OPEN queue (unverified + pending) globally and per IP
    # so unverified rows (each of which sends an email) can't flood or bomb.
    if cp.db.count_open_requests() >= settings.MAX_PENDING_REQUESTS:
        logger.warning("account request queue full ip=%s", ip)
        return _signup_redirect("error")
    if cp.db.count_open_requests_by_ip(ip) >= settings.MAX_PENDING_PER_IP:
        return _signup_redirect("error")

    if settings.email_verification_active:
        # Prove email ownership before the request becomes reviewable. Send the
        # link FIRST (in a thread so smtplib can't stall the event loop); only
        # persist the 'unverified' row if the mail actually went out, so a
        # misconfigured SMTP can't leave dangling unverifiable rows.
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(time.time() + settings.VERIFY_TOKEN_TTL_HOURS * 3600))
        verify_url = f"{settings.portal_url}/ck/account-requests/verify?token={raw}"
        sent = await asyncio.get_running_loop().run_in_executor(
            None, lambda: notify_email_verification(
                settings, email=email, first_name=first_name, verify_url=verify_url))
        if not sent:
            logger.warning("account request verify-email send failed ip=%s", ip)
            return _signup_redirect("error")
        cp.db.create_account_request(username, email, first_name, last_name, ip,
                                     status="unverified", verify_token_hash=token_hash,
                                     verify_expires_at=expires)
        cp.db.audit(None, "account.request",
                    f"{username} <{email}> ip={ip} (awaiting email verification)")
        logger.info("account request awaiting verification user=%s ip=%s", username, ip)
        return _signup_redirect("verify-sent")

    cp.db.create_account_request(username, email, first_name, last_name, ip)
    cp.db.audit(None, "account.request", f"{username} <{email}> ip={ip}")
    logger.info("account request queued user=%s ip=%s", username, ip)
    return _signup_redirect("requested")


@app.get("/account-requests/verify")
@limiter.limit(settings.SIGNUP_RATE_LIMIT)
async def verify_account_request(request: Request, token: str = "",
                                 cp: ControlPlane = Depends(get_cp)) -> RedirectResponse:
    """Consume an email-verification token: promote the matching 'unverified'
    request to 'pending' (now reviewable). Token is single-use, time-limited,
    and only its SHA-256 is stored, so the DB never holds a live token."""
    if not token:
        return _signup_redirect("verify-error")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    req = cp.db.get_unverified_request_by_token(token_hash)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if req is None or (req["verify_expires_at"] and now_iso > req["verify_expires_at"]):
        return _signup_redirect("verify-error")
    cp.db.mark_request_verified(req["id"])
    cp.db.audit(None, "account.verify", f"req#{req['id']} {req['username']}")
    logger.info("account request verified id=%s user=%s", req["id"], req["username"])
    return _signup_redirect("verified")


# ---- resources -------------------------------------------------------------
@app.get("/resources")
async def get_resources(user: dict = Depends(current_user),
                        cp: ControlPlane = Depends(get_cp)) -> dict:
    return await cp.tracker.snapshot(user)


@app.get("/dashboard")
async def dashboard(user: dict = Depends(current_user),
                    cp: ControlPlane = Depends(get_cp)) -> dict:
    """VMs + resources in one round-trip (the SPA polls both together)."""
    return {"vms": cp.manager.list_vms(user),
            "resources": await cp.tracker.snapshot(user)}


# ---- VM lifecycle ----------------------------------------------------------
@app.get("/vms")
async def list_vms(user: dict = Depends(current_user),
                   cp: ControlPlane = Depends(get_cp)) -> dict:
    return {"vms": cp.manager.list_vms(user)}


@app.post("/vms")
@limiter.limit(settings.PROVISION_RATE_LIMIT)
async def create_vm(request: Request, body: CreateVMRequest,
                    user: dict = Depends(current_user),
                    cp: ControlPlane = Depends(get_cp)) -> JSONResponse:
    # QuotaError/Conflict are mapped to 400/409 by the registered handlers.
    vm = await cp.manager.request_vm(user, body.label, body.vcpus,
                                     body.mem_mb, body.disk_gb)
    # 202: accepted but not yet built — the worker provisions asynchronously
    # and the dashboard polls until the state flips to RUNNING or ERROR.
    return JSONResponse(status_code=202, content=vm)


@app.get("/vms/{vm_id}")
async def get_vm(vm_id: int, user: dict = Depends(current_user),
                 cp: ControlPlane = Depends(get_cp)) -> dict:
    return cp.manager.get_vm(user, vm_id)


@app.post("/vms/{vm_id}/start")
async def start_vm(vm_id: int, user: dict = Depends(current_user),
                   cp: ControlPlane = Depends(get_cp)) -> dict:
    return await cp.manager.start_vm(user, vm_id)


@app.post("/vms/{vm_id}/stop")
async def stop_vm(vm_id: int, user: dict = Depends(current_user),
                  cp: ControlPlane = Depends(get_cp)) -> dict:
    return await cp.manager.stop_vm(user, vm_id)


@app.delete("/vms/{vm_id}")
async def delete_vm(vm_id: int, user: dict = Depends(current_user),
                    cp: ControlPlane = Depends(get_cp)) -> Response:
    await cp.manager.delete_vm(user, vm_id)
    # 204 must carry no body — a JSONResponse(content=None) would emit `null`
    # and trip "Response content longer than Content-Length".
    return Response(status_code=204)


@app.post("/vms/{vm_id}/session")
async def create_vm_session(vm_id: int, user: dict = Depends(current_user),
                            cp: ControlPlane = Depends(get_cp)) -> dict:
    token = cp.manager.mint_session(user, vm_id, "vnc")
    return {"session_token": token,
            "expires_in": settings.WS_TOKEN_EXPIRY_SECONDS}


@app.post("/vms/{vm_id}/console-session")
async def create_console_session(vm_id: int, user: dict = Depends(current_user),
                                 cp: ControlPlane = Depends(get_cp)) -> dict:
    token = cp.manager.mint_session(user, vm_id, "console")
    return {"session_token": token,
            "expires_in": settings.WS_TOKEN_EXPIRY_SECONDS}


# ---- VNC bridge ------------------------------------------------------------
@app.websocket("/ws")
async def ws_vnc(websocket: WebSocket) -> None:
    cp: ControlPlane = websocket.app.state.cp
    token = websocket.query_params.get("session_token", "")
    ip = websocket.client.host if websocket.client else "unknown"
    # Defense-in-depth against cross-site WebSocket hijacking: the browser sends
    # Origin = the SPA's page origin, which must match our single allowed origin.
    # Checked before consuming the token so a bad-origin attempt can't burn it.
    if websocket.headers.get("origin") != settings.ALLOWED_ORIGIN:
        logger.warning("ws rejected ip=%s reason=bad_origin origin=%s",
                       ip, websocket.headers.get("origin"))
        await websocket.close(code=4403)
        return
    binding = cp.sessions.consume(token)         # single-use, VM-bound
    if binding is None:
        logger.warning("ws rejected ip=%s reason=invalid_session_token", ip)
        await websocket.close(code=4001)
        return
    # A token's kind is pinned at mint time; a console token must not open the
    # VNC bridge (and vice versa, below). It's already consumed, so just reject.
    if binding.kind != "vnc":
        logger.warning("ws rejected ip=%s reason=wrong_kind kind=%s", ip, binding.kind)
        await websocket.close(code=4403)
        return
    await websocket.accept()
    bridge = VNCBridge(binding.host, binding.port)
    try:
        await bridge.connect()
        await bridge.relay(websocket)
    except ConnectionError:
        logger.warning("VNC connect failed vm=%s", binding.vm_id)
        await websocket.close(code=1011)
    finally:
        await bridge.close()


# ---- live serial console ---------------------------------------------------
@app.websocket("/console")
async def ws_console(websocket: WebSocket) -> None:
    """Stream a VM's serial boot log (read-only). Same token/origin contract as
    /ws, but the token must be kind="console" and we attach to the libvirt
    domain rather than dialling a VNC socket."""
    cp: ControlPlane = websocket.app.state.cp
    token = websocket.query_params.get("session_token", "")
    ip = websocket.client.host if websocket.client else "unknown"
    if websocket.headers.get("origin") != settings.ALLOWED_ORIGIN:
        logger.warning("console ws rejected ip=%s reason=bad_origin origin=%s",
                       ip, websocket.headers.get("origin"))
        await websocket.close(code=4403)
        return
    binding = cp.sessions.consume(token)         # single-use, VM-bound
    if binding is None:
        logger.warning("console ws rejected ip=%s reason=invalid_session_token", ip)
        await websocket.close(code=4001)
        return
    if binding.kind != "console":
        logger.warning("console ws rejected ip=%s reason=wrong_kind kind=%s",
                       ip, binding.kind)
        await websocket.close(code=4403)
        return
    await serve_console(websocket, binding.name)


# ---- health ----------------------------------------------------------------
@app.get("/health")
async def health(cp: ControlPlane = Depends(get_cp)) -> dict:
    return await cp.health()
