# CloudKeep — Edge (AWS NGINX) setup guide

How the public-facing edge is built: an Ubuntu EC2 instance running NGINX that
terminates TLS, serves the SPA, and reverse-proxies `/ck/*` over a WireGuard
tunnel to the control plane (`controld`) on the KVM host at `10.10.10.2:8000`.

```
Browser ⇄ HTTPS/WSS :443 ⇄ [ EC2: NGINX + static SPA ] ⇄ WireGuard ⇄ 10.10.10.2:8000 (controld)
```

The edge holds **no secrets and no state** — it's a TLS terminator + static host
+ reverse proxy. Rebuilding it is just these steps.

## 0. Assumptions / prerequisites

Already done on the new instance:
- Ubuntu, `app` user created, `/var/www/html/cloudkeep/` exists.
- NGINX and WireGuard installed (not yet configured).
- This repo cloned into `~app` (referred to below as `~/cloudkeep`).

You'll also need:
- The **KVM host's WireGuard public key** (`host.pub` from `sys/HOSTSETUP.md` §3).
- `certbot`: `sudo apt update && sudo apt install -y certbot`
- DNS: `cloudkeep-auth.duckdns.org` pointing at this instance's public IP (§2).

> **Use an Elastic IP.** A plain EC2 public IP changes on stop/start, which
> breaks both DNS and the WireGuard endpoint the host dials. Allocate + associate
> an Elastic IP before continuing.

## 1. AWS security group (the real firewall)

On the instance's security group, allow **inbound**:

| Port | Proto | Source | Why |
|---|---|---|---|
| 22 | TCP | your admin IP | SSH |
| 80 | TCP | 0.0.0.0/0 | HTTP (ACME challenge + redirect) |
| 443 | TCP | 0.0.0.0/0 | HTTPS / WSS |
| 51820 | UDP | 0.0.0.0/0 | WireGuard (the host dials in) |

Outbound: default allow. (Optionally mirror this with `ufw` on the instance, but
the security group is the authoritative control.)

## 2. DNS — point DuckDNS at this instance

Set `cloudkeep` to the Elastic IP (DuckDNS dashboard, or):

```bash
curl "https://www.duckdns.org/update?domains=cloudkeep&token=<YOUR_DUCKDNS_TOKEN>&ip=<ELASTIC_IP>"
# verify:
dig +short cloudkeep-auth.duckdns.org      # should print the Elastic IP
```

## 3. WireGuard — edge side

The edge is the side with the public endpoint (it `ListenPort`s); the KVM host
(behind NAT) dials in and keeps the tunnel alive with `PersistentKeepalive`.

```bash
# Generate the edge keypair
wg genkey | sudo tee /etc/wireguard/edge.key | wg pubkey | sudo tee /etc/wireguard/edge.pub
sudo chmod 600 /etc/wireguard/edge.key

sudo tee /etc/wireguard/wg0.conf >/dev/null <<EOF
[Interface]
Address = 10.10.10.1/24
ListenPort = 51820
PrivateKey = $(sudo cat /etc/wireguard/edge.key)

[Peer]
# The KVM host (host.pub from HOSTSETUP §3). No Endpoint — the host dials us.
PublicKey = <KVM_HOST_PUBLIC_KEY>
AllowedIPs = 10.10.10.2/32
EOF

sudo systemctl enable --now wg-quick@wg0
```

Key exchange (two values cross the wire):
- Put this edge's `/etc/wireguard/edge.pub` into the **host's** `wg0.conf`
  `[Peer] PublicKey` (it had a `<EC2_PUBLIC_KEY>` placeholder), then restart
  `wg-quick@wg0` on the host.
- The **host's** `host.pub` goes into the edge config above.

Verify once both sides are up (the host must be running and dialing in):

```bash
sudo wg show                        # expect a handshake + transfer
ping -c3 10.10.10.2                 # the KVM host over the tunnel
curl -s http://10.10.10.2:8000/health   # controld: {"status":"ok",...}
```

> No IP forwarding / NAT is needed on the edge — NGINX originates the proxy
> connection to `10.10.10.2` locally over `wg0`.

## 4. Deploy the frontend files

Copy the SPA to the web root. The committed noVNC **bundle** is all that's
served, so exclude the raw submodule tree.

```bash
sudo rsync -a --delete --exclude 'lib/novnc/' ~/cloudkeep/frontend/ /var/www/html/cloudkeep/
sudo chown -R app:www-data /var/www/html/cloudkeep
sudo chmod -R a+rX /var/www/html/cloudkeep        # nginx (www-data) must read
```

## 5. Rate-limit zones (required by the vhost)

The vhost references two shared-memory zones; they must be defined in the
`http{}` context or `nginx -t` fails with "unknown limit_req zone".

