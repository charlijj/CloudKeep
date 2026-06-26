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
       │   ├─ auth (bcrypt → JWT → 1-time VM+kind-bound token)      │
       │   ├─ SQLite: users(+quotas) · vms(+stage) · audit          │
       │   ├─ resource tracker (live libvirt capacity − allocations)│
       │   ├─ provisioner (libvirt API; overlay-clone golden image) │
       │   ├─ WS↔VNC bridge (per-VM target)                         │
       │   └─ serial-console streamer (boot-log → browser)          │
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
POST /ck/auth/login               → JWT (held in browser memory only)
GET  /ck/dashboard                → your VMs + host free pool + quota/usage (one round-trip)
GET  /ck/resources                → host free pool + your quota/usage + sizing bounds
POST /ck/vms {cpu,ram,disk}       → 202; controld validates (quota ∧ host free),
                                    reserves, clones an overlay, boots the VM
GET  /ck/vms                      → your VMs with live state + provisioning stage
POST /ck/vms/{id}/session         → one-time WS token BOUND to that VM's VNC target
WSS  /ck/ws?session_token=        → bridge consumes token → relays bytes to vm_ip:5901
POST /ck/vms/{id}/console-session → one-time WS token BOUND to that VM's serial console
WSS  /ck/console?session_token=   → streams the VM's live boot log to the browser
POST /ck/vms/{id}/start|stop      → lifecycle (re-validates host free on start)
DELETE /ck/vms/{id}               → tear down: destroy + undefine + reclaim overlay/lease
```

A WS token is single-use, 30-second, and bound at mint time to one VM **and one
kind** (`vnc` or `console`) — it can only ever open the surface it was issued
for, and only for its owner. A console token is rejected by the VNC bridge and
vice-versa, so the two streams can never be crossed.

## Security model (defense in depth)

| Layer | Control | Stops |
|---|---|---|
| Edge | NGINX TLS, `/ck/auth/` rate limit, strict headers, 444 catch-all | Internet scanning, brute force |
| Tunnel | WireGuard host↔EC2; UFW admits `:8000` from the EC2 peer only | Reaching the control plane off-path |
| Identity | bcrypt + JWT + one-time **VM+kind-bound** WS tokens (vnc/console can't be crossed); per-user quotas; ownership checks; audit log | VM creation without credentials; cross-user access |
| Control plane | controld unprivileged (libvirt group), systemd sandbox, **libvirt API only — no shell**, server-generated VM names | Injection, privilege escalation |
| Segmentation | nftables: guest→WAN allow, guest→all-RFC1918 drop, guest→host drop, guest↔guest isolated | A compromised guest pivoting to LAN/host/tunnel/tenants |
| Anti-spoof | libvirt `clean-traffic` nwfilter pins each vNIC to its MAC+IP | ARP/IP spoofing between guests |
| Guest | Golden image: UFW default-deny, 5901 from host only, no SSH, unprivileged desktop user (no sudo), sysprep'd identity, patched on golden rebuild | Internet-side guest compromise, lateral reuse |

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
| `app.py` | `ControlPlane` composition root + FastAPI routes (auth, dashboard/resources, VM lifecycle, delete, `/ws`, `/console`, health) |
| `config.py` | Settings (libvirt, network, golden image, reserves, quotas, sizing bounds, console stream caps) |
| `db.py` | `Database` — SQLite (WAL): users/vms(+`progress` stage)/pcie/audit + resource accounting |
| `states.py` | VM state constants + the `REQUESTED→PROVISIONING→RUNNING↔STOPPED→DELETING` machine |
| `auth_core.py` | `AuthService` (bcrypt + JWT) and the one-time `SessionStore` (tokens bound to one VM **and** one kind: `vnc`/`console`) |
| `resources.py` | `ResourceTracker` — live host capacity − reserve − allocations; quota admission |
| `provisioner.py` | `Provisioner` interface: `LibvirtProvisioner` (real KVM) / `FakeProvisioner` (testing) |
| `libvirt_xml.py` | injection-safe domain / overlay / DHCP-host XML builders |
| `vmmanager.py` | `VMManager` — lifecycle state machine, allocation lock, provisioning queue, stage reporting |
| `vnc_bridge.py` | `VNCBridge` — async TCP↔WebSocket byte relay (reused from v1, per-VM target) |
| `console.py` | live serial-console streamer — libvirt `openConsole` on a daemon thread → bounded queue → browser; cleans ANSI/CR, caps concurrent streams |
| `seed_user.py` | admin CLI to create/update users + quotas |
| `run.py` | uvicorn entrypoint (uvloop, WS deflate off) |

### Frontend (`frontend/`)

Single-page app, deliberately lightweight: no framework, no build step for the
app itself (only noVNC is pre-bundled), one small stylesheet of reusable
primitives (tokens → `btn`/`card`/`field`/`badge`/`meter` → views). JWT lives in
module memory only — never storage or URLs. All dynamic DOM is built with
`createElement`/`textContent` (via a tiny `h()` helper), so server data never
meets `innerHTML` (no XSS sink even via a hostile VM label).

Views: login → **dashboard** → **builder** → **viewer** (noVNC, full-bleed),
plus a **boot-log pop-up window**:

- **Dashboard** — resource **meters** (your used/quota + the host's
  allocated/reserved/free pool), VM cards with live status badges (a colour dot:
  green ready / amber building / red error) and Connect/Start/Stop/**Logs**/Delete,
  3-second polling while anything is building (cards also show the live
  provisioning *stage*, e.g. "cloning golden image…").
- **Builder** — cores/RAM/disk sliders + steppers capped to
  `min(quota-left, host-free)` from the same snapshot the server re-validates.
- **Boot-log pop-up** (`app/console.html` + `assets/console.js`) — clicking
  **Logs** opens a *separate* same-origin browser window streaming the VM's
  serial boot log, so it can be watched alongside the desktop. It holds **no
  JWT**: it asks the opener (`window.opener.__ckMintConsole`) to mint each
  single-use console token, preserving the in-memory-only credential model.

| File | Responsibility |
|---|---|
| `index.html` | landing page (pure-CSS `:target` modal; page CSP is `script-src 'none'`) |
| `app/index.html` | the SPA shell (login / dashboard / builder / viewer) |
| `app/console.html` + `assets/console.js` | the boot-log pop-up window |
| `assets/app.js` | the SPA: api wrapper, views, dashboard render (cards + meters), builder, viewer |
| `assets/style.css` | the single stylesheet (tokens + primitives + views), `prefers-reduced-motion` aware |
| `lib/novnc.bundle.js` | pre-bundled, version-pinned noVNC (the only build artifact) |

### System (`sys/`)

| Path | Purpose |
|---|---|
| `cloudkeep` | NGINX vhost (TLS, hardening, `/ck/` proxy → controld) |
| `HOSTSETUP.md` | command-level KVM host runbook (stack, WG, `ckbr0`, firewall, deploy) |
| `GOLDEN-IMAGE.md` | build a minimal Ubuntu 24.04 → `golden-v1.qcow2` (XFCE + TigerVNC, sysprep, first-boot, serial console) |
| `systemd/cloudkeep-controld.service` | hardened host control-plane unit |
| `systemd/cloudkeep-vnc.service` | in-guest TigerVNC unit (baked into the golden image) |
| `vnc/` | golden-image session config (XFCE, geometry, perf tweaks) |

## Repository layout

```
backend/
  requirements.txt  BACKEND-SETUP.md
  src/  app.py config.py db.py states.py auth_core.py resources.py provisioner.py
        libvirt_xml.py vmmanager.py vnc_bridge.py console.py seed_user.py run.py
