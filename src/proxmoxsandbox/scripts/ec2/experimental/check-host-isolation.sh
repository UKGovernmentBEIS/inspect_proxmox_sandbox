#!/bin/bash
# Run *on the host*. Checks the guest-isolation mechanism from ../userdata.sh: the units
# are installed, enabled and last ran OK (guards against stale AMIs), and the rules they
# install are loaded. check-guest-isolation.sh probes the effect from inside a guest; run
# both — this one ends by printing the guest command line to paste.
# One PASS/SKIP line per check; exits at the first failure.
#
# Usage: check-host-isolation.sh [connected|lockdown|isolated]
#   connected  (default) an ordinary host: guests get NAT and upstream DNS
#   lockdown   the guest egress lockdown marker is armed by hand (see CONTRIBUTING.md)
#   isolated   launched --no-internet: lockdown, plus the isolated VPC's AWS controls
# shellcheck disable=SC2329  # the check helpers are invoked indirectly, via chk
set -uo pipefail

usage() { echo "usage: $0 [connected|lockdown|isolated]" >&2; exit 2; }
mode=${1:-connected}
[ $# -le 1 ] || usage
case "$mode" in connected|lockdown|isolated) ;; *) usage ;; esac

chk() {
    local name=$1
    shift
    if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; exit 1; fi
}
skip() { echo "SKIP  $1 ($2)"; }

unit_ok() { systemctl is-enabled -q "$1" && [ "$(systemctl show -p Result --value "$1")" = success ]; }
pvefw_running() { pve-firewall status | grep -q enabled/running; }
on_failure_is() { [ "$(systemctl show -p OnFailure --value "$1")" = "$2" ]; }
not_masked() {
    local unit
    for unit in "$@"; do
        case "$(systemctl is-enabled "$unit" 2>/dev/null)" in masked*) return 1 ;; esac
    done
    return 0
}
sysctl_is() { [ "$(sysctl -n "$1" 2>/dev/null)" = "$2" ]; }
# Presence only. Ordering against a -j PVEFW-FORWARD jump is not decidable here, which is
# why check-guest-isolation.sh is the arbiter of effect.
has_rule() { iptables -w -t "$1" -S "$2" 2>/dev/null | grep -q -- "$3"; }
has_rule6() { ip6tables -w -S "$1" 2>/dev/null | grep -q -- "$2"; }
is_private() {
    case "$1" in
        10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    esac
    return 1
}
http_code() { curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
connects() { [ "$(http_code "$1")" != 000 ]; }
no_connect() { [ "$(http_code "$1")" = 000 ]; }
resolves_to() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1; exit}'; }
unresolvable() { [ -z "$(resolves_to "$1")" ]; }

nic=$(ip route show default | awk '{print $5; exit}')
node=$(hostname)
echo "host $node, mode $mode, mgmt NIC ${nic:-none}, kernel $(uname -r), $(pveversion 2>/dev/null | head -1)"

echo
echo "# units (stale-AMI guard)"
chk "host firewall unit: proxmox-ami-fixup-firewall.service" unit_ok proxmox-ami-fixup-firewall.service
chk "pve-firewall enabled and running" pvefw_running
chk "IMDS / link-local forwarding block: inspect-proxmox-block-cloud-metadata.service" unit_ok inspect-proxmox-block-cloud-metadata.service
chk "guest NAT/FORWARD rules: proxmox-ami-fixup-nat.service" unit_ok proxmox-ami-fixup-nat.service
chk "egress lockdown (internet+DNS): inspect-proxmox-egress-lockdown.service" unit_ok inspect-proxmox-egress-lockdown.service
chk "egress lockdown re-armed periodically: inspect-proxmox-egress-lockdown.timer" systemctl is-active -q inspect-proxmox-egress-lockdown.timer
chk "egress lockdown fails deadly: OnFailure=inspect-proxmox-egress-lockdown-halt.service" \
    on_failure_is inspect-proxmox-egress-lockdown.service inspect-proxmox-egress-lockdown-halt.service
# Masked means the halt unit fired at some point, so the rules can look right now even
# though a lockdown run failed earlier.
chk "Proxmox API not masked by the halt unit: pveproxy, pvedaemon" not_masked pveproxy.service pvedaemon.service

echo
echo "# forwarding rules and kernel state"
chk "link-local destinations dropped: raw PREROUTING -d 169.254.0.0/16 -j DROP" \
    has_rule raw PREROUTING "-d 169.254.0.0/16 -j DROP"
chk "link-local sources dropped: FORWARD -s 169.254.0.0/16 -j DROP" \
    has_rule filter FORWARD "-s 169.254.0.0/16 -j DROP"
chk "forwarded IPv6 dropped: ip6tables FORWARD -j DROP" has_rule6 FORWARD "-A FORWARD -j DROP"
chk "IPv6 off for interfaces created after boot (SDN vnets): net.ipv6.conf.default.disable_ipv6" \
    sysctl_is net.ipv6.conf.default.disable_ipv6 1
