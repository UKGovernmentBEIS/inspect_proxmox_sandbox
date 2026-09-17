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
#                  or an SDN gateway. Each gets the full host-service port battery. Passing
#                  the gateways of vnets the guest is *not* on is how the cross-segment case
#                  gets covered, so run check-host-isolation.sh while this sample is up.
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

# A rejection (TCP RST, or an ICMP unreachable) says something on the path answered instead of
# swallowing the packet — the target, or any router in between, which is why it is not evidence
# the target was reached. "Network is unreachable" is the guest's own stack declining to send,
# every IPv6 target on a guest with IPv6 off included, so it counts as a block. 124 is
# timeout(1)'s exit for the kill.
tcp_state() {
    local err
    err=$(timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>&1)
    case "$?:$err" in
        0:*) echo reachable ;;
        124:*) echo "blocked(silent)" ;;
        *"Network is unreachable"* | *"not supported"*) echo "blocked(no route from the guest)" ;;
        *) echo "rejected(${err##*: })" ;;
    esac
}
http_state() {
    local code
    code=$(curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null)
    # An empty status is curl failing to run rather than a block, so it must not read as one.
    case "$code" in
        000) echo "blocked(no response)" ;;
        "") echo "error(curl printed no status)" ;;
        *) echo "reachable($code)" ;;
    esac
}
# dig only retries over TCP on a truncated reply, never on silence, so the transport has to
# be selected explicitly to cover both.
dig_rcode() { # server type name [dig args...] -> rcode, empty if nothing answered
    local server=$1 type=$2 name=$3
    shift 3
    dig @"$server" +time=3 +tries=1 -t "$type" "$name" "$@" 2>/dev/null |
        awk -F'status: ' '/status:/ {split($2, a, ","); print a[1]; exit}'
}
# Any rcode other than NOERROR/NXDOMAIN counts as blocked: a REFUSED or SERVFAIL is a live
# resolver that cannot recurse, which carries no data even though it is not silence.
dns_state() { # server type name
    local rcode
    rcode=$(dig_rcode "$@")
    case "$rcode" in
        NOERROR|NXDOMAIN) echo "reachable($rcode)" ;;
        "") echo "blocked(no response)" ;;
        *) echo "blocked($rcode)" ;;
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
    for spec in 8006:pveproxy 22:ssh 85:pvedaemon 111:rpcbind 25:smtp 3128:spiceproxy 4318:otlp-collector 5900:vnc 5901:vnc; do
        port=${spec%%:*}
        want blocked "host $host:$port (${spec#*:})" "$(tcp_state "$host" "$port")"
    done
    want blocked "Proxmox API https://$host:8006/api2/json/version" "$(http_state "https://$host:8006/api2/json/version")"
    # The node rule closes the resolver's port with REJECT so guests fail immediately instead
    # of hanging on every lookup. Silence here means the rule is a DROP; reachable means
    # dnsmasq is still serving this address.
    want rejected "host $host:53 (dns)" "$(tcp_state "$host" 53)"
done
# Not judged, and blocked on an isolated host: the node firewall accepts only DHCP from guests.
# So this is not the witness that the probes above found shut ports rather than a dead network
# — the port-53 rejection is, because a cut path cannot produce one.
echo "INFO  ICMP to the gateway [$(ping -c1 -W2 "$gw" >/dev/null 2>&1 && echo reachable || echo blocked)]"

echo
echo "# cloud metadata and link-local"
# addresses from https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html#instance-metadata-v2-how-it-works
# IMDS addresses: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html#instance-metadata-v2-how-it-works
# Resolver addresses: https://docs.aws.amazon.com/vpc/latest/userguide/AmazonDNS-concepts.html
want blocked "IMDSv1 GET http://169.254.169.254/latest/meta-data/instance-id" \
    "$(http_state http://169.254.169.254/latest/meta-data/instance-id)"
want blocked "IMDSv2 token PUT http://169.254.169.254/latest/api/token" \
    "$(http_state -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' http://169.254.169.254/latest/api/token)"
want blocked "IMDS over IPv6 http://[fd00:ec2::254]/latest/meta-data/" \
    "$(http_state 'http://[fd00:ec2::254]/latest/meta-data/')"
# The resolver also answers on a VPC-specific address, which the guest has no way to derive.
for resolver_addr in 169.254.169.253 fd00:ec2::253; do
    want blocked "VPC resolver $resolver_addr:53 over TCP" "$(tcp_state "$resolver_addr" 53)"
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
want blocked "IPv6 egress https://[2606:4700:4700::1111]/" "$(http_state 'https://[2606:4700:4700::1111]/')"

echo
echo "# internet egress"
for target in 1.1.1.1:443 8.8.8.8:53; do
    want blocked "TCP ${target/:/ port }" "$(tcp_state "${target%:*}" "${target##*:}")"
done
names=(deb.debian.org download.proxmox.com pypi.org)
for name in "${names[@]}"; do
    want blocked "package registry https://$name/" "$(http_state "https://$name/")"
done

echo
echo "# DNS"
if ! command -v dig >/dev/null 2>&1; then
    skip "DNS probes" "no dig; install dnsutils/bind-utils in the guest template"
else
    # UDP to the link-local addresses, which the TCP probes above cannot cover.
    for resolver_addr in 169.254.169.253 fd00:ec2::253; do
        want blocked "VPC resolver $resolver_addr:53 over UDP" \
            "$(dns_state "$resolver_addr" A "${names[0]}")"
    done
    # Under the lockdown the node rule rejects port 53, so the SDN resolver answers nothing at
    # all — not even REFUSED. dig never falls back to TCP on silence, so that transport needs
    # a probe of its own.
    for name in "${names[@]}"; do
        want blocked "recursion via $gw for $name" "$(dns_state "$gw" A "$name")"
    done
    want blocked "recursion via $gw for ${names[0]} over TCP" \
        "$(dns_state "$gw" A "${names[0]}" +tcp)"
    # A long random label under a name the resolver will recurse for is the classic
    # exfil-over-DNS channel; the TXT reply is the return path.
    label=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
    want blocked "DNS exfil TXT $label.${names[0]}" "$(dns_state "$gw" TXT "$label.${names[0]}")"
    for resolver_ip in 1.1.1.1 8.8.8.8; do
        for port in 53 853 443; do
            want blocked "direct resolver $resolver_ip:$port" "$(tcp_state "$resolver_ip" "$port")"
        done
    done
fi

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
