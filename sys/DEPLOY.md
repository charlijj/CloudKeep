# CloudKeep — deploy & restart runbook (performance Phase 1 + 2)

Commands to roll out the latency changes in this PR. Run each block on the host
named in its heading. Nothing here is destructive; every step is reversible by
redeploying the previous file.

---

## 0. Capture the baseline FIRST (Phase 0)

The bridge now logs a per-session summary on close (bytes, throughput, frame
count, average bytes/frame). To get a clean *before* number, deploy **only**
the instrumented bridge and run the gateway the **old** way — i.e. do **not**
yet apply the Phase 1 (xstartup) or Phase 2 (app.js / run.py) changes.

```bash
# On the KVM VM: deploy only the instrumented bridge, keep everything else as-is
# (old ~/.vnc/xstartup, old app.js on the edge, old start command below).

# Start the gateway the existing way (NOT run.py — that would add uvloop/deflate):
cd backend/src
uvicorn auth:app --host 10.10.10.2 --port 8000
```

Run one **representative session** with a fixed protocol so before/after are
comparable, e.g.: 30s idle desktop → 30s typing in a terminal → 30s dragging a
window → 30s scrolling a web page → 30s of video/motion. Then disconnect.

Read the summary line the bridge logs on disconnect:

```bash
# If started in a terminal, it's on stdout. Under systemd:
journalctl -u cloudkeep -n 50 --no-pager | grep "bridge closed"
```

Example line:

```
bridge closed (152.3s) down=812.4KB/s up=1.2KB/s s2c=126500000B c2s=190000B frames=4100 avg_frame=30854B
```

How to read it:

- **`down` (KB/s)** near your link's usable bandwidth → **bytes-bound**: the
  Phase 1/2 encoding changes will help most.
- **`down` low but the session still felt laggy** → likely **RTT-bound**:
  measure ping (below) before expecting encoding changes to help.
- **`avg_frame`** shrinking after Phase 1/2 is the direct proof the desktop +
  Tight/quality changes are cutting bytes.

Pair it with an RTT reading (the structural floor):

```bash
ping -c 20 cloudkeep.duckdns.org     # client -> edge (run from a client machine)
ping -c 20 10.10.10.2                # from EC2: edge -> VM over WireGuard (the hairpin)
```

Record both the bridge summary and the two ping averages. Then apply Phase 1 +
2 (sections below) and re-run the **same** protocol to compare.

---

## 1. KVM VM — desktop session (Phase 1)

Applies the new `sys/vnc/xstartup` (compositing off, outline move/resize) and,
if changed, `sys/vnc/config`. Run as the **desktop user** that owns the VNC
session (the one whose `~/.vnc` is used), not root.

```bash
# Deploy the session files
install -m 0755 sys/vnc/xstartup ~/.vnc/xstartup
install -m 0644 sys/vnc/config   ~/.vnc/config

# Restart the VNC server so the new xstartup takes effect (display :1)
vncserver -kill :1
vncserver :1
```

If the VNC server is managed by systemd instead of run by hand:

```bash
sudo systemctl restart vncserver@:1
# (unit name may differ, e.g. tigervncserver@:1 — check: systemctl list-units '*vnc*')
```

Verify compositing is actually off inside the session:

```bash
DISPLAY=:1 xfconf-query -c xfwm4 -p /general/use_compositing   # -> false
```

> A reconnect from the browser is enough to pick up the change once the VNC
> server has been restarted; no gateway restart is required for Phase 1.

---

## 2. KVM VM — gateway (Phase 2)

Installs uvloop + the tuned entrypoint and disables WebSocket per-message
deflate. Run in the gateway's Python environment.

```bash
# Install/refresh dependencies (uvicorn[standard] provides uvloop + websockets)
pip install -r backend/requirements.txt

# Deploy the new files alongside the existing app
#   backend/src/run.py  (new entrypoint)
# (auth.py / config.py / vnc_bridge.py unchanged this phase)
```

Start the gateway via the new entrypoint (from `backend/src`, with `.env`
present so JWT_SECRET loads):

```bash
cd backend/src
python run.py
```

If the gateway runs under systemd, point the unit's `ExecStart` at the new
entrypoint and restart:

```ini
# /etc/systemd/system/cloudkeep.service  (ExecStart line)
ExecStart=/path/to/venv/bin/python /path/to/CloudKeep/backend/src/run.py
WorkingDirectory=/path/to/CloudKeep/backend/src
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart cloudkeep
```

Confirm it came up and VNC is reachable:

```bash
curl -s http://10.10.10.2:8000/health    # {"status":"ok","vnc_reachable":true,...}
```

---

## 3. EC2 edge — frontend (Phase 2 + 3)

The SPA now loads noVNC as a single pre-built bundle (`lib/novnc.bundle.js`),
not the ~40-module raw tree, so the raw submodule source does **not** need to be
deployed.

```bash
# Sync the SPA to the web root. Exclude the raw noVNC submodule source — only
# the committed bundle (lib/novnc.bundle.js) is served at runtime.
sudo rsync -a --delete --exclude 'lib/novnc/' frontend/ /var/www/html/cloudkeep/

# Validate and reload
sudo nginx -t && sudo systemctl reload nginx
```

Hard-refresh the browser (Ctrl/Cmd-Shift-R) so the updated `app.js` is fetched.