```bash
sudo tee /etc/nginx/conf.d/cloudkeep-limits.conf >/dev/null <<'EOF'
# Brute-force cap for /ck/auth/ (rate) + global per-IP connection cap.
limit_req_zone  $binary_remote_addr zone=cloudkeep_req:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=cloudkeep_conn:10m;
EOF
```

## 6. TLS — issue the certificate (HTTP bootstrap first)

The hardened vhost (§7) references cert files that don't exist yet, so issue the
cert against a minimal HTTP-only site first.

```bash
# 6a. Minimal HTTP site that serves the ACME webroot
sudo tee /etc/nginx/sites-available/cloudkeep >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name cloudkeep-auth.duckdns.org;
    root /var/www/html/cloudkeep;
    location /.well-known/acme-challenge/ { allow all; }
    location / { return 200 'bootstrap'; }
}
EOF
sudo ln -sf /etc/nginx/sites-available/cloudkeep /etc/nginx/sites-enabled/cloudkeep
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 6b. Issue the cert via webroot (no nginx downtime)
sudo certbot certonly --webroot -w /var/www/html/cloudkeep \
     -d cloudkeep-auth.duckdns.org --agree-tos -m <your-email> --no-eff-email

# 6c. Confirm renewal works end-to-end
sudo certbot renew --dry-run
```

`certbot` installs a systemd timer (`certbot.timer`) that auto-renews; the §7
config keeps `/.well-known/` reachable on port 80 so renewals succeed.

## 7. Install the hardened vhost

Replace the bootstrap with the full config below, then reload.

> **Your domain appears in 4 places — keep them identical** (the examples use
> `cloudkeep-auth.duckdns.org`; replace with yours if different):
> 1. HTTP redirect server → `server_name`
> 2. HTTPS server → `server_name`
> 3. HTTPS server → `ssl_certificate` + `ssl_certificate_key` (`live/<domain>/…`)
> 4. `/app/` CSP → `connect-src … wss://<domain>` — **if this is wrong, login
>    works but the VNC + boot-log WebSockets are silently blocked by CSP.**
>    (The boot-log pop-up `app/console.html` is under `/app/`, so it inherits
>    this same CSP — one `connect-src` covers both WebSockets.)
>
> Don't delete the two `ssl_certificate*` lines when editing — without them
> nginx fails with `no "ssl_certificate" is defined for the "listen ... ssl"`.

