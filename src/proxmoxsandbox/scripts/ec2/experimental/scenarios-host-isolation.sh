#!/bin/bash
# Run *on the host*, as root. Exercises the parts of the egress lockdown that
# check-host-isolation.sh cannot see: the marker transitions, idempotency, the stale-host
# guard, and the fail-deadly asymmetry. That script asserts a state; this one drives the
# machine between states and asserts what it does on the way.
#
# Scenarios are chosen from the host's current marker state, so the same invocation suits
# a host launched locked down and one launched open. Both are worth running: each is the
# other's negative control.
#
# Restores the marker, the pvecfg stamp and any masked units on exit, including on ^C.
# --fail-deadly and the ordering test deliberately break the host for a few seconds; the
# ordering test reboots.
# shellcheck disable=SC2329  # the check helpers are invoked indirectly, via chk
set -uo pipefail

MARKER=/etc/inspect-proxmox-egress-lockdown
UNIT=inspect-proxmox-egress-lockdown.service
PVECFG=/usr/share/perl5/PVE/pvecfg.pm
ORDERING_DROPIN=/etc/systemd/system/pve-cluster.service.d/zz-scenario-delay.conf
FW_COMMENT="inspect-proxmox-sandbox: host-isolation"
# The timer is OnUnitActiveSec=1min with AccuracySec=15s.
SETTLE=90

fail_deadly=0
arm_ordering=0
check_ordering=0
usage() {
    cat >&2 <<EOF
usage: $0 [--fail-deadly] [--arm-ordering-test] [--check-ordering-test]

  --fail-deadly         also run the OnFailure scenarios, which mask the Proxmox API
                        for a few seconds before unmasking it
  --arm-ordering-test   delay pve-cluster's start, arm the marker and reboot
  --check-ordering-test after that reboot, report whether the lockdown waited
EOF
    exit 2
}
for arg in "$@"; do
    case "$arg" in
        --fail-deadly) fail_deadly=1 ;;
        --arm-ordering-test) arm_ordering=1 ;;
        --check-ordering-test) check_ordering=1 ;;
        *) usage ;;
    esac
done

checks=0
failures=0
chk() {
    local name=$1 out
    shift
    checks=$((checks + 1))
    if out=$("$@" 2>&1); then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        [ -n "$out" ] && echo "      ${out//$'\n'/$'\n'      }"
        failures=$((failures + 1))
    fi
}
note() { echo "      $*"; }

nic=$(ip route show default | awk '{print $5; exit}')
node=$(hostname)
[ -f "$MARKER" ] && started_locked=1 || started_locked=0

restore() {
    [ -f "$ORDERING_DROPIN" ] && { rm -f "$ORDERING_DROPIN"; systemctl daemon-reload; }
    if [ "$started_locked" = 1 ]; then touch "$MARKER"; else rm -f "$MARKER"; fi
    sed -i "s/\.aisiSCENARIO/.aisi/" "$PVECFG" 2>/dev/null
    systemctl unmask --runtime pve-cluster.service pveproxy.service pvedaemon.service >/dev/null 2>&1
    systemctl start pve-cluster.service pvedaemon.service pveproxy.service >/dev/null 2>&1
    systemctl reset-failed "$UNIT" >/dev/null 2>&1
    systemctl start "$UNIT" >/dev/null 2>&1
}
trap restore EXIT

# --- helpers reading the state the lockdown owns -------------------------------------

