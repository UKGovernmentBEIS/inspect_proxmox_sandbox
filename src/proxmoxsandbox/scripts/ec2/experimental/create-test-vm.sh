#!/bin/bash
# Create a test VM with guest agent on the SDN network to verify DNS.
# Run this ON the Proxmox host (via run-script-on-host.sh).
set -euxo pipefail

VMID=300
VM_NAME=dns-test
STORAGE=local
ZONE=testzone
VNET=tstvn0
SUBNET=192.168.50.0/24
GW=192.168.50.1
DHCP_START=192.168.50.50
DHCP_END=192.168.50.100

# --- Ensure SSH key exists (for cloud-init and VM access) ---
test -f /root/.ssh/id_ed25519 || ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""

# --- Stop and remove existing VM ---
qm stop "$VMID" 2>/dev/null || true
sleep 2
qm destroy "$VMID" --purge 2>/dev/null || true

# --- Create SDN zone, vnet, subnet (idempotent) ---
pvesh get "/cluster/sdn/zones/$ZONE" &>/dev/null || \
    pvesh create /cluster/sdn/zones \
        --zone "$ZONE" --type simple --dhcp dnsmasq --ipam pve

pvesh get "/cluster/sdn/vnets/$VNET" &>/dev/null || \
    pvesh create /cluster/sdn/vnets \
        --vnet "$VNET" --zone "$ZONE"

# PVE internal subnet ID: <zone>-<network>-<prefix>  (slash replaced by dash)
SUBNET_INTERNAL_ID="$ZONE-${SUBNET//\//-}"
pvesh get "/cluster/sdn/vnets/$VNET/subnets/$SUBNET_INTERNAL_ID" &>/dev/null || \
    pvesh create "/cluster/sdn/vnets/$VNET/subnets" \
        --subnet "$SUBNET" --type subnet \
        --gateway "$GW" \
        --snat 1 \
        --dhcp-range "start-address=$DHCP_START,end-address=$DHCP_END"

pvesh set /cluster/sdn

# Wait for dnsmasq to start for this zone
sleep 5
pgrep -af "dnsmasq.*$ZONE" || echo "WARNING: dnsmasq for $ZONE not running"

echo "=== /run/dnsmasq/resolv.conf ==="
cat /run/dnsmasq/resolv.conf

echo "=== DNS via dnsmasq (pre-VM validation) ==="
host -W3 google.com "$GW"

# --- Download cloud image if not already cached ---
IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
IMG_PATH="/tmp/noble-cloudimg.img"
if [ ! -f "$IMG_PATH" ]; then
    wget -q "$IMG_URL" -O "$IMG_PATH"
fi

# --- Create VM ---
qm create "$VMID" \
    --name "$VM_NAME" \
    --memory 2048 \
    --cores 2 \
    --cpu host \
    --ostype l26 \
    --net0 "virtio,bridge=$VNET" \
    --scsihw virtio-scsi-single \
    --agent enabled=1 \
    --serial0 socket

qm importdisk "$VMID" "$IMG_PATH" "$STORAGE" --format qcow2
qm set "$VMID" \
    --scsi0 "$STORAGE:$VMID/vm-$VMID-disk-0.qcow2" \
    --boot order=scsi0 \
    --ide2 "$STORAGE:cloudinit" \
    --ciuser root \
    --sshkeys /root/.ssh/id_ed25519.pub \
    --ipconfig0 ip=dhcp

qm start "$VMID"

# --- Wait for VM to get a DHCP lease ---
LEASE_FILE="/var/lib/misc/dnsmasq.$ZONE.leases"
echo "Waiting for DHCP lease (up to 120s)..."
VM_IP=""
for i in {1..24}; do
    if [ -f "$LEASE_FILE" ]; then
        VM_IP=$(awk '{print $3}' "$LEASE_FILE" | head -1)
        [ -n "$VM_IP" ] && break
    fi
    sleep 5
done

if [ -z "$VM_IP" ]; then
    echo "ERROR: no DHCP lease after 120s"
    cat "$LEASE_FILE" 2>/dev/null || echo "(lease file missing)"
    exit 1
fi
echo "VM got IP: $VM_IP"

# --- Wait for SSH to be ready ---
echo "Waiting for SSH on $VM_IP..."
for i in {1..24}; do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 -o BatchMode=yes \
           root@"$VM_IP" true 2>/dev/null; then
        echo "SSH ready"
        break
    fi
    sleep 5
done

SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@$VM_IP"

# --- Run DNS tests inside the VM ---
echo "=== resolv.conf inside VM ==="
$SSH cat /etc/resolv.conf

echo "=== DNS lookup from inside VM ==="
$SSH host google.com

echo "=== HTTPS connectivity from inside VM ==="
$SSH curl -sf -o /dev/null -w '%{http_code}\n' https://debian.org

echo "=== ALL TESTS PASSED ==="
