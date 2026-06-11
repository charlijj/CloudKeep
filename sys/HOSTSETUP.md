# CloudKeep v2 — KVM host setup runbook

Bring the KVM host up as the **control plane + compute** for the self-service
fleet. Run blocks in order on the host unless noted. Downtime is expected and
fine. Companion docs: `GOLDEN-IMAGE.md` (the template VM), `cloudkeep` (NGINX),
and the systemd units in `systemd/`.

Target layout:

```
Browser ⇄ EC2 NGINX ⇄ WireGuard (one tunnel) ⇄ HOST controld :8000 ⇄ VM_ip:5901
                                                 libvirt/QEMU
                                                 ckbr0 10.20.0.0/24 (isolated, NAT)
```

---

## 1. Virtualisation stack + service identity

```bash
sudo apt update
sudo apt install -y qemu-kvm libvirt-daemon-system virtinst ovmf guestfs-tools nftables python3-venv
sudo systemctl enable --now libvirtd

# Unprivileged service user for controld (in libvirt + kvm groups)
sudo useradd -r -m -d /var/lib/cloudkeep -s /usr/sbin/nologin -G libvirt,kvm cloudkeep
sudo install -d -o cloudkeep -g cloudkeep -m 0700 /var/lib/cloudkeep/db /var/lib/cloudkeep/images
virsh -c qemu:///system list --all      # sanity
```

## 2. IOMMU for future GPU passthrough (the only reboot)

Cheap to enable now so Phase D needs no reboot window. Don't bind anything to
`vfio-pci` yet.

```bash
# /etc/default/grub: add to GRUB_CMDLINE_LINUX_DEFAULT
#   Intel:  intel_iommu=on iommu=pt
#   AMD:    amd_iommu=on  iommu=pt
sudo update-grub && sudo reboot
# after reboot:
sudo dmesg | grep -iE 'DMAR|IOMMU' | head
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

# Admit the control port only from the EC2 peer
sudo ufw allow in on wg0 from 10.10.10.1 to any port 8000 proto tcp
```

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

### 4.1 Egress firewall (the isolation boundary)

The `priority -10` is load-bearing: it runs our drops **before** libvirt's own
permissive forward/accept rules (priority 0). The `ct state established,related
accept` in BOTH chains is also required — without it in `guest_input`, the
host→guest VNC connection's return packets get dropped and desktops never load.

```bash
sudo tee /etc/nftables.d/cloudkeep.nft >/dev/null <<'EOF'
table inet cloudkeep {
  chain guest_fwd {
    type filter hook forward priority -10; policy accept;
    ct state established,related accept
    # Guests reach the internet ONLY. All RFC1918 (your LAN, WG net, host nets) is dead.
    iifname "ckbr0" ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } drop
    iifname "ckbr0" oifname "eno1" accept           # <-- set eno1 to your uplink
    iifname "ckbr0" drop                            # any other egress path (wg0, ...)
    oifname "ckbr0" drop                            # nothing initiates INTO guests
  }
  chain guest_input {
    type filter hook input priority -10; policy accept;
    ct state established,related accept              # return traffic for host->guest VNC
    iifname "ckbr0" udp dport { 53, 67 } accept      # DHCP + DNS (libvirt dnsmasq)
    iifname "ckbr0" tcp dport 53 accept
    iifname "ckbr0" drop                            # :8000, :22, everything else
  }
}
EOF
# include it from /etc/nftables.conf, then:
echo 'include "/etc/nftables.d/cloudkeep.nft"' | sudo tee -a /etc/nftables.conf
sudo systemctl enable --now nftables
sudo nft -f /etc/nftables.conf
sudo nft list table inet cloudkeep            # verify it loaded
```

> Replace `eno1` with the host's actual internet-facing interface (`ip route get 1.1.1.1`).

## 5. Storage pool

```bash
sudo virsh pool-define-as cloudkeep dir --target /var/lib/cloudkeep/images
sudo virsh pool-autostart cloudkeep
sudo virsh pool-start cloudkeep
# Decide the host disk reserve (default 50 GB in config.py: RESERVE_DISK_GB).
```

## 6. Golden image

See **`GOLDEN-IMAGE.md`** — convert the existing soccer-vision-vm into
`/var/lib/cloudkeep/images/golden-v1.qcow2`. Required before any VM can be built.

## 7. Deploy controld

```bash
sudo install -d -o cloudkeep -g cloudkeep /opt/cloudkeep
sudo -u cloudkeep git clone <repo> /opt/cloudkeep/repo        # or rsync the tree
sudo -u cloudkeep cp -r /opt/cloudkeep/repo/backend/src /opt/cloudkeep/src
sudo -u cloudkeep python3 -m venv /opt/cloudkeep/venv
sudo -u cloudkeep /opt/cloudkeep/venv/bin/pip install -r /opt/cloudkeep/repo/backend/requirements.txt

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
# Deploy the updated vhost (adds the generic /ck/ proxy for /vms, /resources):
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