chk "IPv4 forwarding on (guests reach their gateway): net.ipv4.ip_forward" sysctl_is net.ipv4.ip_forward 1
# If the guest NAT path is missing, guest-side egress probes pass for the wrong reason.
chk "guest NAT path present: nat POSTROUTING -s 10.10.10.0/24 -j MASQUERADE" \
    has_rule nat POSTROUTING "-s 10.10.10.0/24 .*-j MASQUERADE"

echo
echo "# Proxmox firewall (host services reachable only on the mgmt NIC)"
node_rules=$(pvesh get "/nodes/$node/firewall/rules" --output-format json 2>/dev/null || echo '[]')
cluster_rules=$(pvesh get /cluster/firewall/rules --output-format json 2>/dev/null || echo '[]')
fw_enabled() { [ "$(pvesh get "$1/firewall/options" --output-format json 2>/dev/null | jq -r '.enable')" = 1 ]; }
have_accept() {
    jq -e --arg p "$1" --arg d "$2" --arg i "$3" \
        'any(.[]; .type == "in" and .action == "ACCEPT" and ((.enable // 1) | tonumber) == 1
             and .proto == $p and ((.dport // "") | tostring) == $d and .iface == $i)' \
        <<<"$node_rules" >/dev/null
}
# Anything an inbound ACCEPT opens without an --iface is open on the SDN gateway too,
# i.e. to guests. Only the SDN DNS/DHCP ports are meant to be.
no_unexpected_unbound() {
    ! jq -e 'any(.[]; .type == "in" and .action == "ACCEPT" and ((.iface // "") == "")
                 and ((((.proto // "") + "/" + ((.dport // "") | tostring)))
                      | IN("udp/53", "tcp/53", "udp/67") | not))' <<<"$1" >/dev/null
}
chk "cluster firewall enabled" fw_enabled /cluster
chk "node firewall enabled" fw_enabled "/nodes/$node"
chk "API accepted only on the mgmt NIC: tcp/8006 iface=$nic" have_accept tcp 8006 "$nic"
chk "SSH accepted only on the mgmt NIC: tcp/22 iface=$nic" have_accept tcp 22 "$nic"
chk "no unbound inbound ACCEPT beyond SDN DNS/DHCP on the node" no_unexpected_unbound "$node_rules"
chk "no unbound inbound ACCEPT beyond SDN DNS/DHCP on the cluster" no_unexpected_unbound "$cluster_rules"

echo
echo "# guest egress lockdown"
no_upstream_resolver() { ! grep -q "^nameserver" /run/dnsmasq/resolv.conf; }
if [ "$mode" = connected ]; then
    # The connected case is the negative control for the guest script's DNS probes: if
    # dnsmasq has no upstream here either, "DNS tunnelling fails" means nothing.
    chk "SDN dnsmasq points at the VPC resolver: /run/dnsmasq/resolv.conf" \
        grep -q "^nameserver 169.254.169.253" /run/dnsmasq/resolv.conf
else
    chk "opt-in marker present: /etc/inspect-proxmox-egress-lockdown" test -f /etc/inspect-proxmox-egress-lockdown
    chk "guest egress dropped: mangle FORWARD -o $nic" has_rule mangle FORWARD "-o $nic .*-j DROP"
    chk "guest ingress dropped: mangle FORWARD -i $nic" has_rule mangle FORWARD "-i $nic .*-j DROP"
    chk "dnsmasq upstream queries dropped: mangle OUTPUT --uid-owner dnsmasq" \
        has_rule mangle OUTPUT "-o $nic .*--uid-owner .*-j DROP"
    chk "no upstream resolver for SDN dnsmasq: /run/dnsmasq/resolv.conf" no_upstream_resolver
fi

echo
echo "# AWS-level controls, as seen from the host"
endpoint_ok() { is_private "$1" && connects "https://$2/"; }
if [ "$mode" != isolated ]; then
    skip "interface endpoints and DNS firewall" "mode is $mode, not isolated"
    chk "host has internet (negative control for the guest egress probes)" connects https://deb.debian.org/
else
    region=$(/usr/local/bin/call-ec2-hypervisor latest/meta-data/placement/region 2>/dev/null)
    chk "region from IMDS" test -n "$region"
    endpoints=""
    # The host keeps these four (SSM is the operator's way in); a guest must not be able
    # to reach them at all, which is what the guest script's address arguments probe.
    for svc in ssm ssmmessages ec2messages monitoring; do
        name="$svc.$region.amazonaws.com"
        ip=$(resolves_to "$name")
        chk "interface endpoint $svc: $name resolves" test -n "$ip"
        chk "interface endpoint $svc: $ip private and answering on 443" endpoint_ok "$ip" "$name"
        endpoints="$endpoints $ip"
    done
    chk "DNS firewall NXDOMAINs everything else: deb.debian.org does not resolve" unresolvable deb.debian.org
    chk "no route off the VPC: https://1.1.1.1 does not connect" no_connect https://1.1.1.1/
    echo
    echo "# paste into the guest run (endpoint IPs are per-VPC; do not commit them):"
    echo "  check-guest-isolation.sh $mode$endpoints"
fi

echo
echo "all checks passed ($mode)"
