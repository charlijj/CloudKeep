"""Libvirt XML builders — injection-safe by construction.

All callers pass typed, validated fields (server-generated names, integers,
regex-checked MAC/IP). Strings are XML-escaped defensively. No user-supplied
string ever reaches a shell or an unescaped XML node.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_NAME_RE = re.compile(r"^vm-\d+$")


def _check(mac: str, ip: str, name: str) -> None:
    if not _MAC_RE.match(mac):
        raise ValueError(f"bad mac: {mac!r}")
    if not _IP_RE.match(ip):
        raise ValueError(f"bad ip: {ip!r}")
    if not _NAME_RE.match(name):
        raise ValueError(f"bad domain name: {name!r}")


def domain_xml(*, name: str, vcpus: int, mem_mb: int, mac: str, ip: str,
               disk_path: str, network: str) -> str:
    """A UEFI q35 guest: virtio disk overlay + isolated virtio NIC pinned to
    its IP via the clean-traffic nwfilter (anti-spoof). A host-loopback VNC is
    attached as an emergency console only — the user desktop is in-guest
    TigerVNC reached at ip:5901."""
    _check(mac, ip, name)
    vcpus = int(vcpus)
    mem_mb = int(mem_mb)
    disk_path = escape(disk_path)
    network = escape(network)
    return f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{mem_mb}</memory>
  <currentMemory unit='MiB'>{mem_mb}</currentMemory>
  <vcpu placement='static'>{vcpus}</vcpu>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough' check='none' migratable='off'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'>
      <mac address='{mac}'/>
      <source network='{network}'/>
      <model type='virtio'/>
      <filterref filter='clean-traffic'>
        <parameter name='IP' value='{ip}'/>
      </filterref>
    </interface>
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>
    <video><model type='virtio'/></video>
    <console type='pty'/>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <memballoon model='virtio'/>
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
  </devices>
</domain>"""


def overlay_volume_xml(*, vol_name: str, disk_gb: int, golden_path: str) -> str:
    """A qcow2 copy-on-write overlay backed by the golden image. Capacity may
    exceed the backing size; the guest's first-boot growpart claims the rest."""
    disk_gb = int(disk_gb)
    vol_name = escape(vol_name)
    golden_path = escape(golden_path)
    return f"""<volume type='file'>
  <name>{vol_name}</name>
  <capacity unit='G'>{disk_gb}</capacity>
  <target><format type='qcow2'/></target>
  <backingStore>
    <path>{golden_path}</path>
    <format type='qcow2'/>
  </backingStore>
</volume>"""


def dhcp_host_xml(*, mac: str, ip: str, name: str) -> str:
    """An <host> entry for `virsh net-update <net> add ip-dhcp-host` so the VM
    always gets the same deterministic lease."""
    _check(mac, ip, name)
    return f"<host mac='{mac}' name='{escape(name)}' ip='{ip}'/>"
