#!/bin/bash
# Run *on the host* (via run-script-on-host.sh). Checks the guest-isolation controls
# installed by ../userdata.sh are present and active. Non-zero exit if any FAIL.
set -uo pipefail
NIC=$(ip route show default | awk '{print $5}' | head -1)
NODE=$(hostname)
LOCK=inspect-proxmox-egress-lockdown
rc=0
chk() { local n=$1; shift; if eval "$*" >/dev/null 2>&1; then echo "PASS  $n"; else echo "FAIL  $n"; rc=1; fi; }
# PVE renders host-rule ACCEPT as RETURN in PVEFW-HOST-IN; accept either. PVE's built-in
# rules also accept 8006/22 from the "management" ipset; that set is checked separately.
MGMT_SET=PVEFW-0-management-v4
port_only_on_nic() { iptables -w -S PVEFW-HOST-IN | grep -q -- "-i $NIC .*--dport $1 .*-j \(ACCEPT\|RETURN\)" && ! iptables -w -S PVEFW-HOST-IN | grep -- "--dport $1 " | grep -v -e "-i $NIC " -e "--match-set $MGMT_SET src" | grep -q -- '-j \(ACCEPT\|RETURN\)'; }
# vmbr0 guest range plus every SDN subnet (subnet ids look like zone-10.0.0.0-24).
guest_nets() { echo 10.10.10.2; awk '/^subnet:/{sub(/^[^-]*-/,"",$2); sub(/-[0-9]+$/,"",$2); print $2}' /etc/pve/sdn/subnets.cfg 2>/dev/null; }
# The DROP must sit above any ACCEPT in its chain (NAT service adds FORWARD ACCEPTs).
drop_before_accept() { iptables -w -t "$1" -S "$2" | grep -m1 -e '-j ACCEPT' -e '169.254.0.0/16 -j DROP' | grep -q DROP; }

echo "node=$NODE nic=$NIC"
chk "pve-firewall enabled at cluster+node, running, policy_in not ACCEPT" \
    'grep -qx "enable: 1" /etc/pve/firewall/cluster.fw && grep -qx "enable: 1" /etc/pve/nodes/$NODE/host.fw && ! grep -q "policy_in: ACCEPT" /etc/pve/firewall/cluster.fw /etc/pve/nodes/$NODE/host.fw && pve-firewall status | grep -q enabled/running'
chk "PVE API (8006) accepted only on $NIC"            'port_only_on_nic 8006'
chk "SSH (22) accepted only on $NIC"                  'port_only_on_nic 22'
chk "no guest subnet in PVE management ipset"          '! for ip in $(guest_nets); do ipset test $MGMT_SET $ip 2>/dev/null && break; done'
chk "IMDS: link-local drops present (raw dst, FORWARD src, ip6 FORWARD)" \
    'iptables -w -t raw -C PREROUTING -d 169.254.0.0/16 -j DROP && iptables -w -C FORWARD -s 169.254.0.0/16 -j DROP && ip6tables -w -C FORWARD -j DROP'
chk "IMDS: drops precede any ACCEPT in their chains" 'drop_before_accept raw PREROUTING && drop_before_accept filter FORWARD'
# IMDS doesn't expose its own hop limit, but the PUT (token) response carries it as the IP
# TTL (the SYN-ACK and GET responses come back with 64), so min TTL seen == hop limit.
ttl=$( { timeout 6 tcpdump -i any -c 8 -nnv 'src host 169.254.169.254' 2>/dev/null & sleep 2; /usr/local/bin/call-ec2-hypervisor latest/meta-data/instance-id >/dev/null; wait; } | grep -o 'ttl [0-9]*' | awk '{print $2}' | sort -n | head -1)
chk "IMDS: HttpPutResponseHopLimit=1 (observed min ttl=${ttl:-?})" '[ "${ttl:-}" = 1 ]'
chk "egress lockdown (internet+DNS): marker, service enabled, last run ok, timer active, pveproxy not halted" \
    '[ -f /etc/$LOCK ] && systemctl is-enabled -q $LOCK.service && [ "$(systemctl show -p Result --value $LOCK.service)" = success ] && systemctl is-active -q $LOCK.timer && systemctl is-active -q pveproxy'
chk "egress lockdown: mangle FORWARD drops on $NIC both directions" \
    'iptables -w -t mangle -S FORWARD | grep -q -- "-i $NIC .*$LOCK.*-j DROP" && iptables -w -t mangle -S FORWARD | grep -q -- "-o $NIC .*$LOCK.*-j DROP"'
exit $rc
