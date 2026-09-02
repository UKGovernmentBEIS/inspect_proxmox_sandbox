#!/bin/bash
# Run *on the host*. Guards against stale AMIs: checks the guest-isolation units from
# ../userdata.sh are installed, enabled and last ran OK. Non-zero exit if any FAIL.
set -uo pipefail
rc=0
chk() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; rc=1; fi; }
unit_ok() { systemctl is-enabled -q "$1" && [ "$(systemctl show -p Result --value "$1")" = success ]; }
chk "host firewall (8006/22 only on mgmt NIC): proxmox-ami-fixup-firewall.service" 'unit_ok proxmox-ami-fixup-firewall.service && pve-firewall status | grep -q enabled/running'
chk "IMDS / link-local forwarding block: inspect-proxmox-block-cloud-metadata.service" 'unit_ok inspect-proxmox-block-cloud-metadata.service'
chk "egress lockdown (internet+DNS): inspect-proxmox-egress-lockdown.service + .timer" 'unit_ok inspect-proxmox-egress-lockdown.service && systemctl is-active -q inspect-proxmox-egress-lockdown.timer'
chk "egress lockdown: opt-in marker /etc/inspect-proxmox-egress-lockdown present" '[ -f /etc/inspect-proxmox-egress-lockdown ]'
exit $rc
