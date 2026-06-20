"""Provisioning mechanism — the libvirt-facing layer.

Two interchangeable backends behind one interface:
  * LibvirtProvisioner — real KVM via the libvirt API (no shelling out: overlay
    creation, define, lifecycle and teardown all go through libvirtd).
  * FakeProvisioner   — in-memory simulation so the control plane (API, DB,
    quotas, state machine) can be exercised with LIBVIRT_URI=fake.

Policy (quotas, accounting, state transitions) lives in vmmanager; this layer
only performs mechanical actions and reports capacity.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import libvirt_xml
from config import settings

logger = logging.getLogger("cloudkeep.provisioner")


@dataclass
class Capacity:
    vcpus_total: int
    mem_mb_total: int
    disk_gb_total: int
    disk_gb_free: int


class ProvisionError(RuntimeError):
    pass


class Provisioner:
    async def capacity(self) -> Capacity: ...
    async def create(self, *, name: str, vcpus: int, mem_mb: int, disk_gb: int,
                     mac: str, ip: str) -> None: ...
    async def start(self, name: str) -> None: ...
    async def stop(self, name: str) -> None: ...           # graceful ACPI
    async def force_off(self, name: str) -> None: ...      # hard power-off
    async def is_active(self, name: str) -> bool: ...      # is the domain running?
    async def destroy(self, *, name: str, mac: str, ip: str) -> None: ...

    async def wait_vnc(self, host: str, port: int, timeout: int) -> bool:
        """Poll the in-guest VNC port until it answers or we time out.

        This is how the state machine knows a clone actually reached a usable
        desktop (TigerVNC up) rather than merely booting the kernel.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=2)
                w.close()
                with contextlib.suppress(Exception):
                    await w.wait_closed()
                return True
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(2)
        return False


