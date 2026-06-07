# CloudKeep

**A hybrid-cloud, browser-based remote-desktop gateway.**

CloudKeep fronts a self-hosted KVM/XFCE desktop with a hardened, internet-facing
gateway. An on-prem KVM virtual machine provides the private compute; a public AWS
EC2 instance provides the internet-facing edge. The two are joined by an encrypted
**WireGuard** tunnel, so the private VM never exposes a port to the public internet —
the only door in is the EC2 edge.

The user experience: open `https://cloudkeep.duckdns.org`, sign in, pick a desktop,
and a live XFCE session renders in the browser over [noVNC](https://github.com/novnc/noVNC).
No native client, no VNC port open to the world.

> **Status:** Working proof-of-concept. Remote desktop over the internet is
> functional end-to-end (browser → EC2 → WireGuard → KVM VM → VNC). The codebase is
> structured for a clean v2 (DB-backed users, multi-VM dashboard, horizontally
> scalable session state).

---

## Table of contents

- [Architecture overview](#architecture-overview)
- [Physical topology](#physical-topology)
- [Request & data flow](#request--data-flow)
- [Why two-step authentication](#why-two-step-authentication)
- [Components](#components)
  - [Backend — FastAPI gateway](#backend--fastapi-gateway)
  - [Frontend — single-page client](#frontend--single-page-client)
  - [System configuration](#system-configuration)
- [Security model](#security-model)
- [Deployment](#deployment)
- [Configuration reference](#configuration-reference)
- [Repository layout](#repository-layout)
- [Roadmap (v2)](#roadmap-v2)

---

## Architecture overview

CloudKeep is split into three clean planes:

| Plane | Responsibility | Transport |
| --- | --- | --- |
| **Presentation** | Static single-page app (login, dashboard, viewer) | HTTPS, served by NGINX on the edge |
| **Control** | Authentication, JWT issuance, one-time session tokens | HTTPS / JSON → FastAPI gateway |
| **Data** | Raw VNC byte relay between browser and desktop | WebSocket (WSS) ↔ TCP |

The guiding invariant is **defense by topology**: the private VM binds its services to
loopback and to the WireGuard interface only. Authentication happens at the gateway;
the VNC server itself trusts its caller because nothing but the local bridge can reach
it.

---

## Physical topology

Two hosts, one tunnel.

```
                       Internet (HTTPS / WSS, :443)
                                  │
                                  ▼
        ┌────────────────────────────────────────────────┐
        │  AWS EC2  —  PUBLIC EDGE                         │
        │  WireGuard addr: 10.10.10.1                      │
        │                                                 │
        │  • NGINX  — TLS termination (Certbot / LE)      │
        │  • Static SPA  /var/www/html/cloudkeep          │
        │  • Reverse proxy + request hardening            │
        │  • DNS: cloudkeep.duckdns.org (DuckDNS)          │
        └───────────────────────┬─────────────────────────┘
                                │
                                │  WireGuard tunnel (encrypted)
                                │  proxy_pass ──► 10.10.10.2
                                ▼
        ┌────────────────────────────────────────────────┐
        │  Local KVM VM  —  PRIVATE COMPUTE               │
        │  WireGuard addr: 10.10.10.2                      │
        │                                                 │
        │  • FastAPI gateway   10.10.10.2:8000  (WG iface)│
        │  • TigerVNC server   127.0.0.1:5901   (loopback)│
        │  • XFCE desktop      1280×800                    │
        │  • UFW: :8000 reachable ONLY from EC2 peer       │
        └────────────────────────────────────────────────┘
```

| Property | Value |
| --- | --- |
| Public hostname | `cloudkeep.duckdns.org` (DuckDNS → EC2) |
| WireGuard subnet | `10.10.10.0/24` |
| EC2 WireGuard address | `10.10.10.1` |
| KVM VM WireGuard address | `10.10.10.2` |
| Gateway listen address | `10.10.10.2:8000` (WireGuard interface only — never `0.0.0.0`) |
| VNC listen address | `127.0.0.1:5901` (loopback only) |

**Key invariant:** nothing on the VM listens on a public interface. FastAPI binds the
WireGuard address; VNC binds loopback. The only ingress to the private side is
NGINX-over-WireGuard, and UFW enforces that at the packet level (port 8000 accepts the
EC2 WireGuard peer and nothing else).

---

## Request & data flow

```
1.  GET  https://cloudkeep.duckdns.org/app/
         └─► EC2 NGINX serves the SPA (index.html, app.js, style.css, noVNC lib)

2.  POST /ck/auth/login            { username, password }
         └─► NGINX ──► 10.10.10.2:8000/auth/login
             └─► bcrypt verify ──► returns JWT (HS256, 60-min expiry)
                 (JWT is held in browser JS memory only — never persisted)

3.  POST /ck/auth/session          Authorization: Bearer <JWT>
         └─► verify JWT ──► issue ONE-TIME session_token (30-second TTL)

4.  WSS  /ck/ws?session_token=<tok>
         └─► NGINX upgrades ──► 10.10.10.2:8000/ws
             └─► SessionStore.consume(tok)  (single-use, popped atomically)
                 └─► VNCBridge opens TCP 127.0.0.1:5901
                     └─► raw RFB bytes relayed bidirectionally

5.  Browser noVNC RFB.js speaks the RFB protocol over the WebSocket
         └─► framebuffer painted to <canvas>; mouse + keyboard forwarded back
```

Every public path is prefixed `/ck/` at the edge and rewritten to the gateway's
unprefixed routes by NGINX.

---

## Why two-step authentication

Browsers **cannot set an `Authorization` header on a WebSocket handshake**. Rather than
embed a long-lived credential in the WebSocket URL, CloudKeep uses a two-step exchange:

1. `POST /ck/auth/login` → a **JWT** (60-minute lifetime), kept only in JS module memory.
2. `POST /ck/auth/session` (Bearer JWT) → a **single-use, 30-second session token**
   minted specifically for the upgrade.
3. `WSS /ck/ws?session_token=…` → the token is validated and **atomically consumed**
   (popped from the store) on connect, so it can never be replayed.

The short-lived single-use token is acceptable to place in the URL precisely because it
is single-use and expires in 30 seconds. The durable credential (the JWT) is never put
on the wire in a URL and never persisted to `localStorage`, `sessionStorage`, or
`window`.

---

## Components

### Backend — FastAPI gateway

Location: `backend/src/`

| File | Responsibility |
| --- | --- |
| `auth.py` | The gateway app — HTTP/JSON endpoints, JWT, session store, WebSocket route |
| `config.py` | Centralized settings (pydantic-settings) + the POC user store |
| `vnc_bridge.py` | Pure async TCP ↔ WebSocket byte relay |

#### `auth.py` — endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness + live VNC reachability probe + uptime |
| `POST` | `/auth/login` | Validate credentials, issue a JWT (rate-limited) |
| `POST` | `/auth/session` | Exchange a valid JWT for a one-time WS session token |
| `WS` | `/ws` | Validate the session token, then run the VNC bridge |

Security-conscious details worth calling out:

- **Constant-time login.** Unknown usernames are still verified against a fixed dummy
  bcrypt hash, so "no such user" and "wrong password" take the same time. This removes
  username enumeration via response timing.
- **bcrypt 72-byte truncation** is handled explicitly, so an over-long password can
  never raise and 500 the endpoint.
- **`SessionStore`** is an in-memory dict of `token → expiry`. Tokens are popped on use
  (single-use, atomic within the single-threaded event loop) and expired tokens are
  swept on each issue. **This makes the gateway single-worker by design** (see
  [Roadmap](#roadmap-v2)).
- **Rate limiting** (SlowAPI) is keyed on the *real* client IP, taken from
  `X-Forwarded-For`. This is trustworthy here because UFW guarantees the only ingress is
  NGINX over WireGuard, so the header cannot be spoofed from outside the tunnel.
- **CORS** is locked to the single allowed origin (no wildcard).
- **Fail-closed config.** `JWT_SECRET` has no default; the app refuses to start without
  it.

#### `config.py` — settings

A single `pydantic-settings` source of truth, loaded from a `.env` resolved *next to the
module* (so the launch directory doesn't matter). The `USERS` map (`username → bcrypt
hash`) is deliberately kept **outside** the `Settings` class so it can be swapped for a
database lookup in v2 without touching configuration.

#### `vnc_bridge.py` — the relay

A pure transport layer. It owns exactly one VNC TCP connection and shuttles raw bytes to
and from a WebSocket — no HTTP, no auth, no RFB parsing (the browser's RFB.js owns the
protocol; this class only moves bytes).

- Two relay tasks (`ws → vnc`, `vnc → ws`) run under
  `asyncio.wait(return_when=FIRST_COMPLETED)`. When **either** side closes, the other is
  cancelled and the whole bridge tears down — a browser tab close or a VNC EOF cleanly
  collapses the connection.
- **32 KB** chunk size — large enough for framebuffer throughput, small enough to keep
  mouse/keyboard input latency low.
- `drain()` provides backpressure if the VNC server lags; `close()` is idempotent.

### Frontend — single-page client

Location: `frontend/`

| File | Responsibility |
| --- | --- |
| `index.html` | Static landing page (service catalog → "remote desktop" → `/app/`) |
| `app/index.html` | SPA shell — three views: `login`, `dashboard`, `viewer` |
| `assets/app.js` | Client logic — auth handshake, view switching, noVNC wiring |
| `assets/style.css` | Shared stylesheet (GitHub-dark, monospace aesthetic) |
| `lib/novnc` | noVNC, included as a git submodule (the RFB.js client) |

Architectural highlights:

- **One page, three views**, toggled in place via `[hidden]`. Because nothing ever
  navigates, **the JWT and session token live only in JS module scope** — never
  `localStorage`, `sessionStorage`, the URL fragment, or `window`.
- **RFB.js is lazy-imported** and *pre-warmed* the moment the dashboard appears, so the
  module graph is already cached by the time the user clicks **Connect** (the viewer
  feels instant).
- On disconnect the client returns to the dashboard. The JWT is typically still valid,
  so reconnecting only needs a fresh single-use session token — which the dashboard
  requests again.
- The viewer uses `scaleViewport = true` (fit framebuffer to canvas),
  `resizeSession = false`, and `viewOnly = false` (forward mouse + keyboard).

### System configuration

Location: `sys/`

#### `sys/cloudkeep` — the NGINX vhost (the edge)

This single file does most of the edge's hardening work:

- **TLS** via Certbot / Let's Encrypt for `cloudkeep.duckdns.org`; HTTP → HTTPS redirect;
  HSTS.
- **Default catch-all** server returns `444` (drop) for any host that isn't
  `cloudkeep.duckdns.org`.
- **Request-shape limits** — small body/header buffers, tight timeouts (slow-loris /
  slow-POST defense), connection and request rate limiting.
- **HTTP method allowlist** (`GET|HEAD|POST|OPTIONS`, else `405`).
- **Security-header baseline** site-wide (X-Frame-Options DENY, nosniff, no-referrer,
  Permissions-Policy, COOP/CORP, a strict CSP).
- **A separately-tuned, relaxed CSP for `/app/`** — RFB.js requires `style-src
  'unsafe-inline'` (inline canvas styles), `img-src blob:` (cursor images), `worker-src
  blob:` (framebuffer decoder workers), and `connect-src wss://…` (the WebSocket). The
  `^~ /app/` prefix is intentional so this location's CSP beats the regex `.html` cache
  block, and the headers are repeated because NGINX does **not** inherit parent
  `add_header` directives once a location sets its own.
- **Routing.** `/ck/auth/` and `/ck/ws` proxy to `10.10.10.2:8000`; `^~ /assets/` and
  `^~ /lib/` are served with correct MIME types (so `.js` is `application/javascript`,
  not `text/html`).

#### `sys/vnc/` — the desktop

| File | Purpose |
| --- | --- |
| `config` | TigerVNC session config — XFCE, `1280x800`, `localhost=yes`, `SecurityTypes=None` |
| `tigervnc.conf` | `$SecurityTypes = "None";` |
| `xstartup` | Launches `startxfce4` via `dbus-launch` |

**`SecurityTypes=None` is intentional and safe in this topology.** The only thing that
can reach `127.0.0.1:5901` is the gateway's bridge running on the same host;
authentication is enforced one layer up at the gateway. VNC-level auth would be
redundant and would require the browser to handle VNC credentials.

---

## Security model

Defense in depth, layer by layer:

| Layer | Control |
| --- | --- |
| **Edge** | TLS only; non-matching hosts dropped (`444`); method allowlist; request-shape + timeout limits; per-IP rate + connection limits |
| **Headers** | Strict CSP (tightened per-route), HSTS, X-Frame-Options DENY, nosniff, COOP/CORP, Permissions-Policy |
| **Transport** | WireGuard encrypts the EC2 ↔ VM hop; UFW restricts `:8000` to the EC2 peer only |
| **Auth** | bcrypt password hashing; constant-time verification (no username enumeration); JWT (HS256, 60-min); fail-closed on missing secret |
| **Session** | Single-use, 30-second WebSocket tokens, atomically consumed on connect |
| **Exposure** | Gateway binds the WireGuard interface; VNC binds loopback; credentials never persisted in the browser |

---

## Deployment

CloudKeep deploys across **two hosts**.

### Edge (AWS EC2)

1. NGINX serves the static SPA from `/var/www/html/cloudkeep` and reverse-proxies
   `/ck/*` over WireGuard to `10.10.10.2:8000`.
2. TLS certificates are issued and renewed by Certbot / Let's Encrypt for
   `cloudkeep.duckdns.org`.
3. DuckDNS points the hostname at the EC2 public IP.
4. WireGuard is configured with the EC2 peer at `10.10.10.1`.

The vhost in this repo (`sys/cloudkeep`) is deployed to NGINX's `sites-available` /
`sites-enabled`.

### Private compute (Local KVM VM)

1. **WireGuard** brings up `10.10.10.2` and peers with the EC2 edge.
2. **UFW** restricts inbound `:8000` to the EC2 WireGuard peer only.
3. **TigerVNC** runs an XFCE session on `:1` (`127.0.0.1:5901`) using the configs in
   `sys/vnc/`.
4. **FastAPI gateway** runs under `uvicorn`, bound to `10.10.10.2:8000`, with a `.env`
   providing `JWT_SECRET`.

### Runtime dependencies (gateway)

The Python gateway depends on:

```
fastapi
uvicorn
bcrypt
python-jose          # JWT (jose)
slowapi              # rate limiting
pydantic-settings
```

> **Operational note:** there is not yet a pinned `requirements.txt`/`pyproject.toml`, a
> systemd unit, or a `.gitignore` checked into the repo, and the noVNC submodule needs a
> `.gitmodules` entry for clean `--recurse-submodules` clones. These are tracked as
> deployment-hardening follow-ups (see [Roadmap](#roadmap-v2)).

---

## Configuration reference

Backend settings (from `config.py`, overridable via `.env` beside the module):

| Setting | Default | Notes |
| --- | --- | --- |
| `VNC_HOST` | `127.0.0.1` | VNC backend, always loopback |
| `VNC_PORT` | `5901` | TigerVNC display `:1` |
| `LISTEN_HOST` | `10.10.10.2` | WireGuard interface only — never `0.0.0.0` |
| `LISTEN_PORT` | `8000` | Gateway port |
| `JWT_SECRET` | *(required)* | No default — app fails closed if unset |
| `JWT_ALGORITHM` | `HS256` | |
| `JWT_EXPIRY_MINUTES` | `60` | JWT lifetime |
| `WS_TOKEN_EXPIRY_SECONDS` | `30` | One-time WebSocket token TTL |
| `ALLOWED_ORIGIN` | `https://cloudkeep.duckdns.org` | Single CORS origin, no wildcard |
| `AUTH_RATE_LIMIT` | `5/minute` | Per-IP limit on `/auth/login` |

The POC user store lives in `config.py` as `USERS: dict[str, str]` (`username → bcrypt
hash`). Generate a hash with:

```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'your_password', bcrypt.gensalt()).decode())"
```

---

## Repository layout

```
CloudKeep/
├── README.md
├── backend/
│   └── src/
│       ├── auth.py            # FastAPI gateway: auth, JWT, session store, /ws
│       ├── config.py          # pydantic-settings + POC user store
│       └── vnc_bridge.py      # async TCP ↔ WebSocket byte relay
├── frontend/
│   ├── index.html             # static landing page
│   ├── app/
│   │   └── index.html         # SPA shell (login / dashboard / viewer)
│   ├── assets/
│   │   ├── app.js             # client logic
│   │   └── style.css          # shared stylesheet
│   └── lib/
│       └── novnc/             # noVNC (git submodule — RFB.js)
└── sys/
    ├── cloudkeep              # NGINX vhost (edge: TLS, proxy, hardening)
    └── vnc/
        ├── config             # TigerVNC session config
        ├── tigervnc.conf      # SecurityTypes = None
        └── xstartup           # XFCE launch via dbus
```

> **Note:** `.env` (containing `JWT_SECRET`) and the VNC `passwd` are intentionally not
> tracked in this repository. Provide them out-of-band on the target hosts.

---

## Roadmap (v2)

The codebase already anticipates its next iteration; the POC seams are documented in
place ("single-worker only", "POC: one hardcoded VM", "in v2 this list is rendered from
the API").

- **DB-backed users.** Replace the `USERS` dict with a database table (the store is
  already isolated from `Settings` for exactly this).
- **Multi-VM dashboard.** Render the desktop list from an API instead of a single
  hardcoded card; map each desktop to its own VNC backend.
- **Horizontally scalable session state.** Move the in-memory `SessionStore` and the
  rate limiter to Redis so the gateway can run multiple workers / replicas (today it is
  single-worker by design).
- **Deployment hardening.** Add a pinned `requirements.txt`/`pyproject.toml`, a systemd
  unit for the gateway, a `.gitignore`, and a `.gitmodules` entry for the noVNC
  submodule.
