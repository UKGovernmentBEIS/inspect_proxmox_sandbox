#!/bin/bash
# Run *inside a Linux sandbox guest* (guest agent, `qm terminal`, or paste into a console).
# Probes the effect of the isolation baked by ../userdata.sh: what the guest can actually
# reach. check-host-isolation.sh checks the mechanism on the host; run both. Run the host
# one first — it prints the --aws-endpoint arguments for this script.
#
# Usage: check-guest-isolation.sh [--expect-no-egress] [--strict] [flags]
#   --expect-no-egress    host was launched --no-internet: the internet, package registry
#                         and DNS-tunnelling probes must all be blocked. Without it they
#                         must all succeed — that is the negative control which proves the
#                         blocked results elsewhere in the run mean something.
#   --strict              a SKIP counts as a failure (use this for a red-team pass)
#   --aws-endpoint IP     VPC interface endpoint address; must be unreachable. Repeatable.
#   --peer-target IP[:PORT]  address across a VPC peering link; must be unreachable. Repeatable.
#   --egress-target H:PORT   extra internet target. Repeatable, added to the defaults.
#   --dns-name NAME       extra name for the DNS/registry probes. Repeatable, added to the defaults.
#   --internal-name NAME  name the SDN resolver is expected to answer for.
#   --peer-guest IP|NAME  another guest in the same sample; reachability is reported, not judged.
set -uo pipefail

no_egress=false
strict=false
internal_name=""
peer_guest=""
aws_endpoints=()
peer_targets=()
egress_targets=(1.1.1.1:443 8.8.8.8:53)
dns_names=(deb.debian.org download.proxmox.com pypi.org)
while [ $# -gt 0 ]; do
    case "$1" in
        --expect-no-egress) no_egress=true ;;
        --strict) strict=true ;;
        --aws-endpoint) aws_endpoints+=("$2"); shift ;;
        --peer-target) peer_targets+=("$2"); shift ;;
        --egress-target) egress_targets+=("$2"); shift ;;
        --dns-name) dns_names+=("$2"); shift ;;
        --internal-name) internal_name=$2; shift ;;
        --peer-guest) peer_guest=$2; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

pass=0
fail=0
skipped=0
report() {
    echo "$1  $2"
    case "$1" in PASS) pass=$((pass + 1)) ;; FAIL) fail=$((fail + 1)) ;; esac
}
must_block() { case "$2" in blocked*) report PASS "$1 [$2]" ;; *) report FAIL "$1 [$2]" ;; esac; }
absent() { case "$2" in "") report PASS "$1 [absent]" ;; *) report FAIL "$1 [$2]" ;; esac; }
# Inverted by --expect-no-egress: on a connected host these targets have to work.
egress() {
    local want=reachable
    $no_egress && want=blocked
    case "$2" in "$want"*) report PASS "$1 [$2]" ;; *) report FAIL "$1 [$2, want $want]" ;; esac
}
info() { echo "INFO  $1 [$2]"; }
skip() { echo "SKIP  $1 ($2)"; skipped=$((skipped + 1)); }

tcp_state() {
    if timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; then echo reachable; else echo blocked; fi
}
http_state() {
    local code
    code=$(curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null)
    if [ "$code" = 000 ] || [ -z "$code" ]; then echo blocked; else echo "reachable($code)"; fi
}
# NTP client request: LI/VN/mode byte then 47 zero bytes. A reply means the guest reached
# the Amazon time service, which is off-VPC as far as the sandbox is concerned.
ntp_state() {
    if timeout 3 bash -c "exec 3<>/dev/udp/$1/123; { printf '\033'; head -c 47 /dev/zero; } >&3; head -c 1 <&3" 2>/dev/null | grep -q .; then
        echo reachable
    else
        echo blocked
    fi
}
dig_status() { # server type name -> rcode, or empty when nothing answered
    dig @"$1" +time=3 +tries=1 -t "$2" "$3" 2>/dev/null |
        awk -F'status: ' '/status:/ {split($2, a, ","); print a[1]; exit}'
}
dns_state() {
    local status
    status=$(dig_status "$@")
    case "${status:-none}" in
        NOERROR|NXDOMAIN) echo "reachable($status)" ;;
        none) echo "blocked(no response)" ;;
        *) echo "blocked($status)" ;;
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
hosts=("$gw")
# The SDN gateway is also the host. Probe the documented address too in case the guest's
# default route has been pointed somewhere else.
[ "$gw" != 10.10.10.1 ] && hosts+=(10.10.10.1)

echo
echo "# the host itself"
for host in "${hosts[@]}"; do
    for spec in 8006:pveproxy 22:ssh 85:pvedaemon 111:rpcbind 25:smtp 3128:spiceproxy 4318:otlp-collector 5900:vnc 5901:vnc; do
        port=${spec%%:*}
        must_block "host $host:$port (${spec#*:})" "$(tcp_state "$host" "$port")"
    done
    must_block "Proxmox API https://$host:8006/api2/json/version" "$(http_state "https://$host:8006/api2/json/version")"