# --------------------------------------------------------------------------
class LibvirtProvisioner(Provisioner):
    def __init__(self) -> None:
        import libvirt  # imported lazily so fake mode needs no libvirt
        self._libvirt = libvirt
        self._conn = libvirt.open(settings.LIBVIRT_URI)
        if self._conn is None:
            raise ProvisionError(f"cannot open libvirt {settings.LIBVIRT_URI}")
        logger.info("libvirt connected: %s", settings.LIBVIRT_URI)

    # libvirt calls are blocking C calls -> run them off the event loop.
    async def _run(self, fn, *a):
        return await asyncio.to_thread(fn, *a)

    async def capacity(self) -> Capacity:
        def _cap():
            info = self._conn.getInfo()          # [model, MB, cpus, ...]
            pool = self._conn.storagePoolLookupByName(settings.STORAGE_POOL)
            _, cap, _alloc, avail = pool.info()  # bytes
            return Capacity(
                vcpus_total=int(info[2]),
                mem_mb_total=int(info[1]),
                disk_gb_total=int(cap // (1024 ** 3)),
                disk_gb_free=int(avail // (1024 ** 3)),
            )
        return await self._run(_cap)

    async def create(self, *, name, vcpus, mem_mb, disk_gb, mac, ip) -> None:
        def _create():
            lv = self._libvirt
            pool = self._conn.storagePoolLookupByName(settings.STORAGE_POOL)
            vol_name = f"{name}-os.qcow2"
            pool.createXML(libvirt_xml.overlay_volume_xml(
                vol_name=vol_name, disk_gb=disk_gb,
                golden_path=settings.GOLDEN_IMAGE), 0)
            disk_path = pool.storageVolLookupByName(vol_name).path()

            net = self._conn.networkLookupByName(settings.LIBVIRT_NETWORK)
            net.update(
                lv.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST,
                lv.VIR_NETWORK_SECTION_IP_DHCP_HOST, -1,
                libvirt_xml.dhcp_host_xml(mac=mac, ip=ip, name=name),
                lv.VIR_NETWORK_UPDATE_AFFECT_LIVE
                | lv.VIR_NETWORK_UPDATE_AFFECT_CONFIG)

            dom = self._conn.defineXML(libvirt_xml.domain_xml(
                name=name, vcpus=vcpus, mem_mb=mem_mb, mac=mac, ip=ip,
                disk_path=disk_path, network=settings.LIBVIRT_NETWORK))
            dom.create()
        await self._run(_create)

    async def start(self, name) -> None:
        def _start():
            dom = self._conn.lookupByName(name)
            if not dom.isActive():
                dom.create()
        await self._run(_start)

    async def stop(self, name) -> None:
        def _stop():
            dom = self._conn.lookupByName(name)
            if dom.isActive():
                dom.shutdown()      # graceful ACPI; manager forces after timeout
        await self._run(_stop)

    async def force_off(self, name) -> None:
        def _off():
            dom = self._conn.lookupByName(name)
            if dom.isActive():
                dom.destroy()
        await self._run(_off)

    async def is_active(self, name) -> bool:
        def _act():
            try:
                return bool(self._conn.lookupByName(name).isActive())
            except self._libvirt.libvirtError:
                return False    # domain gone -> not active
        return await self._run(_act)

    async def destroy(self, *, name, mac, ip) -> None:
        def _destroy():
            lv = self._libvirt
            try:
                dom = self._conn.lookupByName(name)
                if dom.isActive():
                    dom.destroy()
                try:
                    dom.undefineFlags(lv.VIR_DOMAIN_UNDEFINE_NVRAM)
                except Exception:
                    dom.undefine()
            except lv.libvirtError:
                pass  # already gone — idempotent teardown
            # overlay
            try:
                pool = self._conn.storagePoolLookupByName(settings.STORAGE_POOL)
                pool.storageVolLookupByName(f"{name}-os.qcow2").delete(0)
            except lv.libvirtError:
                pass
            # dhcp reservation
            try:
                net = self._conn.networkLookupByName(settings.LIBVIRT_NETWORK)
                net.update(
                    lv.VIR_NETWORK_UPDATE_COMMAND_DELETE,
                    lv.VIR_NETWORK_SECTION_IP_DHCP_HOST, -1,
                    libvirt_xml.dhcp_host_xml(mac=mac, ip=ip, name=name),
                    lv.VIR_NETWORK_UPDATE_AFFECT_LIVE
                    | lv.VIR_NETWORK_UPDATE_AFFECT_CONFIG)
            except lv.libvirtError:
                pass
        await self._run(_destroy)


# --------------------------------------------------------------------------
class FakeProvisioner(Provisioner):
    """No libvirt. Simulates timing so the control plane can be tested."""

    def __init__(self) -> None:
        self._domains: dict[str, str] = {}
        logger.warning("FakeProvisioner active (LIBVIRT_URI=fake) — no real VMs")

    async def capacity(self) -> Capacity:
        return Capacity(vcpus_total=16, mem_mb_total=65536,
                        disk_gb_total=1000, disk_gb_free=1000)

    async def create(self, *, name, vcpus, mem_mb, disk_gb, mac, ip) -> None:
        await asyncio.sleep(2)
        self._domains[name] = "running"

    async def start(self, name) -> None:
        await asyncio.sleep(0.5)
        self._domains[name] = "running"

    async def stop(self, name) -> None:
        await asyncio.sleep(0.5)
        self._domains[name] = "stopped"

    async def force_off(self, name) -> None:
        await asyncio.sleep(0.1)
        self._domains[name] = "stopped"

    async def is_active(self, name) -> bool:
        return self._domains.get(name) == "running"

    async def destroy(self, *, name, mac, ip) -> None:
        await asyncio.sleep(0.5)
        self._domains.pop(name, None)

    async def wait_vnc(self, host, port, timeout) -> bool:
        return True  # pretend the desktop came up


def build_provisioner() -> Provisioner:
    """Factory: the one place that decides real KVM vs the test double."""
    return FakeProvisioner() if settings.use_fake else LibvirtProvisioner()
