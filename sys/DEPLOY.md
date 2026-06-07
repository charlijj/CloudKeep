# CloudKeep — deploy & restart runbook (performance Phase 1 + 2)

Commands to roll out the latency changes in this PR. Run each block on the host
named in its heading. Nothing here is destructive; every step is reversible by
redeploying the previous file.

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

## 3. EC2 edge — frontend (Phase 2 client tuning)

Only `frontend/assets/app.js` changed (qualityLevel / compressionLevel). Static
deploy, no NGINX change this phase.

```bash
# Sync the SPA to the web root (preserves the noVNC submodule under lib/)
sudo rsync -a --delete frontend/ /var/www/html/cloudkeep/

# Validate and reload (reload is harmless for static; keeps things tidy)
sudo nginx -t && sudo systemctl reload nginx
```

Hard-refresh the browser (Ctrl/Cmd-Shift-R) so the updated `app.js` is fetched.

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

## Rollback

- **Desktop:** restore the previous `~/.vnc/xstartup`, `vncserver -kill :1 && vncserver :1`.
- **Gateway:** start with the old command (`uvicorn auth:app --host 10.10.10.2 --port 8000`); uvloop/deflate changes live only in `run.py`.
- **Frontend:** redeploy the previous `app.js` and hard-refresh.