```bash
sudo tee /etc/nginx/sites-available/cloudkeep >/dev/null <<'EOF'
# CloudKeep edge — TLS terminator + static SPA + /ck reverse proxy.

# Drop any request that isn't for our hostname.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 444;
}

# HTTP: serve ACME challenges, redirect everything else to HTTPS.
server {
    listen 80;
    listen [::]:80;
    server_name cloudkeep-auth.duckdns.org;
    root /var/www/html/cloudkeep;
    location /.well-known/acme-challenge/ { allow all; }   # cert renewal
    location / { return 301 https://$host$request_uri; }
}

# HTTPS: the site.
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name cloudkeep-auth.duckdns.org;

    root  /var/www/html/cloudkeep;
    index index.html;

    ssl_certificate     /etc/letsencrypt/live/cloudkeep-auth.duckdns.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cloudkeep-auth.duckdns.org/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    access_log /var/log/nginx/cloudkeep.access.log;
    error_log  /var/log/nginx/cloudkeep.error.log warn;

    # Request-shape limits (slow-loris / slow-POST defense)
    client_max_body_size        1k;
    client_body_buffer_size     1k;
    client_header_buffer_size   1k;
    large_client_header_buffers 2 1k;
    client_body_timeout    10s;
    client_header_timeout  10s;
    keepalive_timeout      15s;
    send_timeout           10s;

    # Per-IP connection cap (request-RATE limiting lives only on /ck/auth/ so a
    # cold asset burst is never throttled into 503s).
    limit_conn cloudkeep_conn 10;

    if ($request_method !~ ^(GET|HEAD|POST|DELETE|OPTIONS)$) { return 405; }

    # Security headers (site-wide baseline)
    add_header X-Frame-Options              "DENY"        always;
    add_header X-Content-Type-Options       "nosniff"     always;
    add_header Referrer-Policy              "no-referrer" always;
    add_header Permissions-Policy           "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;
    add_header Cross-Origin-Opener-Policy   "same-origin" always;
    add_header Cross-Origin-Resource-Policy "same-origin" always;
    add_header Strict-Transport-Security    "max-age=31536000" always;
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'" always;

    location ~ /\.(?!well-known) { deny all; access_log off; log_not_found off; }
    location ~* \.(env|git|sql|bak|swp|log|ini|conf|yml|yaml)$ { deny all; }
    location ~* /(wp-|xmlrpc\.php|administrator/|admin\.php|phpmyadmin|\.aws|\.ssh) { deny all; }
    autoindex off;

    # ---- Control plane (controld on the KVM host, over WireGuard) ----

    # Auth API — request-rate limited (the brute-force surface).
    location /ck/auth/ {
        limit_req zone=cloudkeep_req burst=10 nodelay;
        proxy_pass http://10.10.10.2:8000/auth/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Public account-request intake (landing-page form). Unauthenticated, so
    # throttle hard here; controld adds an invite-code gate + per-IP rate limit
    # + queue caps. Exact match wins over the generic /ck/ proxy below.
    location = /ck/account-requests {
        limit_req zone=cloudkeep_req burst=2 nodelay;
        proxy_pass http://10.10.10.2:8000/account-requests;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # VNC WebSocket bridge.
    location /ck/ws {
        proxy_pass http://10.10.10.2:8000/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_set_header X-Real-IP  $remote_addr;
        proxy_buffering    off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Live serial-console (boot log) WebSocket — same upgrade/streaming setup as
    # the VNC bridge; proxy_buffering off so boot-log lines arrive in real time.
    # Omit this and the portal's Logs pop-up connects, then instantly drops.
    location /ck/console {
        proxy_pass http://10.10.10.2:8000/console;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_set_header X-Real-IP  $remote_addr;
        proxy_buffering    off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # Rest of the REST API: /ck/vms, /ck/resources, /ck/vms/{id}/session ...
    # (longest-prefix means /ck/auth/ and /ck/ws above still win)
    location /ck/ {
        proxy_pass http://10.10.10.2:8000/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ---- SPA + static assets ----

    # The gateway app needs a relaxed CSP for RFB.js (inline canvas styles,
    # blob workers/images, the wss WebSocket). ^~ makes this win over the
    # regex .html block below.
    location ^~ /app/ {
        add_header X-Frame-Options              "DENY"        always;
        add_header X-Content-Type-Options       "nosniff"     always;
        add_header Referrer-Policy              "no-referrer" always;
        add_header Permissions-Policy           "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;
        add_header Cross-Origin-Opener-Policy   "same-origin" always;
        add_header Cross-Origin-Resource-Policy "same-origin" always;
        add_header Strict-Transport-Security    "max-age=31536000" always;
        add_header Content-Security-Policy "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' wss://cloudkeep-auth.duckdns.org; worker-src blob:; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'" always;
        try_files $uri $uri/ /app/index.html;
    }

    # noVNC bundle is version-pinned (?v=) -> safe to cache immutably.
    location ^~ /lib/ {
        add_header Cache-Control "public, max-age=31536000, immutable" always;
        try_files $uri =404;
    }
    # Hand-edited SPA assets -> short cache + ETag revalidation.
    location ^~ /assets/ {
        add_header Cache-Control "public, max-age=300, no-transform" always;
        try_files $uri =404;
    }
    location ~* \.(html|css|svg|png|jpg|jpeg|gif|webp|ico|woff2?)$ {
        # Re-list the security baseline: an add_header here drops all inherited
        # ones, and without them /index.html is frameable (clickjacking).
        add_header X-Frame-Options              "DENY"        always;
        add_header X-Content-Type-Options       "nosniff"     always;
        add_header Referrer-Policy              "no-referrer" always;
        add_header Permissions-Policy           "geolocation=(), microphone=(), camera=(), payment=(), usb=()" always;
        add_header Cross-Origin-Opener-Policy   "same-origin" always;
        add_header Cross-Origin-Resource-Policy "same-origin" always;
        add_header Strict-Transport-Security    "max-age=31536000" always;
        add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'none'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'" always;
        add_header Cache-Control "public, no-transform" always;
        expires 1h;
        try_files $uri =404;
    }
    location / { try_files $uri $uri/ =404; }

    gzip on;
    gzip_vary on;
    gzip_min_length 256;
    gzip_proxied any;
    gzip_types text/plain text/css application/javascript image/svg+xml;
}
EOF

sudo nginx -t && sudo systemctl reload nginx
```

## 8. Verify

```bash
# TLS + HTTP/2 + redirect
curl -sI http://cloudkeep-auth.duckdns.org   | grep -i location      # -> https://...
curl -sI --http2 https://cloudkeep-auth.duckdns.org/app/ | grep -i '^HTTP'   # -> HTTP/2 200

# End-to-end through the tunnel to controld
curl -s https://cloudkeep-auth.duckdns.org/ck/health                 # {"status":"ok",...}
```

Then open `https://cloudkeep-auth.duckdns.org/app/` and sign in. A cold load should
pull a single `novnc.bundle.js` (no request storm) and show no console errors.

> Full end-to-end use also requires `controld` running on the KVM host and the
> golden image sealed — see `sys/HOSTSETUP.md` and `sys/GOLDEN-IMAGE.md`.

## Notes

- This edge config intentionally omits the unrelated legacy port-3001 block
  that appears in `sys/cloudkeep` (a separate project, not on this instance).
- Nothing here is stateful; to rebuild the edge on a new instance, repeat §1–§8.
- If you later move the control plane, only the three `proxy_pass http://10.10.10.2:8000`
  targets change — the rest of the edge is fixed.
