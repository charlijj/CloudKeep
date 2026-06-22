# CloudKeep — Golden Image build guide (minimal Ubuntu 24.04 LTS)

Builds the **golden image** every user VM is cloned from, from a fresh minimal
Ubuntu 24.04 LTS install. The result is small, fast-booting, secure, and tuned
for CloudKeep's in-guest VNC model.

Design choices (why it's efficient + secure):
- **`Xvnc` is the X server.** TigerVNC renders the desktop straight to RAM — no
  display manager, no Xorg, no emulated-GPU desktop path. Leanest possible stack.
- **XFCE core only**, compositing off — a usable desktop with minimal overhead.
- **No SSH.** VMs are reached only through the CloudKeep portal/VNC. Admin
  diagnostics use the host-side QEMU console, never the network.
- **Unprivileged by default.** The desktop user has **no sudo** — it cannot edit
  `/etc`, change the firewall, or stop services. Users install/remove their own
  apps via **Flatpak (user scope)**; Firefox ships pre-installed.
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

## 2. Install the desktop, browser + VNC (in the GUEST)

Minimal set, `--no-install-recommends` to avoid extras:

```bash
sudo apt install --no-install-recommends -y \
    xfce4-session xfwm4 xfdesktop4 xfce4-panel xfce4-settings \
    thunar xfce4-terminal dbus-x11 \
    tigervnc-standalone-server tigervnc-common \
    x11-xserver-utils fonts-dejavu-core cloud-guest-utils ufw \
    flatpak \
    libgl1-mesa-dri libglx-mesa0 libegl-mesa0 mesa-utils
# Optional base CLI tools so users have them without root:
#   sudo apt install --no-install-recommends -y git curl python3 nano
```

> **The Mesa packages are not optional.** `Xvnc` has no GPU, and modern Firefox
> (and VS Code, Electron apps, VLC, …) render exclusively through WebRender,
> which *requires* an OpenGL/EGL context. Without Mesa's software GL
> (`libgl1-mesa-dri` = llvmpipe), the browser's GPU process has nothing to bind
> to and Firefox shows a blank window / fails to open. See §4 for the matching
> session env + Firefox config.

### Firefox — native .deb (no snap)

snapd is gone, so install Firefox from Mozilla's APT repo (a real deb, pinned
above the Ubuntu snap transitional package):

```bash
sudo install -d -m 0755 /etc/apt/keyrings
wget -qO- https://packages.mozilla.org/apt/repo-signing-key.gpg \
  | sudo tee /etc/apt/keyrings/packages.mozilla.org.asc >/dev/null
echo 'deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main' \
  | sudo tee /etc/apt/sources.list.d/mozilla.list >/dev/null
printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' \
  | sudo tee /etc/apt/preferences.d/mozilla >/dev/null
sudo apt update && sudo apt install -y firefox
```

### User-installable apps — no root

Add Flathub so end users install/remove additional GUI apps in **user scope**
(`flatpak install --user …`), without sudo:

```bash
sudo flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
```

> For a clickable app centre add `gnome-software gnome-software-plugin-flatpak`
> (heavier). Otherwise users run `flatpak install --user flathub <app-id>`.

## 3. Desktop user — unprivileged model (in the GUEST)

`app` is the single-tenant desktop user. In the **final image it has no sudo** —
this is the core of the lockdown: with no root it can't edit `/etc`, change the
firewall, stop services, or alter networking. It gets a normal desktop, installs
its own apps via Flatpak (§2), and owns its home directory.

> During the build `app` still needs the installer's sudo to run §1–§8. We strip
> it as the **last** guest action (§10) — don't remove it now or those steps fail.

