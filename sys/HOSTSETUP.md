# CloudKeep v2 — KVM host setup runbook

Bring the KVM host up as the **control plane + compute** for the self-service
fleet. Run blocks in order on the host unless noted. Downtime is expected and
fine. Companion docs: `GOLDEN-IMAGE.md` (the template VM), `cloudkeep` (NGINX),
and the systemd units in `systemd/`.

> **Host OS: CentOS Stream 10** (RHEL family) — commands here use `dnf`,
> `firewalld`/`nftables`, and account for **SELinux (sVirt)**. The *guest*
> (the Golden Image VM) is Ubuntu 24.04, so `GOLDEN-IMAGE.md`'s in-guest
> commands stay `apt`/`ufw`. Two RHEL-isms below are the ones that silently
> break provisioning if skipped: the **SELinux file context** on the image
> pool (§1.1) and the **nwfilter package** the anti-spoof filter needs (§1).

Target layout:

```
Browser ⇄ EC2 NGINX ⇄ WireGuard (one tunnel) ⇄ HOST controld :8000 ⇄ VM_ip:5901
                                                 libvirt/QEMU
                                                 ckbr0 10.20.0.0/24 (isolated, NAT)
```

---

## 1. Virtualisation stack + service identity

> **Reference host:** QEMU/KVM + libvirt are already installed and proven
> (existing VMs run today) — you mainly need the extra packages (esp.
> `libvirt-daemon-config-nwfilter` and `python3-libvirt`), the service user,
> and the directories. Fresh deployments run it all.

```bash
sudo dnf install -y qemu-kvm libvirt virt-install edk2-ovmf guestfs-tools \
                    nftables wireguard-tools python3 python3-pip python3-libvirt \
                    libvirt-daemon-config-nwfilter libvirt-daemon-config-network

# CentOS 10 uses libvirt MODULAR daemons (socket-activated). Enable the sockets
# controld needs (qemu:///system, networks, storage, nwfilter).
sudo systemctl enable --now virtqemud.socket virtnetworkd.socket \
                            virtstoraged.socket virtnwfilterd.socket
# (A compat `libvirtd.socket` also works if you prefer the monolithic daemon.)

# Unprivileged service user for controld (in libvirt + kvm/qemu groups)
sudo useradd -r -m -d /var/lib/cloudkeep -s /sbin/nologin -G libvirt,kvm cloudkeep
# Parent stays traversable (qemu must reach disks under images/); db is private.
sudo install -d -o cloudkeep -g cloudkeep -m 0755 /var/lib/cloudkeep
sudo install -d -o cloudkeep -g cloudkeep -m 0700 /var/lib/cloudkeep/db
sudo install -d -o cloudkeep -g cloudkeep -m 0711 /var/lib/cloudkeep/images
virsh -c qemu:///system list --all      # sanity
```

> `libvirt-daemon-config-nwfilter` provides the `clean-traffic` filter the VM
> XML references for MAC/IP anti-spoof — without it, `virsh define` fails with
> "Referenced filter 'clean-traffic' is missing".

### 1.1 SELinux file context for the image pool (REQUIRED on CentOS)

sVirt runs each QEMU under a confined domain that can only touch disks labeled
`virt_image_t`. Our pool lives outside the default `/var/lib/libvirt/images`, so
we must label it — otherwise VMs fail to start with an SELinux AVC denial.

```bash
sudo semanage fcontext -a -t virt_image_t "/var/lib/cloudkeep/images(/.*)?"
sudo restorecon -Rv /var/lib/cloudkeep/images
# (semanage is in policycoreutils-python-utils: sudo dnf install -y policycoreutils-python-utils)
```

## 2. IOMMU for GPU passthrough

> **Reference host:** already enabled in GRUB and validated with real GPU
> passthrough on previous VMs — nothing to do but verify. Fresh deployments
> need the GRUB edit + reboot.

```bash
# Verify (sufficient on the reference host):
sudo dmesg | grep -iE 'DMAR|IOMMU' | head

# Fresh CentOS hosts only — add kernel args with grubby (no manual grub.cfg edit):
#   Intel:  sudo grubby --update-kernel=ALL --args="intel_iommu=on iommu=pt"
#   AMD:    sudo grubby --update-kernel=ALL --args="amd_iommu=on iommu=pt"
# then: sudo reboot
```

## 3. Move the WireGuard endpoint onto the host

