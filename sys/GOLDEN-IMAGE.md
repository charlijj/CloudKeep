# CloudKeep — Golden Image VM

The **Golden Image VM** is, in reality, the existing **soccer-vision-vm**. We
convert it once into an immutable template (`golden-v1.qcow2`); every user VM is
a copy-on-write overlay of it, so clones take seconds and only consume disk as
they diverge.

> ⚠️ This is destructive to soccer-vision-vm as a *running desktop*: sysprep
> strips its identity. We seal a **copy**, so the original stays bootable — but
> back it up first if you want a clean rollback.

## Two machines — don't mix them up

This is the #1 source of confusion. There are **two** systems involved:

| | Machine | Hostname (example) | Package manager |
|---|---|---|---|
| **HOST** | the KVM hypervisor (CentOS) | `binki-kvm` | `dnf` |
| **GUEST** | the template VM (Ubuntu) | `soccer-vision-vm` | `apt` |

- **§1–§2 run INSIDE the guest** (Ubuntu): get a shell in soccer-vision-vm first
  — `virsh start soccer-vision-vm` on the host, then reach it via SSH, its
  desktop, or `virsh console soccer-vision-vm`.
- **§3 runs ON the host** (CentOS): it shuts the guest down and seals its disk.

If you run §1–§2 on the host by mistake, you'll install a first-boot unit that
**wipes the host's `/etc/machine-id` and hostname on reboot** — remove it from
the host with `sudo systemctl disable --now cloudkeep-firstboot.service && sudo
rm -f /etc/systemd/system/cloudkeep-firstboot.service`.

---

## 1. Desktop stack — RUN INSIDE THE GUEST (Ubuntu)

Make the in-guest desktop clone-ready (as a user with sudo in soccer-vision-vm):

> **If this VM is a v1 single-VM gateway** (it ran the gateway + WireGuard
> inside itself), strip that first — v2 runs the gateway on the HOST, and a
> leftover in-guest gateway bakes the old `JWT_SECRET` and the `10.10.10.2`
> tunnel address into every clone:
> ```bash
> sudo systemctl disable --now cloudkeep-gateway.service
> sudo rm -f /etc/systemd/system/cloudkeep-gateway.service
> sudo rm -rf /opt/rdgateway                     # v1 gateway code + its .env
> sudo systemctl disable --now wg-quick@wg0 2>/dev/null || true
> sudo rm -f /etc/wireguard/wg0.conf
> sudo systemctl daemon-reload
> ```

- **TigerVNC** serves XFCE on `:1` (5901). Session config is in
  `~app/.vnc/{config,xstartup,tigervnc.conf}` (in this repo under `sys/vnc/` —
  copy them into the guest). Two settings are load-bearing for v2:
  - `localhost=no` — controld is on the **host** and reaches this VNC over
    `ckbr0`, so it must NOT bind loopback. Verify (after the service is up):
    `ss -ltn | grep 5901` should show `0.0.0.0:5901`, not `127.0.0.1:5901`.
  - `SecurityTypes=None` — safe because the guest firewall (below) only admits
    the host bridge IP. Note: with `localhost=no`, TigerVNC refuses to start an
    unauthenticated listener unless the service passes `--I-KNOW-THIS-IS-INSECURE`
    (the `cloudkeep-vnc.service` unit already does).
- Install the in-guest VNC service so every clone starts it on boot:
  ```bash
  sudo cp sys/systemd/cloudkeep-vnc.service /etc/systemd/system/
  sudo systemctl enable cloudkeep-vnc.service
  ```
- **Guest firewall** — expose 5901 only to the host bridge address:
  ```bash
  sudo ufw default deny incoming
  sudo ufw default allow outgoing
  sudo ufw allow from 10.20.0.1 to any port 5901 proto tcp
  sudo ufw enable
  ```
- **Auto-updates + no default secrets:**
  ```bash
  sudo apt install -y unattended-upgrades cloud-guest-utils   # cloud-guest-utils provides growpart
  sudo dpkg-reconfigure -plow unattended-upgrades
  # remove any baked-in gateway code, shared passwords, throwaway SSH keys
  ```

## 2. First-boot unit — RUN INSIDE THE GUEST (Ubuntu)

Without this, every clone has the same identity and the user's chosen disk size
won't materialize. **Inside soccer-vision-vm:**