done
info "ICMP to the gateway" "$(ping -c1 -W2 "$gw" >/dev/null 2>&1 && echo reachable || echo blocked)"

echo
echo "# cloud metadata and link-local"
must_block "IMDSv1 GET http://169.254.169.254/latest/meta-data/instance-id" \
    "$(http_state http://169.254.169.254/latest/meta-data/instance-id)"
must_block "IMDSv2 token PUT http://169.254.169.254/latest/api/token" \
    "$(http_state -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' http://169.254.169.254/latest/api/token)"
must_block "IMDS over IPv6 http://[fd00:ec2::254]/latest/meta-data/" \
    "$(http_state 'http://[fd00:ec2::254]/latest/meta-data/')"
must_block "VPC resolver 169.254.169.253:53" "$(tcp_state 169.254.169.253 53)"
must_block "Amazon time sync 169.254.169.123:123" "$(ntp_state 169.254.169.123)"
must_block "link-local router 169.254.1.1:80" "$(tcp_state 169.254.1.1 80)"

echo
echo "# IPv6"
v6addr=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2; exit}')
v6route=$(ip -6 route show default 2>/dev/null | head -1)
absent "no global IPv6 address" "$v6addr"
absent "no IPv6 default route" "$v6route"
# Forwarded IPv6 is dropped on the host whether or not the egress lockdown is armed, so
# this one does not flip with --expect-no-egress.
must_block "IPv6 egress https://[2606:4700:4700::1111]/" "$(http_state 'https://[2606:4700:4700::1111]/')"

echo
echo "# internet egress"
for target in "${egress_targets[@]}"; do
    egress "TCP ${target/:/ port }" "$(tcp_state "${target%:*}" "${target##*:}")"
done
for name in "${dns_names[@]}"; do
    egress "package registry https://$name/" "$(http_state "https://$name/")"
done

echo
echo "# DNS"
if ! command -v dig >/dev/null 2>&1; then
    skip "DNS probes" "no dig; install dnsutils/bind-utils in the guest template"
else
    liveness=$(dig_status "$gw" A "${dns_names[0]}")
    report "$([ -n "$liveness" ] && echo PASS || echo FAIL)" "SDN resolver $gw:53 answers at all [${liveness:-no response}]"
    for name in "${dns_names[@]}"; do
        egress "recursion via $gw for $name" "$(dns_state "$gw" A "$name")"
    done
    # A long random label under a name the resolver will recurse for is the classic
    # exfil-over-DNS channel; the TXT reply is the return path.
    label=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
    egress "DNS exfil TXT $label.${dns_names[0]}" "$(dns_state "$gw" TXT "$label.${dns_names[0]}")"
    for resolver_ip in 1.1.1.1 8.8.8.8; do
        for port in 53 853 443; do
            egress "direct resolver $resolver_ip:$port" "$(tcp_state "$resolver_ip" "$port")"
        done
    done
fi
if [ -n "$internal_name" ]; then
    resolved=$(getent ahostsv4 "$internal_name" 2>/dev/null | awk '{print $1; exit}')
    report "$([ -n "$resolved" ] && echo PASS || echo FAIL)" "internal name $internal_name still resolves [${resolved:-no answer}]"
else
    skip "internal name resolution" "no --internal-name"
fi

echo
echo "# AWS interface endpoints"
if [ ${#aws_endpoints[@]} -eq 0 ]; then
    skip "interface endpoint probes" "no --aws-endpoint; take them from check-host-isolation.sh"
else
    for endpoint in "${aws_endpoints[@]}"; do
        must_block "interface endpoint $endpoint:443" "$(tcp_state "$endpoint" 443)"
    done
fi

echo
echo "# VPC peering and neighbouring guests"
if [ ${#peer_targets[@]} -eq 0 ]; then
    skip "VPC peering probes" "no --peer-target"
else
    for target in "${peer_targets[@]}"; do
        case "$target" in *:*) host=${target%:*}; port=${target##*:} ;; *) host=$target; port=443 ;; esac
        must_block "peered network $host:$port" "$(tcp_state "$host" "$port")"
    done
fi
if [ -n "$peer_guest" ]; then
    info "guest $peer_guest:22 (same-sample guests share a vnet)" "$(tcp_state "$peer_guest" 22)"
else
    skip "neighbouring guest probe" "no --peer-guest"
fi

echo
echo "$pass pass, $fail fail, $skipped skip"
if [ "$fail" -gt 0 ] || { $strict && [ "$skipped" -gt 0 ]; }; then
    exit 1
fi
exit 0
