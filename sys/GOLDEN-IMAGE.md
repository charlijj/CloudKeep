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

On the **host**:

```bash
NAME=soccer-vision-vm
virsh shutdown "$NAME"          # wait for it to power off

# Strip identity: machine-id, SSH host keys, logs, leases, etc.
sudo virt-sysprep -d "$NAME"

# Convert (compress) into the immutable golden image used as the backing file.
DISK=$(virsh domblklist "$NAME" --details | awk '/disk/{print $4; exit}')
sudo qemu-img convert -O qcow2 -c "$DISK" /var/lib/cloudkeep/images/golden-v1.qcow2
sudo chown cloudkeep:cloudkeep /var/lib/cloudkeep/images/golden-v1.qcow2
sudo chmod 0444 /var/lib/cloudkeep/images/golden-v1.qcow2   # never boot directly
sudo restorecon -v /var/lib/cloudkeep/images/golden-v1.qcow2  # CentOS: virt_image_t (see HOSTSETUP §1.1)

# Optional: retire the original domain definition (the template file is enough).
virsh undefine "$NAME" --nvram
```

`config.py` points `GOLDEN_IMAGE` at `/var/lib/cloudkeep/images/golden-v1.qcow2`.
Per-VM overlays are created from it automatically by controld.

## 4. Versioning

Treat the image as immutable and versioned. To update the base (security
patches, new tools): clone `golden-v1` to a scratch VM, change it, re-sysprep,
save as `golden-v2.qcow2`, and bump `GOLDEN_IMAGE`. New VMs use the new base;
existing overlays keep their old backing file untouched.
