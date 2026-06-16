# CloudKeep — Golden Image VM

The **Golden Image VM** is, in reality, the existing **soccer-vision-vm** we've
been running all along. We convert it once into an immutable template
(`golden-v1.qcow2`); every user VM is a copy-on-write overlay of it, so clones
take seconds and only consume disk as they diverge.

> ⚠️ This is destructive to soccer-vision-vm as a *running desktop*: sysprep
> strips its identity and it becomes a read-only template. That's intended.
> Snapshot/back it up first if you want a rollback.

## 1. Prepare the desktop stack inside the VM

Boot soccer-vision-vm and, as the `app` user, make sure the in-guest desktop is
clone-ready:

- **TigerVNC** serves XFCE on `:1` (5901). The session config lives in
  `~app/.vnc/{config,xstartup,tigervnc.conf}` (already in this repo under
  `sys/vnc/`). `config` keeps `SecurityTypes=None` — safe because only the host
  gateway can reach 5901 (firewall below).
- Install the in-guest VNC service so every clone starts it on boot:
  ```bash
  sudo cp /path/to/repo/sys/systemd/cloudkeep-vnc.service /etc/systemd/system/
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
  sudo apt install -y unattended-upgrades
  sudo dpkg-reconfigure -plow unattended-upgrades
  # remove any baked-in gateway code, shared passwords, throwaway SSH keys
  ```

## 2. First-boot unit (per-clone customization)

Without this, every clone has the same identity and the user's chosen disk size
won't materialize. Install inside the VM:

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
sudo apt install -y cloud-guest-utils    # provides growpart
```

## 3. Seal it into the template

On the **host**. Two things bite here if you copy the naive version: `virsh
shutdown` is **asynchronous** (you must wait for `shut off`, or QEMU still holds
the disk lock and `qemu-img` fails with *"Failed to get shared write lock"*),
and `virt-sysprep` lives in **`guestfs-tools`** (`dnf install -y guestfs-tools`).
We convert first, then sysprep the *copy*, so the original stays bootable as a
fallback.

```bash
#!/bin/bash
set -euo pipefail
NAME=soccer-vision-vm
GOLDEN=/var/lib/cloudkeep/images/golden-v1.qcow2

# tooling + destination
command -v virt-sysprep >/dev/null || sudo dnf install -y guestfs-tools
sudo install -d -o cloudkeep -g cloudkeep /var/lib/cloudkeep/images

# shut down and WAIT for actual power-off (shutdown is async)
if [ "$(virsh domstate "$NAME")" != "shut off" ]; then
  virsh shutdown "$NAME" || true
  for i in $(seq 1 60); do
    [ "$(virsh domstate "$NAME")" = "shut off" ] && break; sleep 2
  done
  [ "$(virsh domstate "$NAME")" = "shut off" ] || virsh destroy "$NAME"   # force off
fi

# copy the disk into the immutable golden image (no lock now)
DISK=$(virsh domblklist "$NAME" --details | awk '/disk/{print $4; exit}')
sudo qemu-img convert -O qcow2 -c "$DISK" "$GOLDEN"

# strip identity from the COPY (machine-id, SSH host keys, logs, leases, ...)
# If libguestfs errors on its appliance: prefix LIBGUESTFS_BACKEND=direct
sudo virt-sysprep -a "$GOLDEN"

sudo chown cloudkeep:cloudkeep "$GOLDEN"
sudo chmod 0444 "$GOLDEN"                                      # never boot directly
sudo restorecon -v "$GOLDEN"                                  # CentOS: virt_image_t (HOSTSETUP §1.1)
echo "golden image ready: $GOLDEN"
```

> The original `soccer-vision-vm` domain is left intact (bootable fallback).
> Once you've confirmed clones work, you may retire it: `virsh undefine
> soccer-vision-vm --nvram`.

`config.py` points `GOLDEN_IMAGE` at `/var/lib/cloudkeep/images/golden-v1.qcow2`.
Per-VM overlays are created from it automatically by controld.

## 4. Versioning

Treat the image as immutable and versioned. To update the base (security
patches, new tools): clone `golden-v1` to a scratch VM, change it, re-sysprep,
save as `golden-v2.qcow2`, and bump `GOLDEN_IMAGE`. New VMs use the new base;
existing overlays keep their old backing file untouched.