dns53_rules() {
    pvesh get "/nodes/$node/firewall/rules" --output-format json 2>/dev/null \
        | jq -r --arg c "$FW_COMMENT" "[ .[] | select(.comment == \$c and .type == \"in\"
                   and ((.dport // \"\") | tostring) == \"53\") ] | $1" 2>/dev/null
}
dns53_actions() { dns53_rules 'map("\(.action)/\(.proto)") | sort | join(",")'; }
dns53_is() {
    local got
    got=$(dns53_actions)
    [ "$got" = "$1/tcp,$1/udp" ] && return 0
    echo "want $1/tcp,$1/udp; got ${got:-<none>}"
    return 1
}
has_rule() { iptables -w -t "$1" -S "$2" 2>/dev/null | grep -q -- "$3"; }
forward_drops() { has_rule mangle FORWARD "-o $nic .*-j DROP" && has_rule mangle FORWARD "-i $nic .*-j DROP"; }
no_forward_drops() { ! has_rule mangle FORWARD "-[io] $nic .*-j DROP"; }
resolv_blank() { ! grep -q "^nameserver" /run/dnsmasq/resolv.conf 2>/dev/null; }
resolv_set() { grep -q "^nameserver" /run/dnsmasq/resolv.conf 2>/dev/null; }
unit_succeeded() { [ "$(systemctl show -p Result --value "$UNIT")" = success ]; }
unit_failed() { [ "$(systemctl show -p Result --value "$UNIT")" != success ]; }
masked() { case "$(systemctl is-enabled "$1" 2>/dev/null)" in masked*) return 0 ;; esac; return 1; }
not_masked() { ! masked "$1"; }

# Waits for the timer rather than starting the unit, so what is under test is the path a
# host actually takes when the marker changes. The port-53 rewrite is the run's first act,
# so waiting for the run to finish too is what makes the checks after this one meaningful.
await_dns53() {
    local want=$1 i
    for i in $(seq 1 $SETTLE); do
        if [ "$(dns53_actions)" = "$want/tcp,$want/udp" ] &&
            [ "$(systemctl show -p ActiveState --value "$UNIT")" != activating ]; then
            echo "settled after ${i}s"
            return 0
        fi
        sleep 1
    done
    echo "still $(dns53_actions), unit $(systemctl show -p ActiveState --value "$UNIT") after ${SETTLE}s"
    return 1
}

echo "host $node, mgmt NIC ${nic:-none}, $(pveversion 2>/dev/null | head -1)"
echo "marker $([ "$started_locked" = 1 ] && echo present || echo absent) at start"

# --- ordering test, which is the only scenario needing a reboot -----------------------

if [ "$check_ordering" = 1 ]; then
    echo
    echo "# boot ordering: the lockdown must wait for pmxcfs (After=/Wants=pve-cluster)"
    chk "lockdown unit succeeded on a boot where pve-cluster was delayed 45s" unit_succeeded
    chk "halt unit did not fire: pveproxy" not_masked pveproxy.service
    chk "halt unit did not fire: pvedaemon" not_masked pvedaemon.service
    # Delaying pmxcfs collapses these two into the same instant, which is the only way to
    # see them renumbering each other's node firewall rules.
    fixup_ok() { [ "$(systemctl show -p Result --value proxmox-ami-fixup-firewall.service)" = success ]; }
    chk "the firewall fixup did not race the lockdown over the node rules" fixup_ok
    note "pmxcfs up at     $(systemctl show -p ExecMainStartTimestamp --value pve-cluster.service)"
    note "fixup ran at     $(systemctl show -p ExecMainStartTimestamp --value proxmox-ami-fixup-firewall.service)"
    note "lockdown ran at  $(systemctl show -p ExecMainStartTimestamp --value "$UNIT")"
    note "delete $ORDERING_DROPIN and reboot to undo the delay"
    echo
    if [ "$failures" -eq 0 ]; then
        echo "all $checks checks passed"
        exit 0
    fi
    echo "$failures of $checks checks FAILED"
    exit 1
fi

if [ "$arm_ordering" = 1 ]; then
    mkdir -p "$(dirname "$ORDERING_DROPIN")"
    printf '[Service]\nExecStartPre=/bin/sleep 45\n' > "$ORDERING_DROPIN"
    touch "$MARKER"
    systemctl daemon-reload
    echo "pve-cluster delayed 45s, marker armed. Rebooting; re-run with --check-ordering-test."
    trap - EXIT
    systemctl reboot
    exit 0
fi

# --- contract ------------------------------------------------------------------------

echo
echo "# host contract"
contract_at_least() {
    local v=$1 n=${1##*.aisi}
    n=${n%%[!0-9]*}
    [ -n "$n" ] && [ "$n" -ge "$2" ] 2>/dev/null && return 0
    echo "read $v"
    return 1
}
chk "pveversion carries the contract" contract_at_least "$(pveversion 2>/dev/null | head -1)" 2
chk "the version_info hash carries it" contract_at_least \
    "$(pvesh get /version --output-format json 2>/dev/null | jq -r '.version')" 2

sed -i "s/\.aisi\([0-9]\)/.aisiSCENARIO\1/" "$PVECFG"
unstamped_refused() {
    local out rc
    out=$("$(dirname "$0")/check-host-isolation.sh" 2>&1)
    rc=$?
    [ "$rc" = 2 ] && return 0
    echo "expected exit 2, got $rc: $(head -3 <<<"$out")"
    return 1
}
chk "an unstamped pvecfg.pm makes check-host-isolation.sh refuse to run" unstamped_refused
# pvesh loads pvecfg in-process, so it tracks the file; what pvedaemon serves over 8006
# depends on when it last loaded the module, and is not what this reads.
note "pvesh reports $(pvesh get /version --output-format json 2>/dev/null | jq -r '.version')"
sed -i "s/\.aisiSCENARIO/.aisi/" "$PVECFG"

# --- idempotency, in whatever state the host is already in ----------------------------

echo
echo "# idempotency"
before=$(dns53_rules 'map({action, proto}) | sort | tostring')
systemctl start "$UNIT"
systemctl start "$UNIT"
same_rules() {
    local after
    after=$(dns53_rules 'map({action, proto}) | sort | tostring')
    [ "$after" = "$before" ] && return 0
    echo "before $before"
    echo "after  $after"
    return 1
}
two_rules() {
    local n
    n=$(dns53_rules 'length')
    [ "$n" = 2 ] && return 0
    echo "$n port-53 rules carrying our comment, want 2"
    return 1
}
chk "two runs leave the port-53 rules unchanged" same_rules
chk "no duplicate port-53 rules" two_rules
chk "unit succeeded" unit_succeeded

# --- the transition, driven by the timer ----------------------------------------------

echo
if [ "$started_locked" = 1 ]; then
    echo "# transition: locked down -> open -> locked down"
    rm -f "$MARKER"
    chk "removing the marker reopens port 53 within ${SETTLE}s" await_dns53 ACCEPT
    chk "guest forwarding restored" no_forward_drops
    chk "dnsmasq upstream restored" resolv_set
    chk "no duplicate rules after the flip" two_rules
    touch "$MARKER"
    chk "replacing it closes port 53 again" await_dns53 REJECT
    chk "guest forwarding dropped" forward_drops
    chk "dnsmasq upstream removed" resolv_blank
    chk "no duplicate rules after the flip back" two_rules
else
    echo "# transition: open -> locked down -> open"
    touch "$MARKER"
    chk "arming the marker closes port 53 within ${SETTLE}s" await_dns53 REJECT
    chk "guest forwarding dropped" forward_drops
    chk "dnsmasq upstream removed" resolv_blank
    chk "no duplicate rules after the flip" two_rules
    rm -f "$MARKER"
    chk "removing it reopens port 53" await_dns53 ACCEPT
    chk "guest forwarding restored" no_forward_drops
    chk "dnsmasq upstream restored" resolv_set
    chk "no duplicate rules after the flip back" two_rules
fi

# --- fail-deadly ----------------------------------------------------------------------

if [ "$fail_deadly" = 1 ]; then
    echo
    echo "# fail-deadly asymmetry (pmxcfs stopped, so every pvesh call fails)"
    # Masked, or else Wants=pve-cluster.service just starts it again and pvesh keeps working.
    break_pvesh() { systemctl mask --runtime pve-cluster.service >/dev/null 2>&1; systemctl stop pve-cluster.service; }
    rm -f "$MARKER"
    break_pvesh
    systemctl start "$UNIT" >/dev/null 2>&1
    chk "no marker: the unit does not fail" unit_succeeded
    chk "no marker: pveproxy stays up" not_masked pveproxy.service
    chk "no marker: pvedaemon stays up" not_masked pvedaemon.service

    touch "$MARKER"
    break_pvesh
    systemctl start "$UNIT" >/dev/null 2>&1
    sleep 5
    chk "marker: the unit fails" unit_failed
    chk "marker: the halt unit masks pveproxy" masked pveproxy.service
    chk "marker: the halt unit masks pvedaemon" masked pvedaemon.service
    systemctl unmask --runtime pve-cluster.service pveproxy.service pvedaemon.service >/dev/null 2>&1
    systemctl start pve-cluster.service pvedaemon.service pveproxy.service >/dev/null 2>&1
    systemctl reset-failed "$UNIT" >/dev/null 2>&1

    # A host launched --no-internet hit both of these: cloud-init restarts the unit while the
    # boot run is in flight, and systemd scored the resulting TERM as a lockdown failure.
    echo
    echo "# the halt unit must not fire on systemd's own verdicts"
    systemctl start --no-block "$UNIT"
    sleep 1
    systemctl kill -s TERM "$UNIT"
    sleep 3
    chk "a run killed mid-flight does not fail the unit" unit_succeeded
    chk "TERM leaves pveproxy up" not_masked pveproxy.service
    chk "TERM leaves pvedaemon up" not_masked pvedaemon.service

    for _ in $(seq 1 8); do systemctl restart "$UNIT" >/dev/null 2>&1; done
    chk "8 restarts in a row do not hit systemd's start limit" unit_succeeded
    chk "the restart burst leaves pveproxy up" not_masked pveproxy.service
fi

echo
if [ "$failures" -eq 0 ]; then
    echo "all $checks checks passed"
else
    echo "$failures of $checks checks FAILED"
    exit 1
fi