> **Rebuilding the bundle** (only needed if you bump the noVNC submodule): from
> `frontend/`, run `npm install` then `npm run build:novnc`, and bump the
> `?v=1.5.0` query in `assets/app.js` to the new version so the immutable cache
> is busted. The regenerated `lib/novnc.bundle.js` is what you commit + deploy.

---

## 5. EC2 edge — NGINX (Phase 3)

Fixes the cold-load `NS_ERROR_CORRUPTED_CONTENT` / disallowed-MIME failures and
improves first-connect latency. Three changes in `sys/cloudkeep`:

1. **Rate limiting moved off static assets** onto `/ck/auth/` only. The old
   server-wide `limit_req` returned `503` (HTML) for the overflow of a cold
   asset burst, which the browser rejected as a bad-MIME module load.
2. **HTTP/2 enabled** (`listen 443 ssl http2`) — multiplexes assets over one
   connection.
3. **`/lib/` served immutable** (the bundle is cache-busted via `?v=`), and
   `/assets/` given a short revalidating cache.

```bash
# Deploy the vhost (path may be sites-available/cloudkeep on your box)
sudo cp sys/cloudkeep /etc/nginx/sites-available/cloudkeep
sudo nginx -t && sudo systemctl reload nginx
```

Confirm HTTP/2 and that the limiter no longer hits static assets:

```bash
curl -sI --http2 https://cloudkeep.duckdns.org/app/ | grep -i '^HTTP'   # expect HTTP/2 200
# After a cold load, there should be no 503s on the bundle/assets:
sudo awk '$9==503' /var/log/nginx/cloudkeep.access.log | tail
```

> Requires the `cloudkeep_req` / `cloudkeep_conn` zones to already exist in the
> `http {}` block of `nginx.conf` (they did before — unchanged here). If Certbot
> later rewrites the `listen` lines and drops `http2`, re-add it.

---

## 4. WireGuard MTU — verify, adjust only if needed (Phase 2 #6)

WireGuard usually auto-sets its MTU to ~1420 already, so this is a **check**,
not a mandatory change. Only lower it if the underlying path MTU is smaller
(e.g. PPPoE 1492, or double encapsulation), which causes tunneled TCP to
fragment and adds tail latency.

```bash
# Check current MTU on both peers
ip link show wg0          # look at the 'mtu' value

# If fragmentation is suspected, test the real path MTU to the peer
ping -M do -s 1392 -c 3 10.10.10.2     # from EC2; shrink size until it passes

# Apply (runtime), if a smaller value is needed:
sudo ip link set mtu 1420 dev wg0
```

Persist by adding `MTU = 1420` under `[Interface]` in `/etc/wireguard/wg0.conf`
on both peers, then `sudo systemctl restart wg-quick@wg0`.

> MSS clamping (`iptables ... TCPMSS --clamp-mss-to-pmtu`) only helps if wg0 is
> *routing forwarded* traffic. Here the EC2→VM TCP is locally originated by
> NGINX, so the interface MTU governs it — clamping is not needed for this path.

---

## 6. KVM VM — run on boot (systemd)

End-to-end "starts on boot" needs two units on the VM: the **desktop** (so VNC
is listening on 127.0.0.1:5901) and the **gateway** (which the service file
orders after it). Run as a user who can `sudo`.

### 6a. VNC desktop on boot (TigerVNC's packaged unit)

Use the distro's `tigervncserver@` template — don't hand-roll a sandbox around a
full XFCE session. It reads `~app/.vnc/config` + `xstartup` (already in place).

```bash
# Map display :1 to the 'app' user
echo ':1=app' | sudo tee -a /etc/tigervnc/vncserver.users

# Stop any hand-started session first (a stale one holds :1 / the pid file)
sudo -u app vncserver -kill :1 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable --now tigervncserver@:1.service
systemctl status tigervncserver@:1.service --no-pager
```

### 6b. Gateway on boot (hardened unit in this repo)

```bash
sudo cp sys/systemd/cloudkeep-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloudkeep-gateway.service

# Verify it's up and the hardening applied
systemctl status cloudkeep-gateway --no-pager
journalctl -u cloudkeep-gateway -n 30 --no-pager
systemd-analyze security cloudkeep-gateway.service     # expect a low (good) score
curl -s http://10.10.10.2:8000/health                  # {"status":"ok",...}
```

> **Before enabling:** stop any manually-run `python run.py` so two processes
> don't fight over `10.10.10.2:8000`.
>
> **Adjust the WireGuard dependency** in the unit (`wg-quick@wg0.service`) to
> match this host — check with `systemctl list-units --type=service | grep -i wg`.
> If wg0 comes up via `systemd-networkd`, depend on
> `systemd-networkd-wait-online.service` instead. The bind targets `10.10.10.2`,
> so the unit must start after that address exists (Restart= covers a brief race).

---

## Rollback

- **Boot units:** `sudo systemctl disable --now cloudkeep-gateway.service` (and
  `tigervncserver@:1.service`) to return to manual start.
- **Desktop:** restore the previous `~/.vnc/xstartup`, `vncserver -kill :1 && vncserver :1`.
- **Gateway:** start with the old command (`uvicorn auth:app --host 10.10.10.2 --port 8000`); uvloop/deflate changes live only in `run.py`.
- **Frontend:** redeploy the previous `app.js` and hard-refresh.
