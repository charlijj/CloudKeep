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
  WS   /ws                    consume token -> relay bytes to the bound VM
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
    try:
        vm = await cp.manager.request_vm(user, body.label, body.vcpus,
                                         body.mem_mb, body.disk_gb)
    except QuotaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # 202: accepted but not yet built — the worker provisions asynchronously
    # and the dashboard polls /vms until the state flips to RUNNING or ERROR.
    return JSONResponse(status_code=202, content=vm)


@app.get("/vms/{vm_id}")
async def get_vm(vm_id: int, user: dict = Depends(current_user),
                 cp: ControlPlane = Depends(get_cp)) -> dict:
    return _translate(lambda: cp.manager.get_vm(user, vm_id))


@app.post("/vms/{vm_id}/start")
async def start_vm(vm_id: int, user: dict = Depends(current_user),
                   cp: ControlPlane = Depends(get_cp)) -> dict:
    return await _translate_async(cp.manager.start_vm(user, vm_id))


@app.post("/vms/{vm_id}/stop")
async def stop_vm(vm_id: int, user: dict = Depends(current_user),
                  cp: ControlPlane = Depends(get_cp)) -> dict:
    return await _translate_async(cp.manager.stop_vm(user, vm_id))


@app.delete("/vms/{vm_id}")
async def delete_vm(vm_id: int, user: dict = Depends(current_user),
                    cp: ControlPlane = Depends(get_cp)) -> Response:
    await _translate_async(cp.manager.delete_vm(user, vm_id))
    # 204 must carry no body — a JSONResponse(content=None) would emit `null`
    # and trip "Response content longer than Content-Length".
    return Response(status_code=204)


@app.post("/vms/{vm_id}/session")
async def create_vm_session(vm_id: int, user: dict = Depends(current_user),
                            cp: ControlPlane = Depends(get_cp)) -> dict:
    token = _translate(lambda: cp.manager.mint_session(user, vm_id))
    return {"session_token": token,
            "expires_in": settings.WS_TOKEN_EXPIRY_SECONDS}


def _translate(fn):
    """Map domain exceptions to HTTP codes for sync manager calls."""
    try:
        return fn()
    except NotFound:
        raise HTTPException(status_code=404, detail="No such VM")
    except Forbidden:
        raise HTTPException(status_code=403, detail="Not your VM")
    except (Conflict, QuotaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


async def _translate_async(coro):
    """Map domain exceptions to HTTP codes for async manager calls."""
    try:
        return await coro
    except NotFound:
        raise HTTPException(status_code=404, detail="No such VM")
    except Forbidden:
        raise HTTPException(status_code=403, detail="Not your VM")
    except (Conflict, QuotaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---- VNC bridge ------------------------------------------------------------
@app.websocket("/ws")
async def ws_vnc(websocket: WebSocket) -> None:
    cp: ControlPlane = websocket.app.state.cp
    token = websocket.query_params.get("session_token", "")
    ip = websocket.client.host if websocket.client else "unknown"
    binding = cp.sessions.consume(token)         # single-use, VM-bound
    if binding is None:
        logger.warning("ws rejected ip=%s reason=invalid_session_token", ip)
        await websocket.close(code=4001)
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


# ---- health ----------------------------------------------------------------
@app.get("/health")
async def health(cp: ControlPlane = Depends(get_cp)) -> dict:
    return await cp.health()
