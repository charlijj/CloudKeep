# CloudKeep — Backend (controld) setup guide

How the control plane is brought up on the **KVM host**: `cloudkeep-controld` is
a FastAPI service that authenticates users, tracks host resources, provisions
VMs via libvirt, and bridges the browser's WebSocket to each VM's VNC. It binds
the WireGuard address `10.10.10.2:8000`, which the AWS edge reverse-proxies.

This guide assumes the **current state** of the host:

- Repo cloned to `/opt/cloudkeep` (so the code is at `/opt/cloudkeep/backend/src`).
- An `app` user owns `/opt/cloudkeep`.
- A venv exists at `/opt/cloudkeep/.venv`.

> Paths/user here match that layout (`app`, `/opt/cloudkeep/backend/src`,
> `/opt/cloudkeep/.venv`). The generic template in `sys/systemd/` uses a
> dedicated `cloudkeep` user under `/opt/cloudkeep/src`; if you'd rather isolate
> controld from the `app` account, switch to that — the steps are identical with
> the names changed.

Host infrastructure (WireGuard on the host, the isolated `ckbr0` network +
firewall, the storage pool, the golden image) is covered in
**`../sys/HOSTSETUP.md`** and **`../sys/GOLDEN-IMAGE.md`**. This guide covers the
**controld service itself** and references those where they're prerequisites.

---

## 1. Python dependencies (fixing the libvirt-python build error)

Installing `requirements.txt` fails on `libvirt-python` because pip tries to
**build it from source** and can't find libvirt's dev headers:

```
Package 'libvirt', required by 'virtual:world', not found
ERROR: Failed to build 'libvirt-python'
```

Don't build it — use the distro binding and let the venv import it.

```bash
# Distro libvirt Python binding (matches the system python3.12; no compiler)
sudo dnf install -y python3-libvirt

# Let the existing venv see system site-packages (so `import libvirt` works)
sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' \
    /opt/cloudkeep/.venv/pyvenv.cfg

# Install the rest from PyPI. requirements.txt no longer lists libvirt-python,
# but the grep keeps this working even with an older copy of the file.
source /opt/cloudkeep/.venv/bin/activate
grep -v '^libvirt-python' /opt/cloudkeep/backend/requirements.txt | pip install -r /dev/stdin

# Verify both the binding and the app's imports resolve inside the venv
python -c "import libvirt; print('libvirt', libvirt.getVersion())"
python -c "import fastapi, uvicorn, uvloop, bcrypt, jose, slowapi, pydantic_settings; print('deps ok')"
```

> Why `--system-site-packages`: the libvirt binding is tightly coupled to the
> host's libvirt version, so the distro rpm is the reliable source. Everything
> else stays isolated in the venv.