Today the tunnel terminates in the old single VM. Terminate it on the host now.

```bash
wg genkey | sudo tee /etc/wireguard/host.key | wg pubkey | sudo tee /etc/wireguard/host.pub
sudo chmod 600 /etc/wireguard/host.key

sudo tee /etc/wireguard/wg0.conf >/dev/null <<EOF
[Interface]
Address = 10.10.10.2/24
PrivateKey = $(sudo cat /etc/wireguard/host.key)
MTU = 1420

[Peer]
PublicKey = <EC2_PUBLIC_KEY>
Endpoint = <EC2_PUBLIC_IP>:51820
AllowedIPs = 10.10.10.1/32
PersistentKeepalive = 25
EOF

sudo systemctl enable --now wg-quick@wg0
ping -c3 10.10.10.1
```

Host firewall (admitting `:8000` from the EC2 peer) is handled in §4.1 — on
CentOS we use a single nftables ruleset in place of firewalld, so all host
input + guest egress rules live in one file.

Then update EC2 (see §8) with the host's new public key.

## 4. Isolated guest network `ckbr0`

VMs get internet (NAT out via the host's uplink) but are walled off from the
host, the LAN, the tunnel, and each other.

```bash
cat <<'EOF' | sudo virsh net-define /dev/stdin
<network>
  <name>ckbr0</name>
  <forward mode='nat'/>
  <bridge name='ckbr0' stp='on' delay='0'/>
  <port isolated='yes'/>                 <!-- guest<->guest L2 block -->
  <ip address='10.20.0.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='10.20.0.50' end='10.20.0.250'/>
      <!-- controld adds a static <host mac ip name/> per VM -->
    </dhcp>
  </ip>
</network>
EOF
sudo virsh net-autostart ckbr0
sudo virsh net-start ckbr0
```

### 4.1 Firewall — one nftables ruleset (CentOS: replace firewalld)

On a dedicated KVM host we run a single, deterministic nftables ruleset for
**both** host input protection and guest egress, rather than layering custom
rules under firewalld (whose forward policies and zones fight the libvirt NAT
path). libvirt manages its own network table separately; we never touch it.

Two things are load-bearing:
- **`priority -10` on `forward`** runs our drops *before* libvirt's accepts.
- **`ct state established,related accept` in `input`** lets the host→guest VNC
  connection's return packets back in (without it, desktops never load).
- We **do not `flush ruleset`** — that would wipe libvirt's network rules.
  `table; delete table;` makes re-applying our own table idempotent only.

```bash
sudo systemctl disable --now firewalld 2>/dev/null || true   # we own the ruleset now

sudo tee /etc/sysconfig/nftables.conf >/dev/null <<'EOF'
#!/usr/sbin/nft -f
# Idempotent re-create of ONLY our table (leaves libvirt's table intact).
table inet cloudkeep
delete table inet cloudkeep
table inet cloudkeep {

  # ---- Host input protection (this replaces firewalld) ----
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    ct state invalid drop
    iif "lo" accept
    meta l4proto { icmp, ipv6-icmp } accept

    tcp dport 22 accept                         # SSH (restrict to a mgmt iface if you like)
    udp dport 51820 accept                      # WireGuard underlay

    iifname "wg0" ip saddr 10.10.10.1 tcp dport 8000 accept   # controld API: EC2 peer only

    # Guests may reach the host ONLY for DHCP + DNS (libvirt dnsmasq on ckbr0).
    iifname "ckbr0" udp dport { 53, 67 } accept
    iifname "ckbr0" tcp dport 53 accept
    # everything else (incl. guests hitting :8000, :22) falls through to policy drop
  }

  # ---- Guest egress (the isolation boundary) ----
  chain forward {
    type filter hook forward priority -10; policy accept;
    ct state established,related accept
    # Guests reach the internet ONLY; all RFC1918 (LAN, WG net, host nets) is dead.
    iifname "ckbr0" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } drop
    iifname "ckbr0" oifname "UPLINK" accept     # <-- set UPLINK to your internet NIC
    iifname "ckbr0" drop                        # any other egress path (wg0, ...)
  }
}
EOF

# Set your real internet NIC, then load + verify
UPLINK=$(ip route get 1.1.1.1 | awk '{print $5; exit}')
sudo sed -i "s/UPLINK/$UPLINK/" /etc/sysconfig/nftables.conf
sudo systemctl enable --now nftables
sudo nft -f /etc/sysconfig/nftables.conf
sudo nft list table inet cloudkeep             # verify it loaded
```

> SSH is allowed on all interfaces here for first setup — tighten `tcp dport 22`
> to your management interface/subnet once you're in. `meta l4proto icmp` keeps
> ping/diagnostics working (the §9 reachability tests rely on it).

## 5. Storage pool

```bash
sudo virsh pool-define-as cloudkeep dir --target /var/lib/cloudkeep/images
sudo virsh pool-autostart cloudkeep
sudo virsh pool-start cloudkeep
# Decide the host disk reserve (default 50 GB in config.py: RESERVE_DISK_GB).
```

## 6. Golden image

See **`GOLDEN-IMAGE.md`** — build a minimal Ubuntu 24.04 into
`/var/lib/cloudkeep/images/golden-v1.qcow2`. Required before any VM can be built.

## 7. Deploy controld

```bash
sudo install -d -o cloudkeep -g cloudkeep /opt/cloudkeep
sudo -u cloudkeep git clone <repo> /opt/cloudkeep/repo        # or rsync the tree
sudo -u cloudkeep cp -r /opt/cloudkeep/repo/backend/src /opt/cloudkeep/src

# --system-site-packages lets the venv import the dnf-installed python3-libvirt,
# so there's NO compiler/libvirt-devel build. Install everything else from PyPI.
sudo -u cloudkeep python3 -m venv --system-site-packages /opt/cloudkeep/venv
grep -v '^libvirt-python' /opt/cloudkeep/repo/backend/requirements.txt \
  | sudo -u cloudkeep /opt/cloudkeep/venv/bin/pip install -r /dev/stdin
sudo -u cloudkeep /opt/cloudkeep/venv/bin/python -c "import libvirt; print('libvirt', libvirt.getVersion())"

# Secret (rotate from any value that ever touched git history)
printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 32)" | sudo -u cloudkeep tee /opt/cloudkeep/src/.env >/dev/null
sudo chmod 600 /opt/cloudkeep/src/.env

# Seed at least one user (interactive password). Add --admin for an admin.
sudo -u cloudkeep /opt/cloudkeep/venv/bin/python /opt/cloudkeep/src/seed_user.py alice --admin

# Service
sudo cp /opt/cloudkeep/repo/sys/systemd/cloudkeep-controld.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloudkeep-controld.service
systemd-analyze security cloudkeep-controld.service
curl -s http://10.10.10.2:8000/health        # {"status":"ok","libvirt_ok":true,...}
```

> **Tip — test the control plane without VMs:** set `LIBVIRT_URI=fake` in `.env`
> to run with the in-memory FakeProvisioner (login, quotas, the whole lifecycle
> API work; no real KVM). Switch back to `qemu:///system` for the real thing.

## 8. EC2 edge (two lines, then static forever)

```bash
# On EC2 /etc/wireguard/wg0.conf: set the peer PublicKey to the HOST's host.pub
sudo systemctl restart wg-quick@wg0
# Deploy the updated vhost (the /ck/ proxy: /vms, /resources, the /ck/ws VNC and
# /ck/console boot-log WebSockets, and DELETE in the method allowlist):
sudo cp sys/cloudkeep /etc/nginx/sites-available/cloudkeep
sudo nginx -t && sudo systemctl reload nginx
# Deploy the frontend:
sudo rsync -a --delete --exclude 'lib/novnc/' frontend/ /var/www/html/cloudkeep/
```

## 9. Acceptance test — isolation is the gate

Build one test VM (via the UI or `POST /ck/vms`), then from **inside** it:

```bash
curl -s ifconfig.me           # internet works  ✅
ping -c2 <your-LAN-gateway>   # MUST fail/timeout
curl -m3 10.20.0.1:8000       # MUST fail (host control plane unreachable)
ping -c2 10.10.10.1           # MUST fail (WG/EC2 unreachable)
ping -c2 <another-test-VM-ip> # MUST fail (tenant isolation)
```

And from a LAN machine: the VM's `10.20.0.x` is unreachable. Re-run this whole
block any time the firewall changes — it is the executable form of the
isolation requirement.

Also verify: a clone boots to a desktop in well under a minute, `vm_ip:5901`
shows XFCE from the host, and two clones have distinct hostnames/machine-ids.
