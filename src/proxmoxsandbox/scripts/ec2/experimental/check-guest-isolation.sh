#!/bin/bash
# Run *inside a Linux sandbox guest* (guest agent, `qm terminal`, or paste into a console).
# Probes the effect of the isolation baked by ../userdata.sh: what the guest can actually
# reach. check-host-isolation.sh checks the mechanism on the host; run both, and take this
# script's arguments from the line that one prints.
# One PASS/FAIL/SKIP line per probe. Every probe runs; the exit status is nonzero if any
# failed, and the trailing summary line says how many.
#
# Assumes a guest on a host isolated per the parent README's "Properly isolating the
# host": everything below must be blocked. Elsewhere the egress probes fail by design, because
# the guest can reach the internet — and so do the port-53 and DNS probes, because without the
# lockdown marker the node firewall accepts port 53 and the SDN resolver recurses.
#
# Usage: check-guest-isolation.sh [--host-addr IP] [--unreachable IP[:PORT]] ...
# Both flags repeat; there is no list syntax.
#   --host-addr    an address the Proxmox host holds: its management address, the NAT bridge,
#                  or an SDN gateway. Each gets the full host-service port battery.
#   --unreachable  an address that must not be reachable — a VPC interface endpoint, a host
#                  across a peering link. Port defaults to 443; an IPv6 literal needs
#                  brackets to carry one, as in [fd00::1]:8443.
# Both are site-specific and never hardcoded here; check-host-isolation.sh prints the line
# to paste.
set -uo pipefail

host_addrs=()
unreachable=()
while [ $# -gt 0 ]; do
    case "$1" in
        --host-addr | --unreachable)
            [ $# -ge 2 ] || { echo "$1 needs an address" >&2; exit 2; }
            case "$1" in
                --host-addr) host_addrs+=("$2") ;;
                *) unreachable+=("$2") ;;
            esac
            shift 2
            ;;
        *)
            echo "unknown argument: $1 (every argument takes a --host-addr or" \
                "--unreachable flag)" >&2
            exit 2
            ;;
    esac
done

probes=0
failures=0
want() { # want EXPECTED NAME ACTUAL
    probes=$((probes + 1))
    case "$3" in
        "$1"*) echo "PASS  $2 [$3]" ;;
        *) echo "FAIL  $2 [$3, want $1]"; failures=$((failures + 1)) ;;
    esac
}
skip() { echo "SKIP  $1 ($2)"; }

# Only a completed connect is reachable. How the rest fail is the mechanism's business, not
# this script's: a RST or ICMP error can come from any firewall on the path. 124 is
# timeout(1)'s exit for the kill.
# A probe that could not run must not read as a block: 126/127 is a missing tool, and
# "No such file or directory" against /dev/tcp is a bash built without net redirections.
# bash puts the reason on its first stderr line and a less useful one on the second.
state() { # rc stderr
    local reason=${2%%$'\n'*}
    reason=${reason##*: }
    case "$1:$2" in
        0:*) echo reachable ;;
        124:*) echo "blocked(timeout after 3s)" ;;
        12[67]:* | *"No such file or directory"*) echo "error($reason)" ;;
        *) echo "blocked($reason)" ;;
    esac
}
tcp_state() {
    local err
    err=$(timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>&1)
    state $? "$err"
}
# UDP; the TCP side of port 53 is a tcp_state probe. Any reply is reachable, REFUSED included:
# it means a resolver got the query, which is how a host whose firewall had failed once
# looked, with dnsmasq declining in its place. The query is a hand-built A for deb.debian.org.
dns_state() { # server
    local out rc hdr
    out=$(timeout 3 bash -c "exec 3<>/dev/udp/$1/53 &&
        printf '\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03deb\x06debian\x03org\x00\x00\x01\x00\x01' >&3 &&
        head -c 4 <&3 | od -An -tx1" 2>&1)
    rc=$?
    if ! [[ $out =~ ^[[:space:]]*([0-9a-f]{2}[[:space:]]*){4}$ ]]; then
        # An ICMP error surfaces as a read error in head, which the pipeline's exit hides.
        [ "$rc" = 0 ] && rc=1
        state "$rc" "$out"
        return
    fi
    read -ra hdr <<<"$out"
    case $((0x${hdr[3]} & 15)) in
        0) echo "reachable(NOERROR)" ;;
        2) echo "reachable(SERVFAIL)" ;;
        3) echo "reachable(NXDOMAIN)" ;;
        5) echo "reachable(REFUSED)" ;;
        *) echo "reachable(rcode $((0x${hdr[3]} & 15)))" ;;
    esac
}