```bash
sudo tee /usr/local/sbin/cloudkeep-firstboot.sh >/dev/null <<'EOF'
#!/bin/bash
set -e
# Grow the root filesystem to the overlay's (user-chosen) size.
ROOT_DEV=$(findmnt -no SOURCE /)
DISK=/dev/$(lsblk -no PKNAME "$ROOT_DEV")
PART=$(echo "$ROOT_DEV" | grep -o '[0-9]*$')
growpart "$DISK" "$PART" || true
resize2fs "$ROOT_DEV" || true
# Fresh identity.
rm -f /etc/machine-id && systemd-machine-id-setup
hostnamectl set-hostname "vm-$(cat /sys/class/dmi/id/product_uuid | cut -c1-8)"
systemctl disable cloudkeep-firstboot.service
EOF
sudo chmod 755 /usr/local/sbin/cloudkeep-firstboot.sh

sudo tee /etc/systemd/system/cloudkeep-firstboot.service >/dev/null <<'EOF'
[Unit]
Description=CloudKeep first-boot customization
After=network-pre.target
Before=cloudkeep-vnc.service
ConditionPathExists=/usr/local/sbin/cloudkeep-firstboot.sh
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cloudkeep-firstboot.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable cloudkeep-firstboot.service
```

When §1–§2 are done, shut the guest down (`sudo poweroff` inside it, or let §3
do it).

## 3. Seal into the template — RUN ON THE HOST (CentOS)

`virsh shutdown` is **async** (wait for `shut off` or `qemu-img` hits the disk
lock) and `qemu-img convert -c` is **slow and silent** — the script below adds a
progress bar (`-p`) and per-phase echoes so it never *looks* hung. It converts
first, then syspreps the copy.

```bash
#!/bin/bash
set -euo pipefail
NAME=soccer-vision-vm
GOLDEN=/var/lib/cloudkeep/images/golden-v1.qcow2
OWNER=app          # the user controld runs as (use 'cloudkeep' if you chose the dedicated user)

command -v virt-sysprep >/dev/null || sudo dnf install -y guestfs-tools
virsh domstate "$NAME" >/dev/null 2>&1 || { echo "ERROR: domain '$NAME' not found — check 'virsh list --all'"; exit 1; }
sudo install -d -o "$OWNER" -g "$OWNER" /var/lib/cloudkeep/images

echo "==> shutting down $NAME (waiting for power-off; up to ~2 min)…"
if [ "$(virsh domstate "$NAME")" != "shut off" ]; then
  virsh shutdown "$NAME" || true
  for i in $(seq 1 60); do
    [ "$(virsh domstate "$NAME")" = "shut off" ] && break
    sleep 2
  done
  [ "$(virsh domstate "$NAME")" = "shut off" ] || { echo "==> forcing off"; virsh destroy "$NAME"; }
fi

DISK=$(virsh domblklist "$NAME" --details | awk '/disk/{print $4; exit}')
echo "==> converting $DISK -> $GOLDEN (several minutes):"
sudo qemu-img convert -p -O qcow2 -c "$DISK" "$GOLDEN"

echo "==> sysprep (stripping identity)…"
# If libguestfs errors on its appliance: sudo LIBGUESTFS_BACKEND=direct virt-sysprep -a "$GOLDEN"
sudo virt-sysprep -a "$GOLDEN"

sudo chown "$OWNER:$OWNER" "$GOLDEN"
sudo chmod 0444 "$GOLDEN"                                     # never boot directly
sudo restorecon -v "$GOLDEN"                                 # CentOS: virt_image_t (HOSTSETUP §1.1)
echo "==> done:"; qemu-img info "$GOLDEN"
```

> The original `soccer-vision-vm` domain is left intact (bootable fallback).
> Once clones work, you may retire it: `virsh undefine soccer-vision-vm --nvram`.

`config.py` points `GOLDEN_IMAGE` at `/var/lib/cloudkeep/images/golden-v1.qcow2`.
Per-VM overlays are created from it automatically by controld.

## 4. Versioning

Treat the image as immutable and versioned. To update the base (security
patches, new tools): clone `golden-v1` to a scratch VM, change it, re-sysprep,
save as `golden-v2.qcow2`, and bump `GOLDEN_IMAGE`. New VMs use the new base;
existing overlays keep their old backing file untouched.
