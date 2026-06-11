"""Resource tracking and quota math.

Host totals are read live from the provisioner (libvirt); allocations come from
the DB. Free = total - host reserve - allocated. A request is admissible only
if it fits BOTH the user's remaining quota AND the host's free pool. The
manager calls check_request() while holding its allocation lock, so the
check-then-reserve sequence is race-free.
"""
from __future__ import annotations

from db import Database
from config import settings
from provisioner import Provisioner


class QuotaError(ValueError):
    """Raised when a request exceeds sizing bounds, user quota, or host pool."""


class ResourceTracker:
    def __init__(self, db: Database, prov: Provisioner) -> None:
        self._db = db
        self._prov = prov

    async def _host_free(self) -> dict:
        cap = await self._prov.capacity()
        alloc = self._db.host_allocation()
        return {
            "vcpus": cap.vcpus_total - settings.RESERVE_VCPUS - alloc["vcpus"],
            "mem_mb": cap.mem_mb_total - settings.RESERVE_MEM_MB - alloc["mem_mb"],
            "disk_gb": cap.disk_gb_total - settings.RESERVE_DISK_GB - alloc["disk_gb"],
            "_cap": cap, "_alloc": alloc,
        }

    async def snapshot(self, user: dict) -> dict:
        free = await self._host_free()
        cap, alloc = free["_cap"], free["_alloc"]
        used = self._db.user_allocation(user["id"])
        return {
            "host": {
                "vcpus": {"total": cap.vcpus_total, "reserved": settings.RESERVE_VCPUS,
                          "allocated": alloc["vcpus"], "free": max(0, free["vcpus"])},
                "mem_mb": {"total": cap.mem_mb_total, "reserved": settings.RESERVE_MEM_MB,
                           "allocated": alloc["mem_mb"], "free": max(0, free["mem_mb"])},
                "disk_gb": {"total": cap.disk_gb_total, "reserved": settings.RESERVE_DISK_GB,
                            "allocated": alloc["disk_gb"], "free": max(0, free["disk_gb"])},
            },
            "you": {
                "max_vms": user["max_vms"], "used_vms": used["vms"],
                "vcpus": {"quota": user["max_vcpus"], "used": used["vcpus"]},
                "mem_mb": {"quota": user["max_mem_mb"], "used": used["mem_mb"]},
                "disk_gb": {"quota": user["max_disk_gb"], "used": used["disk_gb"]},
            },
            "bounds": {
                "vcpus": [settings.VM_MIN_VCPUS, settings.VM_MAX_VCPUS],
                "mem_mb": [settings.VM_MIN_MEM_MB, settings.VM_MAX_MEM_MB,
                           settings.VM_MEM_STEP_MB],
                "disk_gb": [settings.VM_MIN_DISK_GB, settings.VM_MAX_DISK_GB],
            },
        }

    async def check_request(self, user: dict, vcpus: int, mem_mb: int,
                            disk_gb: int) -> None:
        # 1. static sizing bounds
        if not (settings.VM_MIN_VCPUS <= vcpus <= settings.VM_MAX_VCPUS):
            raise QuotaError("vCPU count out of range")
        if not (settings.VM_MIN_MEM_MB <= mem_mb <= settings.VM_MAX_MEM_MB):
            raise QuotaError("memory out of range")
        if mem_mb % settings.VM_MEM_STEP_MB:
            raise QuotaError("memory must be a multiple of the step size")
        if not (settings.VM_MIN_DISK_GB <= disk_gb <= settings.VM_MAX_DISK_GB):
            raise QuotaError("disk out of range")

        # 2. user quota remaining
        used = self._db.user_allocation(user["id"])
        if used["vms"] >= user["max_vms"]:
            raise QuotaError("VM count quota reached")
        if used["vcpus"] + vcpus > user["max_vcpus"]:
            raise QuotaError("vCPU quota exceeded")
        if used["mem_mb"] + mem_mb > user["max_mem_mb"]:
            raise QuotaError("memory quota exceeded")
        if used["disk_gb"] + disk_gb > user["max_disk_gb"]:
            raise QuotaError("disk quota exceeded")

        # 3. host pool remaining
        free = await self._host_free()
        if vcpus > free["vcpus"]:
            raise QuotaError("not enough free vCPUs on the host")
        if mem_mb > free["mem_mb"]:
            raise QuotaError("not enough free memory on the host")
        if disk_gb > free["disk_gb"]:
            raise QuotaError("not enough free disk on the host")

    async def check_resume(self, vcpus: int, mem_mb: int) -> None:
        """Re-validate host RAM/CPU when a STOPPED VM is started again."""
        free = await self._host_free()
        if vcpus > free["vcpus"]:
            raise QuotaError("not enough free vCPUs on the host to start")
        if mem_mb > free["mem_mb"]:
            raise QuotaError("not enough free memory on the host to start")
