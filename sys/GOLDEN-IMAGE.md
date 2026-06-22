# CloudKeep — Golden Image build guide (minimal Ubuntu 24.04 LTS)

Builds the **golden image** every user VM is cloned from, from a fresh minimal
Ubuntu 24.04 LTS install. The result is small, fast-booting, secure, and tuned
for CloudKeep's in-guest VNC model.

Design choices (why it's efficient + secure):
- **`Xvnc` is the X server.** TigerVNC renders the desktop straight to RAM — no
  display manager, no Xorg, no emulated-GPU desktop path. Leanest possible stack.
- **XFCE core only**, compositing off — a usable desktop with minimal overhead.
- **No SSH.** VMs are reached only through the CloudKeep portal/VNC. Admin
  recovery is a host-only serial console (`virsh console`), never the network.
- **In-guest VNC on the guest interface** (`localhost=no`), `SecurityTypes=None`
  — safe because UFW admits `5901` only from the host bridge `10.20.0.1`, and the
  portal already authenticated the user.
- **Stateless clones:** snapd and cloud-init removed; identity + disk handled by
  a tiny first-boot unit.

> Two machines: the **host** is CentOS (`virsh`, `dnf`); the **guest** you build
> is Ubuntu (`apt`). Steps say which. Run §0 and §11 on the **host**; §1–§10
> **inside the guest**.

---

## 0. Create the build VM (on the HOST)

Build on `ckbr0` so the guest has NAT internet for `apt`, but stays isolated.

```bash
sudo virt-install \
  --name golden-build \
  --memory 4096 --vcpus 2 \
  --disk path=/var/lib/cloudkeep/images/golden-build.qcow2,size=16,format=qcow2,bus=virtio \
  --network network=ckbr0,model=virtio \
  --graphics vnc,listen=127.0.0.1 \
  --osinfo ubuntu24.04 \
  --boot uefi \
  --cdrom /path/to/ubuntu-24.04.x-live-server-amd64.iso
```

Drive the installer over the host-loopback VNC: `virsh vncdisplay golden-build`
→ point a VNC viewer at `127.0.0.1:<5900+N>`. In the installer:

- Choose **"Ubuntu Server (minimized)"**.
- Create the user **`app`** (this is the desktop user the VNC service runs as).
- **Do NOT** check "Install OpenSSH server".
- Don't add any featured snaps.

After install + reboot, log in at the console as `app` (or use the serial steps
later). Everything below is **inside the guest**.

## 1. Slim the base system (in the GUEST)

```bash
sudo apt update && sudo apt -y full-upgrade

# snapd: heavy, unused on a minimal desktop
sudo systemctl disable --now snapd.service snapd.socket 2>/dev/null || true
sudo apt -y purge snapd && sudo apt-mark hold snapd

# cloud-init: we manage identity/network ourselves; stop it fighting us + speed boot
sudo touch /etc/cloud/cloud-init.disabled

# SSH server: not used (portal/VNC only). Remove if the minimized image has it.
sudo systemctl disable --now ssh 2>/dev/null || true
sudo apt -y purge openssh-server 2>/dev/null || true

# Auto-updates off: clones are ephemeral; keep the GOLDEN patched on rebuild
# instead of every clone re-downloading updates (efficiency).
sudo systemctl disable --now unattended-upgrades 2>/dev/null || true

sudo apt -y autoremove --purge
```

## 2. Install the desktop + VNC (in the GUEST)

Explicit minimal set, `--no-install-recommends` to avoid pulling extras:

```bash
sudo apt install --no-install-recommends -y \
    xfce4-session xfwm4 xfdesktop4 xfce4-panel xfce4-settings \
    thunar xfce4-terminal dbus-x11 \
    tigervnc-standalone-server tigervnc-common \
    x11-xserver-utils fonts-dejavu-core cloud-guest-utils ufw
```

> This is the daily-driver baseline. Bake in more (a browser, editors) only if
> you want a richer default — every package adds image size and boot time.

## 3. Desktop user + sudo (in the GUEST)

`app` is the single-tenant desktop user (the VM belongs to whoever the portal
authenticated). Passwordless sudo so they can manage their own machine:

```bash
echo 'app ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/app
sudo chmod 440 /etc/sudoers.d/app
```

> Security note: this is acceptable because the VM is single-owner, network-
> isolated (no LAN/host reach), and ephemeral — a compromised guest is contained.
> If you'd rather require a password, set one for `app` (`sudo passwd app`) and
> remove the sudoers file; users then type it in the terminal.

## 4. TigerVNC session config (in the GUEST)

```bash
sudo -u app mkdir -p /home/app/.vnc

sudo -u app tee /home/app/.vnc/config >/dev/null <<'EOF'
session=xfce
geometry=1280x800
localhost=no
SecurityTypes=None
AlwaysShared=1
EOF

sudo -u app tee /home/app/.vnc/xstartup >/dev/null <<'EOF'
#!/bin/sh
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
exec dbus-launch --exit-with-session sh -c '
    xfconf-query -c xfwm4 -p /general/use_compositing -s false --create -t bool 2>/dev/null || true
    xfconf-query -c xfwm4 -p /general/box_move        -s true  --create -t bool 2>/dev/null || true
    xfconf-query -c xfwm4 -p /general/box_resize      -s true  --create -t bool 2>/dev/null || true
    exec startxfce4
'
EOF
sudo chmod 755 /home/app/.vnc/xstartup
sudo chown -R app:app /home/app/.vnc
```

`localhost=no` + `SecurityTypes=None` = network-reachable, unauthenticated VNC —
safe only because of the firewall in §6. Compositing off is the biggest VNC
efficiency win (fewer/smaller framebuffer updates).

## 5. The VNC service (in the GUEST)

Starts TigerVNC on boot so the desktop is ready when controld polls `5901`.
`--I-KNOW-THIS-IS-INSECURE` is required for `SecurityTypes=None` on a non-loopback
listener; safe here per §6.

```bash
sudo tee /etc/systemd/system/cloudkeep-vnc.service >/dev/null <<'EOF'
[Unit]
Description=CloudKeep in-guest VNC desktop (TigerVNC :1)
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User=app
Group=app
PIDFile=/home/app/.vnc/%H:1.pid
ExecStartPre=-/usr/bin/vncserver -kill :1
ExecStartPre=-/bin/rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
ExecStart=/usr/bin/vncserver :1 -localhost no --I-KNOW-THIS-IS-INSECURE
ExecStop=/usr/bin/vncserver -kill :1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable cloudkeep-vnc.service
```

## 6. Firewall (in the GUEST)

Default-deny inbound; the only opening is VNC from the host bridge. No SSH rule.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 10.20.0.1 to any port 5901 proto tcp
sudo ufw --force enable
sudo ufw status verbose          # expect: deny (incoming) + just the 5901 rule
```

## 7. Networking (in the GUEST)

Match **any** ethernet name so every clone's virtio NIC gets a `ckbr0` lease
(clones may get a different interface name than the build VM).

```bash
sudo rm -f /etc/netplan/50-cloud-init.yaml
sudo tee /etc/netplan/01-cloudkeep.yaml >/dev/null <<'EOF'
network:
  version: 2
  ethernets:
    any:
      match:
        name: "en*"
      dhcp4: true
      dhcp6: false
      optional: true
EOF
sudo chmod 600 /etc/netplan/01-cloudkeep.yaml
sudo netplan generate
```

## 8. First-boot customization (in the GUEST)

Per-clone: grow the disk to the chosen size and give it a unique identity, then
disable itself.

```bash
sudo tee /usr/local/sbin/cloudkeep-firstboot.sh >/dev/null <<'EOF'
#!/bin/bash
set -e
ROOT_DEV=$(findmnt -no SOURCE /)
DISK=/dev/$(lsblk -no PKNAME "$ROOT_DEV")
PART=$(echo "$ROOT_DEV" | grep -o '[0-9]*$')
growpart "$DISK" "$PART" || true
resize2fs "$ROOT_DEV" || true
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

## 9. Recovery console (in the GUEST)

With SSH gone, the only way into a misbehaving clone is the host-only serial
console (`virsh console vm-N`). Enable a getty on it:

```bash
sudo systemctl enable serial-getty@ttyS0.service
```

This is reachable only from the host via libvirt — never over the network.

## 10. Final tidy + power off (in the GUEST)

```bash
sudo apt -y autoremove --purge && sudo apt clean
sudo journalctl --rotate && sudo journalctl --vacuum-time=1s
cat /dev/null > ~/.bash_history && history -c
sudo poweroff
```

## 11. Seal into the template (on the HOST)

```bash
#!/bin/bash
set -euo pipefail
NAME=golden-build
GOLDEN=/var/lib/cloudkeep/images/golden-v1.qcow2
OWNER=app

command -v virt-sysprep >/dev/null || sudo dnf install -y guestfs-tools
virsh domstate "$NAME" >/dev/null 2>&1 || { echo "ERROR: domain '$NAME' not found"; exit 1; }
sudo install -d -o "$OWNER" -g "$OWNER" /var/lib/cloudkeep/images

echo "==> ensuring $NAME is shut off…"
if [ "$(virsh domstate "$NAME")" != "shut off" ]; then
  virsh shutdown "$NAME" || true
  for i in $(seq 1 60); do [ "$(virsh domstate "$NAME")" = "shut off" ] && break; sleep 2; done
  [ "$(virsh domstate "$NAME")" = "shut off" ] || virsh destroy "$NAME"
fi

DISK=$(virsh domblklist "$NAME" --details | awk '/disk/{print $4; exit}')
echo "==> converting $DISK -> $GOLDEN:"
sudo qemu-img convert -p -O qcow2 -c "$DISK" "$GOLDEN"

echo "==> sysprep…"
sudo virt-sysprep -a "$GOLDEN"     # if appliance error: prefix LIBGUESTFS_BACKEND=direct
sudo chown "$OWNER:$OWNER" "$GOLDEN"; sudo chmod 0444 "$GOLDEN"; sudo restorecon -v "$GOLDEN"
echo "==> done:"; qemu-img info "$GOLDEN"

# Optional: retire the build VM (the template file is all controld needs)
# virsh undefine golden-build --nvram
# rm -f /var/lib/cloudkeep/images/golden-build.qcow2
```

`config.py` already points `GOLDEN_IMAGE` at
`/var/lib/cloudkeep/images/golden-v1.qcow2`.

## 12. Verification (build a real VM)

In the portal, click **+ build**. On success controld logs
`provisioned vm-N -> 10.20.0.x:5901` and the card flips to **ready**. Inside a
clone (via `virsh console vm-N`, login `app`):

```bash
ss -ltn | grep 5901           # 0.0.0.0:5901
systemctl is-active cloudkeep-vnc      # active
sudo ufw status | grep 5901   # ALLOW from 10.20.0.1
ip a                          # has a 10.20.0.x lease
```

## Versioning + patching

Treat the image as immutable and versioned. To ship security patches or new
defaults: boot `golden-build` again (or clone `golden-v1`), `apt full-upgrade`,
re-seal as `golden-v2.qcow2`, and bump `GOLDEN_IMAGE`. New VMs use the new base;
existing overlays keep their old backing file. This keeps clones lean (no
per-clone auto-updates) while staying patched.
