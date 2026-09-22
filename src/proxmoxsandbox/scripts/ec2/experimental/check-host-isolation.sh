#!/bin/bash
# Run *on the host*. Checks the guest-isolation mechanism from ../userdata.sh: the units
# are installed, enabled and last ran OK (guards against stale AMIs), and the rules they
# install are loaded. check-guest-isolation.sh probes the effect from inside a guest; run
# both — this one ends by printing the guest command line to paste.
# One PASS/FAIL/SKIP line per check. Every check runs; the exit status is nonzero if any
# failed, and the trailing summary line says how many.
#
# Assumes the configuration in the parent README's "Properly isolating the host": egress
# lockdown armed, no route off the VPC, reachable only via interface endpoints. That is
# the configuration worth checking; an ordinary host fails these by design.
# shellcheck disable=SC2329  # the check helpers are invoked indirectly, via check
set -uo pipefail

usage() { echo "usage: $0" >&2; exit 2; }
[ $# -eq 0 ] || usage

checks=0
failures=0
check() {
    local name=$1 out
    shift
    checks=$((checks + 1))
    if out=$("$@" 2>&1); then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        # Most helpers are quiet; the ones reading a value print what they saw.
        [ -n "$out" ] && echo "      ${out//$'\n'/$'\n'      }"
        failures=$((failures + 1))
    fi
}
skip() { echo "SKIP  $1 ($2)"; }

unit_ok() { systemctl is-enabled -q "$1" && [ "$(systemctl show -p Result --value "$1")" = success ]; }
pvefw_running() { pve-firewall status | grep -q enabled/running; }
on_failure_is() { [ "$(systemctl show -p OnFailure --value "$1")" = "$2" ]; }
unit_prop_is() { # unit, then property/value pairs
    local unit=$1 got
    shift
    while [ $# -ge 2 ]; do
        got=$(systemctl show -p "$1" --value "$unit")
        [ "$got" = "$2" ] || { echo "$1 is ${got:-<unset>}, want $2"; return 1; }
        shift 2
    done
}
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
# Fragments are literals, and every one must sit on the same rule line; the ones a comment
# match separates are passed as separate fragments.
has_rule() { # table chain fragment...
    local rules frag
    rules=$(iptables -w -t "$1" -S "$2" 2>/dev/null) || return 1
    shift 2
    for frag; do rules=$(grep -F -- "$frag" <<<"$rules") || return 1; done
}
has_rule6() { ip6tables -w -S "$1" 2>/dev/null | grep -qF -- "$2"; }
# 100.64.0.0/10 is in here because AWS allows it as a VPC CIDR, so an endpoint can land there.
is_private() {
    case "$1" in
        10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
        100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
    esac
    return 1
}
http_code() { curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
connects() { [ "$(http_code "$1")" != 000 ]; }
resolves_to() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1; exit}'; }
no_connect() {
    local code
    code=$(http_code "$1")
    [ "$code" = 000 ] && return 0
    echo "connected, HTTP $code"
    return 1
}
unresolvable() {
    local ip
    ip=$(resolves_to "$1")
    [ -z "$ip" ] && return 0
    echo "resolves to $ip"
    return 1
}

nic=$(ip route show default | awk '{print $5; exit}')
node=$(hostname)
marker=/etc/inspect-proxmox-egress-lockdown
echo "host $node, mgmt NIC ${nic:-none}, kernel $(uname -r), $(pveversion 2>/dev/null | head -1)"

# The contract stamped by the inspect-proxmox-host deb. Read locally rather than over the API: if the halt
# unit has masked pvedaemon, an API read fails and a fine host looks like a stale one.
WANT_CONTRACT=3
contract=$(pveversion 2>/dev/null | head -1)
case "$contract" in
    *.aisi[0-9]*) contract=${contract##*.aisi}; contract=${contract%%[!0-9]*} ;;
    *) contract=0 ;;
esac
if ! [ "$contract" -ge "$WANT_CONTRACT" ] 2>/dev/null; then
    echo
    echo "host contract aisi${contract:-0}, need aisi$WANT_CONTRACT; install inspect-proxmox-host $WANT_CONTRACT or later (see host/README.md)"
    exit 2
fi

echo
echo "# units (stale-AMI guard)"
check "host firewall unit: inspect-proxmox-host-configure.service" unit_ok inspect-proxmox-host-configure.service
check "pve-firewall enabled and running" pvefw_running
check "IMDS / link-local forwarding block: inspect-proxmox-block-cloud-metadata.service" unit_ok inspect-proxmox-block-cloud-metadata.service
check "guest NAT/FORWARD rules: inspect-proxmox-ec2-network.service" unit_ok inspect-proxmox-ec2-network.service
check "egress lockdown (internet+DNS): inspect-proxmox-egress-lockdown.service" unit_ok inspect-proxmox-egress-lockdown.service
check "egress lockdown re-armed periodically: inspect-proxmox-egress-lockdown.timer" systemctl is-active -q inspect-proxmox-egress-lockdown.timer
check "egress lockdown fails deadly: OnFailure=inspect-proxmox-egress-lockdown-halt.service" \
    on_failure_is inspect-proxmox-egress-lockdown.service inspect-proxmox-egress-lockdown-halt.service
# Masked means the halt unit fired at some point, so the rules can look right now even
# though a lockdown run failed earlier.
check "Proxmox API not masked by the halt unit: pveproxy, pvedaemon" not_masked pveproxy.service pvedaemon.service
check "contract re-stamped after apt runs: /etc/apt/apt.conf.d/80inspect-proxmox-contract" \
    grep -qF /usr/libexec/inspect-proxmox/stamp-contract /etc/apt/apt.conf.d/80inspect-proxmox-contract
# Without these two, systemd scores a concurrent restart or a burst of starts as a unit
# failure, and the halt unit masks the API on a host whose lockdown is applied correctly.
check "halt unit fires only on the lockdown's own verdict: SuccessExitStatus, StartLimitIntervalUSec" \
    unit_prop_is inspect-proxmox-egress-lockdown.service SuccessExitStatus TERM \
    StartLimitIntervalUSec 0

echo
echo "# forwarding rules and kernel state"
check "link-local destinations dropped: raw PREROUTING -d 169.254.0.0/16 -j DROP" \
    has_rule raw PREROUTING "-d 169.254.0.0/16 -j DROP"
check "link-local sources dropped: FORWARD -s 169.254.0.0/16 -j DROP" \
    has_rule filter FORWARD "-s 169.254.0.0/16 -j DROP"
check "forwarded IPv6 dropped: ip6tables FORWARD -j DROP" has_rule6 FORWARD "-A FORWARD -j DROP"
check "IPv6 off for interfaces created after boot (SDN vnets): net.ipv6.conf.default.disable_ipv6" \
    sysctl_is net.ipv6.conf.default.disable_ipv6 1
check "IPv4 forwarding on (guests reach their gateway): net.ipv4.ip_forward" sysctl_is net.ipv4.ip_forward 1

echo
echo "# Proxmox firewall (host services reachable only on the mgmt NIC)"
node_rules=$(pvesh get "/nodes/$node/firewall/rules" --output-format json 2>&1)
node_rules_rc=$?
cluster_rules=$(pvesh get /cluster/firewall/rules --output-format json 2>&1)
cluster_rules_rc=$?
fetched() { [ "$1" = 0 ] || { echo "$2"; return 1; }; }
# Read the .fw files, not /cluster/firewall/options: pvesh cannot GET that path since
# pve-manager 9.2.7, fixed in 9.2.19 (https://bugzilla.proxmox.com/show_bug.cgi?id=7942).
# pmxcfs serves the API from these files anyway, and pve-firewall status above covers the
# effective state.
fw_enabled() {
    local f=$1 enable
    [ -f "$f" ] || { echo "$f absent, so the firewall is off"; return 1; }
    enable=$(awk '/^\[/ { s = $0 } s == "[OPTIONS]" && /^[[:space:]]*enable:/ { print $2; exit }' "$f")
    [ "$enable" = 1 ] && return 0
    echo "enable=${enable:-<unset>} in $f [OPTIONS]"
    return 1
}
# An ACCEPT with no --iface applies on every SDN gateway as well as the management address,
# so it is open to guests on any vnet; one bound to an SDN bridge is open to that vnet. Both
# are invisible to a check that only asks whether the wanted rules are there, so the whole
# inbound ACCEPT set is compared against the set ../userdata.sh creates. pvesh reports .enable
# only for rules that carry it, and a rule without it is enabled.
oneline() { printf '%s' "${1:-<none>}" | tr '\n' ' '; }
inbound_accepts() { # one "proto/dport@iface" per inbound ACCEPT, sorted; macros by name
    jq -r '[ .[]
             | select(.type == "in" and .action == "ACCEPT")
             | (.macro // ((.proto // "any") + "/" + ((.dport // "any") | tostring)))
               + "@" + (.iface // "")
               + (if ((.enable // 1) | tonumber) == 1 then "" else "(disabled)" end)
           ] | sort | .[]' <<<"$1"
}
accepts_are() { # rules expected...
    local got want
    got=$(inbound_accepts "$1")
    shift
    want=$([ $# -eq 0 ] || printf '%s\n' "$@" | LC_ALL=C sort)
    [ "$got" = "$want" ] && return 0
    echo "want: $(oneline "$want")"
    echo "got:  $(oneline "$got")"
    return 1
}
check "node firewall rules readable" fetched "$node_rules_rc" "$node_rules"
check "cluster firewall rules readable" fetched "$cluster_rules_rc" "$cluster_rules"
check "cluster firewall enabled" fw_enabled /etc/pve/firewall/cluster.fw
check "node firewall enabled" fw_enabled "/etc/pve/nodes/$node/host.fw"
# Guests reach the host only where an ACCEPT is unbound: DHCP and DNS on every gateway. The
# port-53 ACCEPTs stay under lockdown; the lockdown's iptables INPUT REJECT (checked below)
# sits ahead of the node firewall and closes the port.
node_accepts=("tcp/8006@$nic" "tcp/22@$nic" "udp/67@" "udp/53@" "tcp/53@")
# Skipped rather than run on an unreadable fetch, which would fail for a reason that has
# nothing to do with the rules.
if [ "$node_rules_rc" = 0 ]; then
    check "node inbound ACCEPTs are exactly the AMI's, mgmt NIC $nic" \
        accepts_are "$node_rules" "${node_accepts[@]}"
else
    skip "node inbound ACCEPT checks" "node firewall rules unreadable"
fi
if [ "$cluster_rules_rc" = 0 ]; then
    # A cluster-level rule applies on every node NIC, and the AMI creates none.
    check "no inbound ACCEPT at cluster level" accepts_are "$cluster_rules"
else
    skip "cluster inbound ACCEPT check" "cluster firewall rules unreadable"
fi

echo
echo "# guest egress lockdown"
no_upstream_resolver() { ! grep -q "^nameserver" /run/dnsmasq/resolv.conf; }
check "opt-in marker present: $marker" test -f "$marker"
check "guest egress dropped: mangle FORWARD -o $nic" has_rule mangle FORWARD "-o $nic " "-j DROP"
check "guest ingress dropped: mangle FORWARD -i $nic" has_rule mangle FORWARD "-i $nic " "-j DROP"
# iptables -S prints the owner as a number
dnsmasq_uid=$(id -u dnsmasq 2>/dev/null)
check "dnsmasq upstream queries dropped: mangle OUTPUT --uid-owner dnsmasq (${dnsmasq_uid:-no such user})" \
    has_rule mangle OUTPUT "-o $nic " "--uid-owner ${dnsmasq_uid:-dnsmasq} " "-j DROP"
check "no upstream resolver for SDN dnsmasq: /run/dnsmasq/resolv.conf" no_upstream_resolver
# With no upstream, all the resolver could still serve a guest is its own lease table, which
# spans every vnet in the zone. REJECT, not DROP, so lookups fail instead of hanging.
check "guest DNS rejected rather than dropped: INPUT udp/53 -j REJECT" \
    has_rule filter INPUT "! -i lo -p udp -m udp --dport 53 " "-j REJECT"
check "guest DNS rejected rather than dropped: INPUT tcp/53 -j REJECT" \
    has_rule filter INPUT "! -i lo -p tcp -m tcp --dport 53 " "-j REJECT --reject-with tcp-reset"

echo
echo "# AWS-level controls, as seen from the host"
endpoint_ok() { is_private "$1" && connects "https://$2/"; }
region=$(/usr/libexec/inspect-proxmox/imds latest/meta-data/placement/region 2>/dev/null)
check "region from IMDS" test -n "$region"
endpoints=""
if [ -z "$region" ]; then
    skip "interface endpoint checks" "no region, so the endpoint names cannot be built"
else
    # The host keeps these three — SSM is the operator's way in; a guest must not be able
    # to reach them at all, which is what the guest script's address arguments probe.
    for svc in ssm ssmmessages ec2messages; do
        name="$svc.$region.amazonaws.com"
        ip=$(resolves_to "$name")
        check "interface endpoint $svc: $name resolves" test -n "$ip"
        check "interface endpoint $svc: $ip private and answering on 443" endpoint_ok "$ip" "$name"
        [ -n "$ip" ] && endpoints="$endpoints $ip"
    done
    # The CloudWatch endpoint is optional, so a public answer here is a VPC without one
    # rather than a leak. Add it to the guest's target list only when it is an endpoint.
    name="monitoring.$region.amazonaws.com"
    ip=$(resolves_to "$name")
    if is_private "$ip"; then
        check "interface endpoint monitoring: $ip answering on 443" connects "https://$name/"
        endpoints="$endpoints $ip"
    else
        skip "interface endpoint monitoring" \
            "${ip:-$name} is not an endpoint in this VPC (metrics are optional)"
    fi
fi
check "DNS firewall NXDOMAINs everything else: deb.debian.org does not resolve" unresolvable deb.debian.org
check "no route off the VPC: https://1.1.1.1 does not connect" no_connect https://1.1.1.1/

echo
# Every IPv4 address this host holds is one a guest must not reach: the management address,
# the NAT bridge, and one SDN gateway per vnet of whatever sample is running right now. Run
# this while a sample is up and those gateways are included, which is the only way the guest
# script gets to probe segments other than its own — it cannot discover them itself.
args=""
for ip in $(ip -4 -o addr show scope global | awk '{split($4, a, "/"); print a[1]}' | sort -u); do
    args="$args --host-addr $ip"
done
for ip in $endpoints; do
    args="$args --unreachable $ip"
done
if [ -n "$args" ]; then
    echo "# paste into the guest run (addresses are per-host and per-VPC; do not commit them):"
    echo "  check-guest-isolation.sh$args"
else
    echo "# no host or endpoint addresses found, so there is no guest command line to print"
fi

echo
# Load-bearing: without it there is nothing to distinguish a clean run from one SSM
# truncated at 24k.
if [ "$failures" -eq 0 ]; then
    echo "all $checks checks passed"
else
    echo "$failures of $checks checks FAILED"
    exit 1
fi