**Why no sudo (and where the real boundary is):** "install system packages" is
equivalent to root — `apt` runs maintainer scripts as root and `sudo apt install
./x.deb` runs arbitrary root code, so sudo *cannot* be scoped to "just packages".
Hence none. Crucially, the host was never protected by the guest's restrictions:
even a root guest **cannot reach the host or other VMs** — that isolation is
enforced on the *host* (nftables drops guest→host/LAN, the bridge's `port
isolated` blocks VM↔VM, the `clean-traffic` nwfilter pins MAC/IP), none of which
a guest can touch. No-sudo keeps the *guest* in a known-good state; the host's
safety is independent of it.

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
# Force software OpenGL for every app in the session (no GPU under Xvnc).
# This is what lets Firefox/VS Code/etc. get a GL context instead of crashing.
export LIBGL_ALWAYS_SOFTWARE=1
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

### Lock Firefox to software rendering (belt-and-suspenders)

So Firefox's GPU process never probes for (nonexistent) hardware and crash-loops,
pin it to software WebRender system-wide via Firefox's autoconfig:

```bash
sudo tee /usr/lib/firefox/defaults/pref/autoconfig.js >/dev/null <<'EOF'
pref("general.config.filename", "firefox.cfg");
pref("general.config.obscure_value", 0);
EOF
sudo tee /usr/lib/firefox/firefox.cfg >/dev/null <<'EOF'
// (Firefox ignores this first line)
defaultPref("gfx.webrender.software", true);
defaultPref("layers.acceleration.disabled", true);
defaultPref("media.hardware-video-decoding.enabled", false);
defaultPref("gfx.x11-egl.force-enabled", true);
EOF
```

> `defaultPref` (not `lockPref`) sets the safe default but still lets an advanced
> user flip it. With the Mesa packages from §2 installed, Firefox already
> auto-selects software WebRender — this just makes it deterministic.

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

## 9. Diagnostics + recovery (no in-guest admin)

With no SSH and no sudo there's intentionally no privileged shell in a clone.
To inspect one:
- **Watch boot / login**: host-side QEMU console — `virsh vncdisplay vm-N` →
  point a VNC viewer at `127.0.0.1:<5900+N>` on the host (no guest creds needed).
- **Inspect the disk offline**: `guestfish`/`virt-cat` on the host against the
  clone's overlay.
- **Broken clone**: delete and rebuild — clones are ephemeral and cheap.

root stays locked (Ubuntu default) — no baked-in credentials in the image.

## 10. Lock down (in the GUEST) — final guest step

Tidy, then strip the desktop user's privileges as the very last action. Do
**not** power off here — the seal script (§11) shuts the VM down from the host,
which avoids needing sudo after you've removed it.

```bash
sudo apt -y autoremove --purge && sudo apt clean
sudo journalctl --rotate && sudo journalctl --vacuum-time=1s
cat /dev/null > ~/.bash_history

# Make `app` unprivileged (takes effect on every clone's boot):
sudo rm -f /etc/sudoers.d/90-cloud-init-users
sudo gpasswd -d app sudo
```

Now switch to the host and run §11.

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
`provisioned vm-N -> 10.20.0.x:5901` and the card flips to **ready**; Connect
shows the XFCE desktop with Firefox. From the HOST (no guest login needed):

```bash
virsh domifaddr vm-N                                   # has a 10.20.0.x lease
timeout 3 bash -c '</dev/tcp/10.20.0.50/5901' && echo "VNC reachable"
virsh vncdisplay vm-N                                  # emergency console to watch boot
```

In the portal desktop's terminal, confirm three things:

```bash
glxinfo | grep "OpenGL renderer"     # -> "llvmpipe" (software GL works)
sudo -n true                          # MUST fail (app is unprivileged)
flatpak install --user flathub org.gnome.gedit   # works WITHOUT a password prompt
```

Then open **Firefox** and load a page — it must render, not show a blank window.
If it's blank, GL is missing: re-check the Mesa packages (§2) and `glxinfo`.

## Everyday + developer apps

The baseline (browser + terminal + file manager) covers basics; users add the
rest from Flathub in user scope, all of which benefit from the software-GL setup:

- **Email:** `org.mozilla.Thunderbird` (or webmail in Firefox).
- **Code/IDE:** `com.visualstudio.code`, `org.gnome.gitg`; runtimes via user space
  (`pip install --user`, `nvm`, `rustup`).
- **Media/office:** `org.videolan.VLC`, `org.libreoffice.LibreOffice`.

If you want any of these in the *default* image, install them system-wide before
sealing (`sudo flatpak install -y flathub <app-id>`) — at the cost of image size.

## Versioning + patching

Treat the image as immutable and versioned. To ship security patches or new
defaults: boot `golden-build` again (or clone `golden-v1`), `apt full-upgrade`,
re-seal as `golden-v2.qcow2`, and bump `GOLDEN_IMAGE`. New VMs use the new base;
existing overlays keep their old backing file. This keeps clones lean (no
per-clone auto-updates) while staying patched.
