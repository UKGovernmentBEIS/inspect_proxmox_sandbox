#!/usr/bin/env bash
# Wall sandbox VMs off from the Proxmox host's own services.
#
# Run this ON the Proxmox node (it uses `pvesh` and the live routing table).
# It is the reference implementation for host isolation; the provisioning
# scripts (scripts/virtualized_proxmox/build_proxmox_auto.sh's on-first-boot
# heredoc, and scripts/ec2/userdata.sh) inline the same steps. If you provision
# Proxmox some other way, run this once per host instead.
#
# Why: a sandbox VM can otherwise reach pveproxy (8006), SSH (22) and other host
# services via the SDN gateway IP, the vmbr0 IP, or the host's external NIC. This
# enables Proxmox's own cluster + node firewall and accepts the host management
# ports ONLY on the interface carrying the default route -- where external
# API/SSH callers arrive. Sandbox VMs sit on separate SDN vnet bridges, so their
# packets ingress on a different interface and hit the default-deny INPUT policy,
# even when they target the host's own management IP. DNS/DHCP from VMs is
# accepted on any interface so the SDN dnsmasq keeps working.
#
# The default-route interface is the ground truth for "where management traffic
# arrives" and is correct on every topology: a dedicated NIC (e.g. enp39s0/ens5)
# or a bridge that owns the IP (e.g. vmbr0 over a physical port). Reading it from
# the live routing table -- rather than guessing from config -- is why this lives
# on the host at provision time, not in the eval-time API client.
#
# Set INSPECT_PROXMOX_SKIP_HOST_ISOLATION=1 to skip (NOT recommended -- re-exposes
# the API to sandbox VMs).
set -euo pipefail

if [ "${INSPECT_PROXMOX_SKIP_HOST_ISOLATION:-}" = "1" ]; then
    echo "INSPECT_PROXMOX_SKIP_HOST_ISOLATION=1: leaving host firewall untouched"
    exit 0
fi

COMMENT="inspect-proxmox-sandbox: host-isolation"
NODE=$(hostname)
IFACE=$(ip -o route show default | awk '{print $5}' | head -1)

if [ -z "$IFACE" ]; then
    echo "ERROR: no default route found; cannot determine the management interface." >&2
    echo "Refusing to enable the firewall (would risk locking out the API)." >&2
    exit 1
fi
echo "Host isolation: management interface = $IFACE (node $NODE)"

# Idempotent: skip any rule we've already added (matched by our comment + dport).
existing_dports=$(pvesh get "/nodes/$NODE/firewall/rules" --output-format json \
    | python3 -c 'import sys,json; print(" ".join(r.get("dport","") for r in json.load(sys.stdin) if r.get("comment")=="'"$COMMENT"'"))')

add_rule() {  # proto dport [iface]
    local proto=$1 dport=$2 iface=${3:-}
    for d in $existing_dports; do
        [ "$d" = "$dport" ] && { echo "  rule for $proto/$dport already present"; return; }
    done
    local args=(--type in --action ACCEPT --proto "$proto" --dport "$dport" \
        --enable 1 --comment "$COMMENT")
    [ -n "$iface" ] && args+=(--iface "$iface")
    pvesh create "/nodes/$NODE/firewall/rules" "${args[@]}"
    echo "  added IN ACCEPT $proto/$dport ${iface:+iface=$iface}"
}

# Management ports: only on the interface external callers arrive on.
add_rule tcp 8006 "$IFACE"
add_rule tcp 22 "$IFACE"
# DNS + DHCP for the SDN dnsmasq: any interface (VMs lease/resolve via the gateway).
add_rule udp 53
add_rule tcp 53
add_rule udp 67

# Enable last, so the management ACCEPTs are in place before the default-deny
# policy takes effect.
pvesh set "/nodes/$NODE/firewall/options" --enable 1
pvesh set /cluster/firewall/options --enable 1

echo "Host isolation applied: cluster + node firewall enabled; management ports"
echo "accepted on $IFACE only; sandbox VMs (on SDN bridges) default-denied."
