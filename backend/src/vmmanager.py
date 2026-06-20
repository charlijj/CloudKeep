"""VM lifecycle orchestration — policy layer.

Ties together the DB, the resource tracker, the provisioner, and the WS session
store. Provisioning runs on a single background worker (concurrency 1: cloning
is disk-bound and serialising avoids I/O storms and races). All resource
admission happens under one asyncio lock so two requests can't claim the same
capacity.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from auth_core import Binding, SessionStore
from config import settings
from db import Database
from provisioner import Provisioner
from resources import ResourceTracker, QuotaError
from states import DELETING, ERROR, PROVISIONING, REQUESTED, RUNNING, STOPPED

logger = logging.getLogger("cloudkeep.manager")


class NotFound(Exception): ...
class Forbidden(Exception): ...
class Conflict(Exception): ...


class VMManager:
    def __init__(self, db: Database, prov: Provisioner,
                 tracker: ResourceTracker, sessions: SessionStore) -> None:
        self._db = db
        self._prov = prov
        self._tracker = tracker
        self._sessions = sessions
        self._alloc_lock = asyncio.Lock()       # guards check-then-reserve
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    # -- worker lifecycle --------------------------------------------------
    async def start(self) -> None:
        self._worker = asyncio.create_task(self._run())
        await self._reconcile()

    async def _reconcile(self) -> None:
        """Sync DB state with libvirt reality on startup.

        A VM left mid-provision by a restart is unreachable -> ERROR. A RUNNING
        VM whose domain is gone (e.g. host reboot) -> STOPPED, so the UI doesn't
        lie and the user can start it again; a STOPPED VM that is somehow active
        -> RUNNING.
        """
        for vm in self._db.list_vms():
            st = vm["state"]
            if st in (REQUESTED, PROVISIONING):
                self._db.set_state(vm["id"], ERROR, "interrupted by restart")
                continue
            if st in (RUNNING, STOPPED):
                try:
                    active = await self._prov.is_active(vm["name"])
                except Exception:
                    continue                 # libvirt unreachable -> leave as-is
                if st == RUNNING and not active:
                    self._db.set_state(vm["id"], STOPPED)
                elif st == STOPPED and active:
                    self._db.set_state(vm["id"], RUNNING)

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()

    async def _run(self) -> None:
        while True:
            vm_id = await self._queue.get()
            try:
                await self._provision(vm_id)
            except Exception:                    # never let the worker die
                logger.exception("provision worker error vm=%s", vm_id)
            finally:
                self._queue.task_done()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _is_admin(user: dict) -> bool:
        return user.get("role") == "admin"

    def _require(self, user: dict, vm_id: int) -> dict:
        vm = self._db.get_vm(vm_id)
        if vm is None:
            raise NotFound("no such VM")
        if vm["owner_id"] != user["id"] and not self._is_admin(user):
            raise Forbidden("not your VM")
        return vm

    async def _wait_off(self, name: str, timeout: int) -> bool:
        """Poll until the domain is powered off, or timeout. True if it stopped."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if not await self._prov.is_active(name):
                return True
            await asyncio.sleep(2)
        return False

    def _alloc_mac_ip(self) -> tuple[str, str]:
        # Stable MAC derived from the IP. Reusing an address across rebuilds
        # then reuses the same MAC, so the host's ARP/neighbour cache and
        # dnsmasq's lease for that IP stay consistent. A fresh *random* MAC on a
        # recycled IP leaves a stale neighbour entry on the host (it dials the
        # old MAC), so the VNC connect intermittently fails with "VNC did not
        # come up in time". The IP is unique among live VMs, so the MAC is too.
        used_ips = self._db.used_ips()
        for host in range(settings.GUEST_IP_START, settings.GUEST_IP_END + 1):
            ip = f"{settings.GUEST_NET_PREFIX}.{host}"
            if ip not in used_ips:
                o = [int(x) for x in ip.split(".")]
                mac = "52:54:00:%02x:%02x:%02x" % (o[1], o[2], o[3])
                return mac, ip
        raise Conflict("no free guest IP addresses")

    # -- public API --------------------------------------------------------
    def list_vms(self, user: dict) -> list[dict]:
        is_admin = self._is_admin(user)
        rows = self._db.list_vms(None if is_admin else user["id"])
        return [_public(v, is_admin) for v in rows]

    def get_vm(self, user: dict, vm_id: int) -> dict:
        return _public(self._require(user, vm_id), self._is_admin(user))

    async def request_vm(self, user: dict, label: str, vcpus: int,
                         mem_mb: int, disk_gb: int) -> dict:
        async with self._alloc_lock:
            await self._tracker.check_request(user, vcpus, mem_mb, disk_gb)
            mac, ip = self._alloc_mac_ip()
            vm_id = self._db.create_vm(user["id"], label, vcpus, mem_mb,
                                       disk_gb, mac, ip, settings.GUEST_VNC_PORT)
        self._db.audit(user["id"], "vm.request",
                       f"vm-{vm_id} {vcpus}c/{mem_mb}M/{disk_gb}G")
        await self._queue.put(vm_id)
        return _public(self._db.get_vm(vm_id), self._is_admin(user))

    async def _provision(self, vm_id: int) -> None:
        vm = self._db.get_vm(vm_id)
        if vm is None or vm["state"] != REQUESTED:
            return
        self._db.set_state(vm_id, PROVISIONING)
        try:
            await self._prov.create(name=vm["name"], vcpus=vm["vcpus"],
                                    mem_mb=vm["mem_mb"], disk_gb=vm["disk_gb"],
                                    mac=vm["mac"], ip=vm["ip_addr"])
            ready = await self._prov.wait_vnc(vm["ip_addr"], vm["vnc_port"],
                                              settings.PROVISION_TIMEOUT_S)
            if not ready:
                raise TimeoutError("VNC did not come up in time")
            self._db.set_state(vm_id, RUNNING)
            self._db.audit(vm["owner_id"], "vm.running", vm["name"])
            logger.info("provisioned %s -> %s:%s", vm["name"],
                        vm["ip_addr"], vm["vnc_port"])
        except Exception as exc:
            logger.exception("provision failed %s", vm["name"])
            with contextlib.suppress(Exception):
                await self._prov.destroy(name=vm["name"], mac=vm["mac"],
                                         ip=vm["ip_addr"])
            self._db.set_state(vm_id, ERROR, str(exc)[:200])
            self._db.audit(vm["owner_id"], "vm.error", f"{vm['name']}: {exc}")

    async def start_vm(self, user: dict, vm_id: int) -> dict:
        vm = self._require(user, vm_id)
        if vm["state"] != STOPPED:
            raise Conflict(f"cannot start a VM in state {vm['state']}")
        async with self._alloc_lock:
            await self._tracker.check_resume(vm["vcpus"], vm["mem_mb"])
            self._db.set_state(vm_id, RUNNING)
        try:
            await self._prov.start(vm["name"])
        except Exception as exc:
            self._db.set_state(vm_id, ERROR, str(exc)[:200])
            raise Conflict(f"start failed: {exc}")
        self._db.audit(user["id"], "vm.start", vm["name"])
        return _public(self._db.get_vm(vm_id), self._is_admin(user))

    async def stop_vm(self, user: dict, vm_id: int) -> dict:
        vm = self._require(user, vm_id)
        if vm["state"] != RUNNING:
            raise Conflict(f"cannot stop a VM in state {vm['state']}")
        await self._prov.stop(vm["name"])               # graceful ACPI
        # Confirm power-off before freeing RAM/CPU in the accounting; force if
        # the guest ignored ACPI, else the pool would treat the RAM as free
        # while the VM is still running.
        if not await self._wait_off(vm["name"], settings.STOP_TIMEOUT_S):
            await self._prov.force_off(vm["name"])
        self._db.set_state(vm_id, STOPPED)
        self._db.audit(user["id"], "vm.stop", vm["name"])
        return _public(self._db.get_vm(vm_id), self._is_admin(user))

    async def delete_vm(self, user: dict, vm_id: int) -> None:
        vm = self._require(user, vm_id)
        # Block delete mid-build: the worker is still creating the domain/overlay
        # and would race the teardown. ERROR/RUNNING/STOPPED are all deletable.
        if vm["state"] in (REQUESTED, PROVISIONING):
            raise Conflict("cannot delete a VM while it is being built")
        self._db.set_state(vm_id, DELETING)
        await self._prov.destroy(name=vm["name"], mac=vm["mac"], ip=vm["ip_addr"])
        self._db.delete_vm(vm_id)
        self._db.audit(user["id"], "vm.delete", vm["name"])

    def mint_session(self, user: dict, vm_id: int) -> str:
        vm = self._require(user, vm_id)
        if vm["state"] != RUNNING:
            raise Conflict("VM is not running")
        return self._sessions.issue(Binding(
            username=user["username"], vm_id=vm_id,
            host=vm["ip_addr"], port=vm["vnc_port"]))


def _public(vm: dict, is_admin: bool = False) -> dict:
    """VM fields safe to return to the browser (no MAC/internal IP).

    Non-admins get a generic error message so provisioning internals (host
    paths, libvirt detail) aren't disclosed; the full message stays in the logs
    and the audit table for operators.
    """
    d = {k: vm[k] for k in
         ("id", "label", "vcpus", "mem_mb", "disk_gb", "state",
          "error_msg", "created_at")}
    if d["error_msg"] and not is_admin:
        d["error_msg"] = "Provisioning failed — contact an administrator."
    return d
