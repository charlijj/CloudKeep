"""cloudkeep-controld — FastAPI control plane (runs on the KVM host).

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

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import auth_core
from auth_core import SessionStore
from config import settings
from db import Database
from provisioner import build_provisioner
from resources import ResourceTracker, QuotaError
from vmmanager import VMManager, NotFound, Forbidden, Conflict
from vnc_bridge import VNCBridge

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("cloudkeep")

_START = time.monotonic()

# Module-level singletons (single-worker control plane), wired in lifespan.
db: Database
sessions: SessionStore
tracker: ResourceTracker
manager: VMManager


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else get_remote_address(request)


limiter = Limiter(key_func=_client_ip)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, sessions, tracker, manager
    db = Database(settings.DB_PATH)
    sessions = SessionStore()
    prov = build_provisioner()
    tracker = ResourceTracker(db, prov)
    manager = VMManager(db, prov, tracker, sessions)
    await manager.start()
    logger.info("controld up: libvirt=%s db=%s", settings.LIBVIRT_URI, settings.DB_PATH)
    yield
    await manager.stop()


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


# ---- auth dependency -----------------------------------------------------
async def current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    username = auth_core.verify_jwt(token) if token else None
    user = db.get_user(username) if username else None
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


# ---- request models ------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class CreateVMRequest(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    vcpus: int
    mem_mb: int
    disk_gb: int


# ---- auth ----------------------------------------------------------------
@app.post("/auth/login")
@limiter.limit(settings.AUTH_RATE_LIMIT)
async def login(request: Request, body: LoginRequest) -> JSONResponse:
    user = db.get_user(body.username)
    ok = auth_core.verify_password(body.password, user["pw_hash"] if user else None)
    if not ok or not user:
        logger.info("login failed user=%s ip=%s", body.username, _client_ip(request))
        return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})
    logger.info("login ok user=%s ip=%s", body.username, _client_ip(request))
    return JSONResponse({
        "access_token": auth_core.make_jwt(body.username),
        "token_type": "bearer",
        "expires_in": settings.JWT_EXPIRY_MINUTES * 60,
    })


# ---- resources -----------------------------------------------------------
@app.get("/resources")
async def get_resources(user: dict = Depends(current_user)) -> dict:
    return await tracker.snapshot(user)


# ---- VM lifecycle --------------------------------------------------------
@app.get("/vms")
async def list_vms(user: dict = Depends(current_user)) -> dict:
    return {"vms": manager.list_vms(user)}


@app.post("/vms")
@limiter.limit(settings.PROVISION_RATE_LIMIT)
async def create_vm(request: Request, body: CreateVMRequest,
                    user: dict = Depends(current_user)) -> JSONResponse:
    try:
        vm = await manager.request_vm(user, body.label, body.vcpus,
                                      body.mem_mb, body.disk_gb)
    except QuotaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return JSONResponse(status_code=202, content=vm)


@app.get("/vms/{vm_id}")
async def get_vm(vm_id: int, user: dict = Depends(current_user)) -> dict:
    try:
        return manager.get_vm(user, vm_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such VM")
    except Forbidden:
        raise HTTPException(status_code=403, detail="Not your VM")


@app.post("/vms/{vm_id}/start")
async def start_vm(vm_id: int, user: dict = Depends(current_user)) -> dict:
    return await _lifecycle(manager.start_vm, user, vm_id)


@app.post("/vms/{vm_id}/stop")
async def stop_vm(vm_id: int, user: dict = Depends(current_user)) -> dict:
    return await _lifecycle(manager.stop_vm, user, vm_id)


@app.delete("/vms/{vm_id}")
async def delete_vm(vm_id: int, user: dict = Depends(current_user)) -> JSONResponse:
    try:
        await manager.delete_vm(user, vm_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such VM")
    except Forbidden:
        raise HTTPException(status_code=403, detail="Not your VM")
    return JSONResponse(status_code=204, content=None)


@app.post("/vms/{vm_id}/session")
async def create_vm_session(vm_id: int, user: dict = Depends(current_user)) -> dict:
    try:
        token = manager.mint_session(user, vm_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such VM")
    except Forbidden:
        raise HTTPException(status_code=403, detail="Not your VM")
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"session_token": token, "expires_in": settings.WS_TOKEN_EXPIRY_SECONDS}


async def _lifecycle(fn, user: dict, vm_id: int) -> dict:
    try:
        return await fn(user, vm_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="No such VM")
    except Forbidden:
        raise HTTPException(status_code=403, detail="Not your VM")
    except (Conflict, QuotaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---- VNC bridge ----------------------------------------------------------
@app.websocket("/ws")
async def ws_vnc(websocket: WebSocket) -> None:
    token = websocket.query_params.get("session_token", "")
    ip = websocket.client.host if websocket.client else "unknown"
    binding = sessions.consume(token)            # single-use, VM-bound
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


# ---- health --------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    libvirt_ok, disk_free = True, None
    try:
        cap = await tracker._prov.capacity()
        disk_free = cap.disk_gb_free
    except Exception:
        libvirt_ok = False
    return {
        "status": "ok" if libvirt_ok else "degraded",
        "libvirt_ok": libvirt_ok,
        "pool_free_gb": disk_free,
        "uptime_seconds": int(time.monotonic() - _START),
    }
