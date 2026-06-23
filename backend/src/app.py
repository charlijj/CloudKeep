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

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from auth_core import AuthService, SessionStore
from config import settings
from console import serve_console
from db import Database
from provisioner import build_provisioner
from resources import ResourceTracker, QuotaError
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
    # Trustworthy because UFW only admits the NGINX/WireGuard peer, which
    # always sets X-Forwarded-For with the true client address.
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else get_remote_address(request)


limiter = Limiter(key_func=_client_ip)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cp = ControlPlane()
    await app.state.cp.start()
    yield
    await app.state.cp.stop()


app = FastAPI(title="CloudKeep controld", lifespan=lifespan)
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