gw=$(ip route show default | awk '{print $3; exit}')
addr=$(ip -4 addr show scope global | awk '/inet /{print $2; exit}')
resolver=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf 2>/dev/null)
echo "guest ${addr:-no address}, gateway ${gw:-none}, resolver ${resolver:-none}, kernel $(uname -r)"
if [ -z "$gw" ]; then
    echo "no default gateway; the host-plane probes below cannot run" >&2
    exit 2
fi
# The default gateway is the host for a sample wired straight to its vnets, but not for a
# range whose guests route through a router VM — and a router's own sshd would then be read as
# a host service. So it stands in for the host only when nothing better was passed;
# check-host-isolation.sh emits every address the host holds, the gateway among them.
if [ ${#host_addrs[@]} -gt 0 ]; then
    hosts=("${host_addrs[@]}")
else
    hosts=("$gw")
    echo "no --host-addr given, so the default gateway is assumed to be the host"
fi

echo
echo "# the host itself"
for host in "${hosts[@]}"; do
    for spec in 8006:pveproxy 22:ssh 85:pvedaemon 111:rpcbind 25:smtp 3128:spiceproxy 4318:otlp-collector 5900:vnc 5901:vnc 53:dns; do
        port=${spec%%:*}
        want blocked "host $host:$port (${spec#*:})" "$(tcp_state "$host" "$port")"
    done
    want blocked "host $host:53/udp (dns)" "$(dns_state "$host")"
done

echo
echo "# cloud metadata and link-local"
# IMDS addresses: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html#instance-metadata-v2-how-it-works
# Resolver addresses: https://docs.aws.amazon.com/vpc/latest/userguide/AmazonDNS-concepts.html
want blocked "IMDS 169.254.169.254:80" "$(tcp_state 169.254.169.254 80)"
want blocked "IMDS [fd00:ec2::254]:80" "$(tcp_state fd00:ec2::254 80)"
# The resolver also answers on a VPC-specific address, which the guest has no way to derive.
for resolver_addr in 169.254.169.253 fd00:ec2::253; do
    label="VPC resolver $resolver_addr"
    [[ $resolver_addr == *:* ]] && label="VPC resolver [$resolver_addr]"
    want blocked "$label:53" "$(tcp_state "$resolver_addr" 53)"
    want blocked "$label:53/udp" "$(dns_state "$resolver_addr")"
done
want blocked "link-local router 169.254.1.1:80" "$(tcp_state 169.254.1.1 80)"

echo
echo "# IPv6"
# Forwarded IPv6 is dropped on the host whether or not the egress lockdown is armed.
v6addr=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2; exit}')
v6route=$(ip -6 route show default 2>/dev/null | head -1)
# want's pattern is "$1"*, so an empty expectation matches anything: the sentinel is what
# makes these two checks able to fail.
want absent "no global IPv6 address" "${v6addr:-absent}"
want absent "no IPv6 default route" "${v6route:-absent}"
want blocked "IPv6 egress [2606:4700:4700::1111]:443" "$(tcp_state 2606:4700:4700::1111 443)"

echo
echo "# internet egress"
for ip in 1.1.1.1 8.8.8.8; do
    for port in 53 853 443; do
        want blocked "TCP $ip:$port" "$(tcp_state "$ip" "$port")"
    done
done
for name in deb.debian.org download.proxmox.com pypi.org; do
    want blocked "package registry $name:443" "$(tcp_state "$name" 443)"
done

echo
echo "# operator-supplied addresses"
if [ ${#unreachable[@]} -eq 0 ]; then
    skip "interface endpoint and VPC peering probes" "no addresses given; check-host-isolation.sh prints them"
else
    for target in "${unreachable[@]}"; do
        case "$target" in
            # Brackets are how an IPv6 literal gets a port; bash then wants it without them.
            \[*\]:*) host=${target#[}; host=${host%%]:*}; port=${target##*:} ;;
            *:*:*) host=$target; port=443 ;;
            *:*) host=${target%:*}; port=${target##*:} ;;
            *) host=$target; port=443 ;;
        esac
        want blocked "must be unreachable: $host:$port" "$(tcp_state "$host" "$port")"
    done
fi

echo
# Load-bearing: without it there is nothing to distinguish a clean run from one SSM
# truncated at 24k.
if [ "$failures" -eq 0 ]; then
    echo "all $probes probes passed"
else
    echo "$failures of $probes probes FAILED"
    exit 1
fi
