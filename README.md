# CloudKeep

**A self-service private cloud: build and use browser-based virtual desktops.**

CloudKeep lets authenticated users provision their own KVM virtual machines from
a web portal — choosing cores, memory, and disk within a per-user quota — and
connect to each VM's desktop in the browser over noVNC. A public AWS EC2 instance
is the internet-facing edge; the on-prem **KVM host** is the control plane *and*
the compute. The two are joined by a single persistent WireGuard tunnel, and
provisioned VMs live in a strictly isolated network — internet access, but no
path to the host, the LAN, the tunnel, or each other.

> **Status:** v2 in development. The v1 single-VM gateway is generalised into a
> fleet control plane (`cloudkeep-controld`) with a libvirt provisioning engine,
> SQLite-backed users + quotas, live resource tracking, and full VM lifecycle.
> A `FakeProvisioner` lets the entire control plane run without KVM for testing.

---

## Architecture

```
                         Internet (HTTPS / WSS :443)
                                    │
              ┌─────────────────────▼─────────────────────┐
              │  AWS EC2 — EDGE (static)                   │
              │  NGINX · TLS · static SPA                  │
              │  /ck/* ──► 10.10.10.2:8000                 │
              └─────────────────────┬─────────────────────┘
                                    │ WireGuard (one persistent tunnel)
       ┌────────────────────────────▼──────────────────────────────┐
       │  KVM HOST — CONTROL PLANE + COMPUTE                         │
       │                                                            │
       │  cloudkeep-controld (FastAPI :8000 on wg0)                 │
       │   ├─ auth (bcrypt → JWT → one-time VM-bound WS token)       │
       │   ├─ SQLite: users(+quotas) · vms · audit                  │
       │   ├─ resource tracker (live libvirt capacity − allocations)│
       │   ├─ provisioner (libvirt API; overlay-clone golden image) │
       │   └─ WS↔VNC bridge (per-VM target)                         │
       │                                                            │
       │  libvirt/QEMU · pool: golden-v1.qcow2 + per-VM overlays    │
       │  ckbr0 10.20.0.0/24 — isolated, NAT out, no lateral access │
       │   └─ vm-1, vm-2 …  (XFCE + in-guest TigerVNC :5901)         │
       └────────────────────────────────────────────────────────────┘
```

**Provisioning is a host-only operation.** Cloning an overlay, defining the
domain, and assigning a deterministic lease all happen on the KVM host — the EC2
edge is never reconfigured per VM. That keeps provisioning to seconds and the
edge permanently static.

### Request & data flow

```
POST /ck/auth/login        → JWT (held in browser memory only)
GET  /ck/resources         → host free pool + your quota/usage + sizing bounds
POST /ck/vms {cpu,ram,disk} → 202; controld validates (quota ∧ host free),
                              reserves, clones an overlay, boots the VM
GET  /ck/vms               → your VMs with live state badges
POST /ck/vms/{id}/session  → one-time WS token BOUND to that VM's VNC target
WSS  /ck/ws?session_token= → bridge consumes token → relays bytes to vm_ip:5901
```

A WS token is single-use, 30-second, and bound at mint time to one VM — it can
only ever open the bridge to the VM it was issued for, and only for its owner.

## Security model (defense in depth)

| Layer | Control | Stops |
|---|---|---|
| Edge | NGINX TLS, `/ck/auth/` rate limit, strict headers, 444 catch-all | Internet scanning, brute force |
| Tunnel | WireGuard host↔EC2; UFW admits `:8000` from the EC2 peer only | Reaching the control plane off-path |
| Identity | bcrypt + JWT + one-time **VM-bound** WS tokens; per-user quotas; ownership checks; audit log | VM creation without credentials; cross-user access |
| Control plane | controld unprivileged (libvirt group), systemd sandbox, **libvirt API only — no shell**, server-generated VM names | Injection, privilege escalation |
| Segmentation | nftables: guest→WAN allow, guest→all-RFC1918 drop, guest→host drop, guest↔guest isolated | A compromised guest pivoting to LAN/host/tunnel/tenants |
| Anti-spoof | libvirt `clean-traffic` nwfilter pins each vNIC to its MAC+IP | ARP/IP spoofing between guests |
| Guest | Golden image: UFW default-deny, 5901 from host only, auto-updates, sysprep'd identity | Internet-side guest compromise, lateral reuse |

