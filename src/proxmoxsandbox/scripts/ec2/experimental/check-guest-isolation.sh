#!/bin/bash
# Run *inside a Linux sandbox guest* (guest agent, `qm terminal`, or paste into a console).
# Probes the effect of the isolation baked by ../userdata.sh: what the guest can actually
# reach. check-host-isolation.sh checks the mechanism on the host; run both, and take this
# script's arguments from the line that one prints.
# One PASS/SKIP line per probe; exits at the first failure.
#
# Assumes a guest on a host launched --no-internet: everything below must be blocked. On
# an ordinary connected host the egress probes fail by design, because there the guest can
# reach the internet.
#
# Usage: check-guest-isolation.sh [IP[:PORT] ...]
#   IP[:PORT]  addresses that must be unreachable — VPC interface endpoints, a host across
#              a peering link. Port defaults to 443. Site-specific, so never hardcoded
#              here; check-host-isolation.sh prints the line to paste.
set -uo pipefail

unreachable=("$@")

want() { # want EXPECTED NAME ACTUAL
    case "$3" in
        "$1"*) echo "PASS  $2 [$3]" ;;
        *) echo "FAIL  $2 [$3, want $1]"; exit 1 ;;
    esac
}
skip() { echo "SKIP  $1 ($2)"; }

tcp_state() {
    if timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; then echo reachable; else echo blocked; fi
}
http_state() {
    local code
    code=$(curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null)
    if [ "$code" = 000 ] || [ -z "$code" ]; then echo blocked; else echo "reachable($code)"; fi
}
dig_rcode() { # server type name -> rcode, empty if nothing answered
    dig @"$1" +time=3 +tries=1 -t "$2" "$3" 2>/dev/null |
        awk -F'status: ' '/status:/ {split($2, a, ","); print a[1]; exit}'
}
# A REFUSED answer is a live resolver refusing to recurse, which is what the lockdown
# leaves behind — blocked for tunnelling purposes, but not silence.
dns_state() { # server type name
    local rcode
    rcode=$(dig_rcode "$@")
    case "${rcode:-none}" in
        NOERROR|NXDOMAIN) echo "reachable($rcode)" ;;
        none) echo "blocked(no response)" ;;
        *) echo "blocked($rcode)" ;;
    esac
}

names=(deb.debian.org download.proxmox.com pypi.org)
gw=$(ip route show default | awk '{print $3; exit}')
addr=$(ip -4 addr show scope global | awk '/inet /{print $2; exit}')
resolver=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf 2>/dev/null)
echo "guest ${addr:-no address}, gateway ${gw:-none}, resolver ${resolver:-none}, kernel $(uname -r)"
if [ -z "$gw" ]; then
    echo "no default gateway; the host-plane probes below cannot run" >&2
    exit 2
fi
hosts=("$gw")
# The SDN gateway is also the host. Probe the documented address too in case the guest's
# default route has been pointed somewhere else.
[ "$gw" != 10.10.10.1 ] && hosts+=(10.10.10.1)

echo
echo "# the host itself"
for host in "${hosts[@]}"; do
    for spec in 8006:pveproxy 22:ssh 85:pvedaemon 111:rpcbind 25:smtp 3128:spiceproxy 4318:otlp-collector 5900:vnc 5901:vnc; do
        port=${spec%%:*}
        want blocked "host $host:$port (${spec#*:})" "$(tcp_state "$host" "$port")"
    done
    want blocked "Proxmox API https://$host:8006/api2/json/version" "$(http_state "https://$host:8006/api2/json/version")"
done
# Not judged: the L3 path to the gateway is what makes "blocked" above mean the port is
# shut rather than the network being dead.
echo "INFO  ICMP to the gateway [$(ping -c1 -W2 "$gw" >/dev/null 2>&1 && echo reachable || echo blocked)]"

echo
echo "# cloud metadata and link-local"
want blocked "IMDSv1 GET http://169.254.169.254/latest/meta-data/instance-id" \
    "$(http_state http://169.254.169.254/latest/meta-data/instance-id)"
want blocked "IMDSv2 token PUT http://169.254.169.254/latest/api/token" \
    "$(http_state -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' http://169.254.169.254/latest/api/token)"
want blocked "IMDS over IPv6 http://[fd00:ec2::254]/latest/meta-data/" \
    "$(http_state 'http://[fd00:ec2::254]/latest/meta-data/')"
want blocked "VPC resolver 169.254.169.253:53" "$(tcp_state 169.254.169.253 53)"
want blocked "link-local router 169.254.1.1:80" "$(tcp_state 169.254.1.1 80)"

echo
echo "# IPv6"
# Forwarded IPv6 is dropped on the host whether or not the egress lockdown is armed.
v6addr=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2; exit}')
v6route=$(ip -6 route show default 2>/dev/null | head -1)
want absent "no global IPv6 address" "${v6addr:-absent}"
want absent "no IPv6 default route" "${v6route:-absent}"
want blocked "IPv6 egress https://[2606:4700:4700::1111]/" "$(http_state 'https://[2606:4700:4700::1111]/')"

echo
echo "# internet egress"
for target in 1.1.1.1:443 8.8.8.8:53; do
    want blocked "TCP ${target/:/ port }" "$(tcp_state "${target%:*}" "${target##*:}")"
done
for name in "${names[@]}"; do
    want blocked "package registry https://$name/" "$(http_state "https://$name/")"
done

echo
echo "# DNS"
if ! command -v dig >/dev/null 2>&1; then
    skip "DNS probes" "no dig; install dnsutils/bind-utils in the guest template"
else
    # UDP to a link-local address, which the TCP probes above cannot cover.
    want blocked "VPC resolver 169.254.169.253:53 over UDP" "$(dns_state 169.254.169.253 A "${names[0]}")"
    rcode=$(dig_rcode "$gw" A "${names[0]}")
    want answered "SDN resolver $gw:53 answers at all" \
        "$([ -n "$rcode" ] && echo "answered($rcode)" || echo "no response")"
    for name in "${names[@]}"; do
        want blocked "recursion via $gw for $name" "$(dns_state "$gw" A "$name")"
    done
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
        case "$target" in *:*) host=${target%:*}; port=${target##*:} ;; *) host=$target; port=443 ;; esac
        want blocked "must be unreachable: $host:$port" "$(tcp_state "$host" "$port")"
    done
fi

echo
echo "all probes passed"