**Troubleshooting `ModuleNotFoundError: No module named 'libvirt'`** (the venv
can't see the binding — almost always the rpm isn't installed or the venv flag
isn't set). Diagnose in order:

```bash
rpm -q python3-libvirt || sudo dnf install -y python3-libvirt          # 1. installed?
/usr/bin/python3 -c "import libvirt; print(libvirt.getVersion())"      # 2. system can import?
grep '^include-system-site-packages' /opt/cloudkeep/.venv/pyvenv.cfg   # 3. should be '= true'
/opt/cloudkeep/.venv/bin/python -c "import libvirt; print(libvirt.getVersion())"  # 4. venv can import?
```

- Step 2 fails → rpm missing (step 1 fixes it).
- Step 2 works, step 4 fails → flip the flag in step 3 to `= true` (no
  reactivation needed; it's read at interpreter startup).
- Still failing → recreate the venv with the flag baked in:
  `rm -rf /opt/cloudkeep/.venv && python3 -m venv --system-site-packages
  /opt/cloudkeep/.venv` then redo the pip step above.

> Note: `pip install libvirt` does **not** work — there is no PyPI package by
> that name (the buildable one is `libvirt-python`, which we deliberately avoid).

## 2. Give the `app` user libvirt access

controld talks to `qemu:///system`, which requires membership in the `libvirt`
(and `kvm`) groups.

```bash
sudo usermod -aG libvirt,kvm app
# Confirm (log out/in for an interactive shell; the systemd unit picks it up via
# SupplementaryGroups regardless):
id app | tr ',' '\n' | grep -E 'libvirt|kvm'
```

> The same `libvirt` group membership is what lets controld attach to a domain's
> serial console (`openConsole`) for the portal's live **Logs** boot-log pop-up —
> no extra privilege needed. How many of those streams may run at once, and the
> per-stream buffer, are bounded by `CONSOLE_MAX_STREAMS` / `CONSOLE_QUEUE_MAX`
> in `config.py` (sane defaults; override in `.env` only if you need to).

## 3. Runtime directories + SELinux

controld writes its SQLite DB under `/var/lib/cloudkeep`; libvirt stores VM disk
overlays in the image pool there too. On CentOS the image path **must** carry the
`virt_image_t` SELinux label or sVirt blocks VM disks (see `HOSTSETUP.md` §1.1).

```bash
sudo install -d -o app -g app -m 0755 /var/lib/cloudkeep          # traversable by qemu
sudo install -d -o app -g app -m 0700 /var/lib/cloudkeep/db       # private (user table)
sudo install -d -o app -g app -m 0711 /var/lib/cloudkeep/images   # libvirt pool
sudo dnf install -y policycoreutils-python-utils      # provides semanage
sudo semanage fcontext -a -t virt_image_t "/var/lib/cloudkeep/images(/.*)?"
sudo restorecon -Rv /var/lib/cloudkeep/images
```

> Keep the **parent `0755`** (or at least `o+x`): on real libvirt the `qemu`
> user must traverse `/var/lib/cloudkeep` to open VM disks under `images/`. A
> `0700` parent would `Permission denied` both the controld DB write and qemu.

## 4. Configuration (`.env`)

`config.py` loads `.env` from **beside itself** — i.e.
`/opt/cloudkeep/backend/src/.env`. Only `JWT_SECRET` is mandatory; everything
else has a sane default in `config.py`.

```bash
sudo -u app tee /opt/cloudkeep/backend/src/.env >/dev/null <<EOF
JWT_SECRET=$(openssl rand -hex 32)
ALLOWED_ORIGIN=https://cloudkeep-auth.duckdns.org
# Bring controld up WITHOUT real KVM first (login/quotas/lifecycle all work):
LIBVIRT_URI=fake
EOF
chmod 600 /opt/cloudkeep/backend/src/.env
```

> `ALLOWED_ORIGIN` **must** match the edge domain exactly, or CORS rejects the
> browser's API calls. Default is `https://cloudkeep-auth.duckdns.org`; set it to
> your real domain.

## 5. Smoke test in fake mode

Confirm the service runs and the API works before wiring real libvirt.

```bash
# Seed an admin user (interactive password)
sudo -u app /opt/cloudkeep/.venv/bin/python /opt/cloudkeep/backend/src/seed_user.py alice --admin

# Run it in the foreground
cd /opt/cloudkeep/backend/src
sudo -u app /opt/cloudkeep/.venv/bin/python run.py
```

In another shell:

```bash
curl -s http://10.10.10.2:8000/health        # {"status":"ok","libvirt_ok":true,...}
curl -s -X POST http://10.10.10.2:8000/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"alice","password":"<the password>"}'   # returns access_token
```

Stop it with Ctrl-C once both succeed.

## 6. Switch to real provisioning (prerequisites)

Before flipping off fake mode, the host infrastructure must exist — from
`sys/HOSTSETUP.md`:

- **WireGuard on the host** (§3): `wg0` = `10.10.10.2`, peered with the edge.
- **Isolated network `ckbr0`** + the nftables egress policy (§4–4.1).
- **Storage pool `cloudkeep`** at `/var/lib/cloudkeep/images` (§5).
- **Golden image** `golden-v1.qcow2` in that pool (`sys/GOLDEN-IMAGE.md`).

Then point controld at real libvirt:

```bash
sudo -u app sed -i 's#^LIBVIRT_URI=fake#LIBVIRT_URI=qemu:///system#' \
    /opt/cloudkeep/backend/src/.env
```

## 7. Install the systemd service

The canonical unit lives in the repo at `sys/systemd/cloudkeep-controld.service`.
If you have the full repo on the host, `sudo cp` it into `/etc/systemd/system/`.
Otherwise paste it (this is the same content, tailored to the `app` /
`/opt/cloudkeep/backend/src` / `/opt/cloudkeep/.venv` layout):

```bash
sudo tee /etc/systemd/system/cloudkeep-controld.service >/dev/null <<'EOF'
[Unit]
Description=CloudKeep control plane (controld)
Documentation=https://github.com/charlijj/CloudKeep
After=network-online.target wg-quick@wg0.service libvirtd.service virtqemud.service virtnetworkd.service virtstoraged.service virtnwfilterd.service
Wants=network-online.target wg-quick@wg0.service
StartLimitIntervalSec=120
StartLimitBurst=10

[Service]
Type=exec
User=app
Group=app
SupplementaryGroups=libvirt kvm
WorkingDirectory=/opt/cloudkeep/backend/src
ExecStart=/opt/cloudkeep/.venv/bin/python /opt/cloudkeep/backend/src/run.py
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=3
TimeoutStartSec=30

# --- Hardening (least privilege; verify: systemd-analyze security) ---
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/cloudkeep /run/libvirt
PrivateTmp=yes
ProtectProc=invisible
ProcSubset=pid
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
SystemCallArchitectures=native
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
IPAddressDeny=any
IPAddressAllow=localhost 10.10.10.0/24 10.20.0.0/24
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

# Stop the manual run first so both don't bind 10.10.10.2:8000:
#   (Ctrl-C the foreground `python run.py`)
sudo systemctl daemon-reload
sudo systemctl enable --now cloudkeep-controld.service
systemctl status cloudkeep-controld --no-pager
journalctl -u cloudkeep-controld -n 30 --no-pager
systemd-analyze security cloudkeep-controld.service     # expect a low (good) score
```

> If it fails to start after a `python3-libvirt` or library upgrade,
> `MemoryDenyWriteExecute` is the first knob to relax (then `SystemCallFilter`).
> Check `journalctl -u cloudkeep-controld`.

## 8. Verify end-to-end

```bash
# Local (on the host)
curl -s http://10.10.10.2:8000/health

# Through the edge (from anywhere) — exercises NGINX + WireGuard + controld
curl -s https://cloudkeep-auth.duckdns.org/ck/health
```

Then sign in at `https://cloudkeep-auth.duckdns.org/app/`, build a VM, and watch
the card go `queued → building → ready` (the card shows the live provisioning
*stage* as it goes). Click **Logs** on a building or running card to open the
serial boot-log pop-up and watch the kernel/systemd stream in real time. If
`building` ever ends in `error`, `journalctl -u cloudkeep-controld` has the
provisioning failure (usually a missing golden image, SELinux label, or
`clean-traffic` nwfilter — all covered in `HOSTSETUP.md`).

> The boot-log pop-up needs the edge to proxy `/ck/console` — that block is in
> the repo's `sys/cloudkeep` and in `frontend/EDGE-SETUP.md` §7. If Logs opens
> then instantly disconnects, the edge is missing that `location`.

## Managing users

```bash
# Create or update a user + quotas (interactive password)
sudo -u app /opt/cloudkeep/.venv/bin/python /opt/cloudkeep/backend/src/seed_user.py \
    bob --max-vms 3 --max-vcpus 6 --max-mem-mb 12288 --max-disk-gb 150
```

## Self-service account requests (optional)

Users can request an account from the landing page; you approve each one on the
host. A request is **inert** — it only queues a row and grants nothing, so this
doesn't weaken the posture. It's **fail-closed**: with no invite code configured,
every request is rejected.

```bash
# 1. Enable it: set one or more invite codes in .env, then restart controld.
#    (Empty/unset => signups closed. Comma-separated => multiple codes.)
echo 'SIGNUP_INVITE_CODES=clinic-2026' | sudo -u app tee -a \
    /opt/cloudkeep/backend/src/.env
sudo systemctl restart cloudkeep-controld
# Hand the code to the people you want to let in. (Optional tuning, all have
# sane defaults: ACCOUNT_REQUESTS_ENABLED, SIGNUP_RATE_LIMIT,
# MAX_PENDING_REQUESTS=10, MAX_PENDING_PER_IP=1.)

# 2. Review incoming requests and approve/deny them:
CK=/opt/cloudkeep/.venv/bin/python
sudo -u app $CK /opt/cloudkeep/backend/src/review_requests.py list
sudo -u app $CK /opt/cloudkeep/backend/src/review_requests.py show 1
sudo -u app $CK /opt/cloudkeep/backend/src/review_requests.py approve 1 --max-vms 3
sudo -u app $CK /opt/cloudkeep/backend/src/review_requests.py deny 2 --reason "unrecognised"
```

`approve` prompts for the user's initial password and creates the account exactly
like `seed_user.py` (the requester never sets a password). Deliver that password
to the user out-of-band — it is never emailed. The edge must proxy the account
endpoints (`POST /ck/account-requests` and `GET /ck/account-requests/verify`,
both in `sys/cloudkeep` and `frontend/EDGE-SETUP.md`).

**Optional — email + email verification.** Off by default. Set `SMTP_ENABLED=true`
plus the `SMTP_*` values in `.env` (see `USER-MANAGEMENT.md` §5) and restart
controld. With it on, a requester must confirm their address via a tokenised
email link before their request is reviewable (`REQUIRE_EMAIL_VERIFICATION`,
default on), and approved users get a "your account is ready" email. With SMTP
off, behaviour is unchanged (requests go straight to `pending`, no email).

## Updating controld

```bash
cd /opt/cloudkeep && sudo -u app git pull
# re-run §1's pip step if requirements.txt changed
sudo systemctl restart cloudkeep-controld
```