Guests are treated as hostile (they're internet-connected): a fully compromised
VM can reach the internet and nothing else. The acceptance test in
`sys/HOSTSETUP.md` §9 makes this executable.

## Components

### Backend — `cloudkeep-controld` (`backend/src/`)

Service-oriented design: `ControlPlane` (in `app.py`) is the composition root —
it constructs the whole service graph and owns its lifecycle. There is no
module-level mutable state; route handlers receive the control plane through
FastAPI dependencies, so every service is swappable and testable in isolation
(which is exactly how the `FakeProvisioner` slots in).

| File | Responsibility |
|---|---|
| `app.py` | `ControlPlane` composition root + FastAPI routes (auth, resources, VM lifecycle, `/ws`, health) |
| `config.py` | Settings (libvirt, network, golden image, reserves, quotas, sizing bounds) |
| `db.py` | `Database` — SQLite (WAL): users/vms/pcie/audit + resource accounting |
| `auth_core.py` | `AuthService` (bcrypt + JWT) and the VM-bound one-time `SessionStore` |
| `resources.py` | `ResourceTracker` — live host capacity − reserve − allocations; quota admission |
| `provisioner.py` | `Provisioner` interface: `LibvirtProvisioner` (real KVM) / `FakeProvisioner` (testing) |
| `libvirt_xml.py` | injection-safe domain / overlay / DHCP-host XML builders |
| `vmmanager.py` | `VMManager` — lifecycle state machine, allocation lock, provisioning queue |
| `vnc_bridge.py` | `VNCBridge` — async TCP↔WebSocket byte relay (reused from v1, per-VM target) |
| `seed_user.py` | admin CLI to create/update users + quotas |
| `run.py` | uvicorn entrypoint (uvloop, WS deflate off) |

### Frontend (`frontend/`)

Single-page app, deliberately lightweight: no framework, no build step for the
app itself (only noVNC is pre-bundled), one small stylesheet of reusable
primitives. JWT lives in module memory only — never storage or URLs. All
dynamic DOM is built with `createElement`/`textContent`, so server data never
meets `innerHTML` (no XSS sink even via a hostile VM label).

Views: login → **dashboard** (always-visible quota/host-free strip, VM cards
with live state badges + Connect/Start/Stop/Delete, 3-second polling while
anything is building) → **builder** (cores/RAM/disk sliders capped to
`min(quota-left, host-free)`) → **viewer** (noVNC, full-bleed).

### System (`sys/`)

| Path | Purpose |
|---|---|
| `cloudkeep` | NGINX vhost (TLS, hardening, `/ck/` proxy → controld) |
| `HOSTSETUP.md` | command-level KVM host runbook (stack, WG, `ckbr0`, firewall, deploy) |
| `GOLDEN-IMAGE.md` | convert soccer-vision-vm → `golden-v1.qcow2` (sysprep, first-boot, in-guest VNC) |
| `systemd/cloudkeep-controld.service` | hardened host control-plane unit |
| `systemd/cloudkeep-vnc.service` | in-guest TigerVNC unit (baked into the golden image) |
| `vnc/` | golden-image session config (XFCE, geometry, perf tweaks) |

## Repository layout

```
backend/
  requirements.txt
  src/  app.py config.py db.py auth_core.py resources.py provisioner.py
        libvirt_xml.py vmmanager.py vnc_bridge.py seed_user.py run.py
frontend/
  index.html  app/index.html  assets/{app.js,style.css}
  lib/novnc (submodule)  lib/novnc.bundle.js  package.json
sys/
  cloudkeep  HOSTSETUP.md  GOLDEN-IMAGE.md
  systemd/{cloudkeep-controld.service,cloudkeep-vnc.service}
  vnc/{config,xstartup,tigervnc.conf}
```

## Getting started

> **Host OS:** CentOS Stream 10 (RHEL family) — the host runbook uses `dnf`,
> a single nftables ruleset (in place of firewalld), and accounts for SELinux
> (sVirt). The Golden Image VM guest is Ubuntu 24.04. The control-plane Python
> is OS-agnostic.

1. **Host:** follow `sys/HOSTSETUP.md` (stack, WireGuard, `ckbr0` + firewall, pool, controld).
2. **Golden image:** follow `sys/GOLDEN-IMAGE.md` to seal soccer-vision-vm into `golden-v1.qcow2`.
3. **Users:** `seed_user.py <name> [--admin] [--max-* …]`.
4. **Edge:** deploy `sys/cloudkeep` + the SPA to EC2; point its WireGuard peer key at the host.
5. Sign in, build a VM, connect.

> Control-plane only, no KVM? Set `LIBVIRT_URI=fake` to run against the
> in-memory provisioner — the full API, quotas, and lifecycle work without real VMs.

## Roadmap

- **PCIe passthrough (Phase D):** VFIO GPU + audio function (IOMMU pre-enabled in host setup).
- **Frontend polish:** dedicated visual pass on the builder/dashboard.
- **Scale-out:** externalize session/queue state (Redis) if the control plane ever needs multiple workers.