frontend/
  index.html  EDGE-SETUP.md
  app/{index.html,console.html}  assets/{app.js,console.js,style.css}
  lib/novnc (submodule)  lib/novnc.bundle.js  package.json
sys/
  cloudkeep  HOSTSETUP.md  GOLDEN-IMAGE.md
  systemd/{cloudkeep-controld.service,cloudkeep-vnc.service}
  vnc/{config,xstartup,tigervnc.conf}
```

Setup runbooks live next to what they bring up: `sys/HOSTSETUP.md` (KVM host),
`sys/GOLDEN-IMAGE.md` (the template VM), `backend/BACKEND-SETUP.md` (controld),
`frontend/EDGE-SETUP.md` (the AWS edge).

## Getting started

> **Host OS:** CentOS Stream 10 (RHEL family) — the host runbook uses `dnf`,
> a single nftables ruleset (in place of firewalld), and accounts for SELinux
> (sVirt). The Golden Image VM guest is Ubuntu 24.04. The control-plane Python
> is OS-agnostic.

1. **Host:** follow `sys/HOSTSETUP.md` (stack, WireGuard, `ckbr0` + firewall, pool, controld).
2. **Golden image:** follow `sys/GOLDEN-IMAGE.md` to build + seal a minimal Ubuntu 24.04 into `golden-v1.qcow2`.
3. **Users:** `seed_user.py <name> [--admin] [--max-* …]`.
4. **Edge:** deploy `sys/cloudkeep` + the SPA to EC2; point its WireGuard peer key at the host.
5. Sign in, build a VM, connect.

> Control-plane only, no KVM? Set `LIBVIRT_URI=fake` to run against the
> in-memory provisioner — the full API, quotas, and lifecycle work without real VMs.

## Development & extending

The control plane is built to be worked on without ever touching real hardware,
and to make the common extensions obvious.

- **Run the whole stack without KVM.** Set `LIBVIRT_URI=fake` in `.env` — the
  `FakeProvisioner` (in `provisioner.py`) gives you the full API, auth, quotas,
  lifecycle, and even a synthetic boot log, no libvirt required. This is the
  fastest dev loop and how the logic is exercised in isolation.
- **Adding a service.** `ControlPlane` in `app.py` is the composition root: it
  constructs every service once and hands it to route handlers via FastAPI
  `Depends(get_cp)`. There is no module-level mutable state — add your service to
  `ControlPlane.__init__`, inject it the same way, and it's swappable/testable.
- **Adding an endpoint.** Define the route in `app.py` and raise the domain
  exceptions (`NotFound`/`Forbidden`/`Conflict`/`QuotaError`) — they're mapped to
  HTTP codes by registered handlers, so handlers stay thin. **Gotcha:** any new
  HTTP *method* (e.g. `PUT`) must also be added to the edge nginx method
  allowlist in `sys/cloudkeep` (`if ($request_method !~ …)`), or the edge returns
  405 before the request ever reaches controld.
- **Swapping the compute backend.** Implement the `Provisioner` interface
  (both `LibvirtProvisioner` and `FakeProvisioner` satisfy it) to target
  something other than libvirt/KVM.
- **Frontend conventions.** Build DOM with the `h()` helper + `textContent`
  (never `innerHTML`); keep the JWT in module memory; route authed calls through
  the `api()` wrapper (it centralises 401 → session-expired); and reuse the CSS
  tokens/primitives in `style.css` rather than adding new ones. New WS surfaces
  must also be allowed by the `/app/` CSP `connect-src` in `sys/cloudkeep`.
- **After a change, what to redeploy:** controld code → `systemctl restart
  cloudkeep-controld`; the edge vhost → `cp sys/cloudkeep …` + `nginx -t &&
  systemctl reload nginx`; the SPA/static files → `rsync … frontend/
  /var/www/html/cloudkeep/` (the `/assets/` 5-min cache means edits show on a
  hard refresh). DB schema changes auto-migrate on controld startup.

## Roadmap

- **PCIe passthrough (Phase D):** VFIO GPU + audio function (IOMMU pre-enabled in host setup).
- **Scale-out:** externalize session/queue state (Redis) if the control plane ever needs multiple workers.

Recently delivered: live serial boot-log console (in-portal pop-up window),
provisioning **stage** reporting on the cards, dashboard **resource meters**, the
VM **delete** lifecycle end-to-end, and a UI / accessibility polish pass
(status dots, focus rings, reduced-motion).
