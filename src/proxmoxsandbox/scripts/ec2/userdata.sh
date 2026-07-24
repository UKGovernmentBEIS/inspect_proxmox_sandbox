#!/bin/bash
# Installs SSM agent on Debian 13 (not included by default), then installs Proxmox VE.
# Follows https://pve.proxmox.com/wiki/Install_Proxmox_VE_on_Debian_13_Trixie
# with workarounds for non-interactive EC2 environments.
#
# NOTE: This script shares setup logic with scripts/virtualized_proxmox/build_proxmox_auto.
# If you change shared logic here, update that file too and vice versa.
set -euxo pipefail
# Log all output with timestamps to /root/install-proxmox.log for debugging
exec > >(while IFS= read -r line; do echo "$(date '+%H:%M:%S') $line"; done | tee /root/install-proxmox.log) 2>&1

# --- IMDSv2 helper (also used by EIC and AMI fixup services below) ---
apt-get update -y
apt-get install -y wget curl
cat > /usr/local/bin/call-ec2-hypervisor << 'CALL_EC2_HYPERVISOR'
#!/bin/bash
# Fetch a path from EC2 IMDSv2 (the EC2 hypervisor's metadata service).
# Usage: call-ec2-hypervisor <path>
#   call-ec2-hypervisor latest/meta-data/placement/region
#   call-ec2-hypervisor latest/meta-data/instance-id
set -euo pipefail
TOKEN=$(curl -sf -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" \
    http://169.254.169.254/latest/api/token)
curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
    "http://169.254.169.254/$1"
CALL_EC2_HYPERVISOR
chmod 755 /usr/local/bin/call-ec2-hypervisor

# --- SSM agent (needed for out-of-band access before Proxmox is up) ---
# Pull from the in-region bucket so the build doesn't pay cross-region S3 egress.
REGION=$(/usr/local/bin/call-ec2-hypervisor latest/meta-data/placement/region)
wget -q "https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/debian_amd64/amazon-ssm-agent.deb" \
    -O /tmp/amazon-ssm-agent.deb
dpkg -i /tmp/amazon-ssm-agent.deb
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

# --- EC2 Instance Connect (package not in Debian 13 repos; configure sshd manually) ---
# Fetches temporary keys pushed by `aws ec2-instance-connect send-ssh-public-key` from IMDS.
cat > /usr/local/bin/eic_authorized_keys << 'EICSCRIPT'
#!/bin/bash
exec /usr/local/bin/call-ec2-hypervisor "latest/meta-data/managed-ssh-keys/active-keys/${1}/"
EICSCRIPT
chmod 755 /usr/local/bin/eic_authorized_keys
cat >> /etc/ssh/sshd_config << 'SSHDCONF'
AuthorizedKeysCommand /usr/local/bin/eic_authorized_keys %u
AuthorizedKeysCommandUser nobody
SSHDCONF
systemctl restart ssh

PRIVATE_IP=$(hostname -I | awk '{print $1}')

# --- Hostname ---
hostnamectl set-hostname proxmox
echo "$PRIVATE_IP proxmox.localdomain proxmox" >> /etc/hosts

# --- Proxmox repo key ---
wget -q https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
    -O /usr/share/keyrings/proxmox-archive-keyring.gpg
echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" \
    | sha256sum -c

# --- Proxmox apt source ---
cat > /etc/apt/sources.list.d/pve-install-repo.sources << 'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

# --- Update and full-upgrade ---
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y

# --- Install Proxmox kernel ---
# Preseed grub-pc install device to avoid interactive prompt on NVMe-based EC2 instances
echo "grub-pc grub-pc/install_devices string /dev/nvme0n1" | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-default-kernel

# --- Wait for amazon-guardduty-agent (if AWS GuardDuty Runtime Monitoring is
# pushing it) to install BEFORE we reboot. If we reboot mid-install, the
# postinst's `systemctl start` fails because systemd has reboot.target queued,
# and combined with a non-idempotent configure.sh that wedges the package at
# dpkg state `iF`, every later apt-get install in stage 2 exits non-zero.
# In accounts without GuardDuty Runtime Monitoring, short-circuits after 30s.
echo "Waiting up to 3 min for amazon-guardduty-agent to install before reboot..."
state=""
for i in $(seq 1 36); do
    state=$(dpkg-query -f '${Status}' -W amazon-guardduty-agent 2>/dev/null || true)
    if [ "$state" = "install ok installed" ]; then
        break
    fi
    # Short-circuit: after 30s, if there's no sign GuardDuty Runtime Monitoring
    # is pushing the agent, stop waiting (saves ~2.5 min in accounts where it
    # isn't enabled).
    if [ "$i" -ge 6 ] && \
       [ ! -d /var/lib/amazon/ssm/packages/AmazonGuardDuty-RuntimeMonitoringSsmPlugin ] && \
       ! grep -qF AmazonGuardDuty /var/log/amazon/ssm/amazon-ssm-agent.log 2>/dev/null; then
        break
    fi
    sleep 5
done
echo "  amazon-guardduty-agent state: ${state:-not present}; proceeding"

# --- Reboot into Proxmox kernel, then continue via systemd oneshot ---
cat > /etc/systemd/system/proxmox-install-stage2.service << 'UNIT'
[Unit]
Description=Proxmox VE install stage 2 (post-kernel-reboot)
After=network-online.target
Wants=network-online.target
ConditionPathExists=/root/proxmox-install-stage2.sh

[Service]
Type=oneshot
ExecStart=/bin/bash /root/proxmox-install-stage2.sh
ExecStartPost=/bin/rm -f /etc/systemd/system/proxmox-install-stage2.service
RemainAfterExit=yes
StandardOutput=append:/root/install-proxmox.log
StandardError=append:/root/install-proxmox.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /root/proxmox-install-stage2.sh << 'STAGE2'
#!/bin/bash
set -euxo pipefail

# --- Install Proxmox VE packages ---
echo "postfix postfix/main_mailer_type select Local only" | debconf-set-selections
echo "postfix postfix/mailname string proxmox.localdomain" | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-ve postfix open-iscsi chrony

# --- Remove old Debian kernel and os-prober ---
DEBIAN_FRONTEND=noninteractive apt-get remove -y linux-image-amd64 'linux-image-6.12*' os-prober
update-grub

# --- Root password is generated/refreshed by proxmox-ami-fixup-password.service
# on every boot where the EC2 instance-id has changed (i.e. on the build
# instance's first boot, and on every subsequent launch from an AMI). See
# below.

# --- SDN dependencies ---
# dnsmasq: needed for SDN DHCP/IPAM; disable the system service (PVE manages per-zone instances)
DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq patch jq
systemctl disable --now dnsmasq
# frr: needed for SDN routing (EVPN/OSPF zones); installed with proxmox-ve but not enabled.
# Not needed for simple zones (the default), only for EVPN/OSPF.
# systemctl enable frr

# --- Fix IPAM bug ---
# Without this patch, static DHCP IP reservations (by MAC address) don't work.
# See https://forum.proxmox.com/threads/ipam-reserving-dhcp-leases-via-mac-addresses.174704/
# and https://lists.proxmox.com/pipermail/pve-devel/2025-November/076472.html

cat << 'EOFPATCH' | patch /usr/share/perl5/PVE/Network/SDN/Subnets.pm
--- a/usr/share/perl5/PVE/Network/SDN/Subnets.pm
+++ b/usr/share/perl5/PVE/Network/SDN/Subnets.pm
@@ -235,6 +235,30 @@ sub add_next_free_ip {
     #verify dns zones before ipam
     verify_dns_zone($dnszone, $dns) if !$skipdns;

+    if ($mac && $ipamid) {
+        my ($zoneid) = split(/-/, $subnetid);
+        my ($existing_ip4, $existing_ip6) = PVE::Network::SDN::Ipams::get_ips_from_mac(
+            $mac, $zoneid, $zone,
+        );
+
+        my $is_ipv4 = Net::IP::ip_is_ipv4($subnet->{network});
+        my $existing_ip = $is_ipv4 ? $existing_ip4 : $existing_ip6;
+
+        if ($existing_ip) {
+            my $ip_obj = NetAddr::IP->new($existing_ip);
+            my $subnet_obj = NetAddr::IP->new($subnet->{cidr});
+
+            if ($subnet_obj->contains($ip_obj)) {
+                $ip = $existing_ip;
+
+                eval { PVE::Network::SDN::Ipams::add_cache_mac_ip($mac, $ip); };
+                warn $@ if $@;
+
+                goto DNS_SETUP;
+            }
+        }
+    }
+
     if ($ipamid) {
         my $ipam_cfg = PVE::Network::SDN::Ipams::config();
         my $plugin_config = $ipam_cfg->{ids}->{$ipamid};
@@ -267,6 +291,7 @@ sub add_next_free_ip {
         warn $@ if $@;
     }

+DNS_SETUP:
     eval {
         my $reversednszone = get_reversedns_zone($subnetid, $subnet, $reversedns, $ip);

EOFPATCH

# Mark version to indicate patching
sed -i "s/\('version' => '[0-9]\+\.[0-9]\+\.[0-9]\+\)',/\1.aisi1',/" /usr/share/perl5/PVE/pvecfg.pm

# --- DNS forwarding for SDN dnsmasq instances ---
# PVE launches per-zone dnsmasq with -r /run/dnsmasq/resolv.conf for upstream DNS.
# On EC2 that file doesn't exist, so dnsmasq can't forward queries and VMs have
# no working DNS. Point it at the VPC resolver (second IP in the VPC CIDR).
cat > /etc/tmpfiles.d/dnsmasq-resolv.conf <<'EOF'
d /run/dnsmasq 0755 root root -
f /run/dnsmasq/resolv.conf 0644 root root - nameserver 169.254.169.253
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/dnsmasq-resolv.conf

# --- NAT bridge for VMs ---
# VMs can't use IPs directly on the VPC subnet (EC2 only routes traffic to
# IPs assigned to ENIs), so we give VMs a private 10.10.10.0/24 network and
# NAT their traffic through the host's single NIC.
pvesh create /nodes/proxmox/network \
    --iface vmbr0 --type bridge \
    --autostart 1 \
    --cidr 10.10.10.1/24
# Add source directive for SDN (PVE writes per-zone configs to interfaces.d/)
grep -qxF 'source /etc/network/interfaces.d/*' /etc/network/interfaces.new \
    || sed -i '1s|^|source /etc/network/interfaces.d/*\n\n|' /etc/network/interfaces.new

# --- IP forwarding ---
# NAT/FORWARD rules are applied at boot by proxmox-ami-fixup-nat.service below,
# which resolves the management NIC name dynamically (it differs across EC2
# instance families: enp39s0 on m8i, ens5 on m6i, etc., so it can't be baked
# into the AMI).
echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-vm-nat.conf
sysctl -w net.ipv4.ip_forward=1

# --- Configure 'local' storage to accept all content types (including import) ---
pvesm set local --content images,rootdir,vztmpl,backup,iso,snippets,import

# --- AMI boot-time fixup services ---
# When an AMI is launched with a new IP, EC2 changes the hostname to ip-x-x-x-x,
# breaking Proxmox node identity, SSL certs, and pveproxy. These services fix
# that on every boot. The password fixup additionally detects fresh launches
# (by EC2 instance-id) and regenerates the root password so credentials don't
# leak across instances launched from the same AMI.
NODE_NAME="proxmox"

cat > /usr/local/bin/proxmox-ami-fixup-hostname.sh << 'FIXUP_HOSTNAME'
#!/bin/bash
set -euo pipefail
PRIVATE_IP=$(hostname -I | awk '{print $1}')
hostnamectl set-hostname proxmox
sed -i "/proxmox/d" /etc/hosts
echo "$PRIVATE_IP proxmox.localdomain proxmox" >> /etc/hosts
FIXUP_HOSTNAME
chmod +x /usr/local/bin/proxmox-ami-fixup-hostname.sh

cat > /etc/systemd/system/proxmox-ami-fixup-hostname.service << 'FIXUP_HOSTNAME_UNIT'
[Unit]
Description=Fix hostname and /etc/hosts for AMI-launched Proxmox
Before=pve-cluster.service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/proxmox-ami-fixup-hostname.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FIXUP_HOSTNAME_UNIT

cat > /usr/local/bin/proxmox-ami-fixup-certs.sh << 'FIXUP_CERTS'
#!/bin/bash
set -euo pipefail
pvecm updatecerts --force
FIXUP_CERTS
chmod +x /usr/local/bin/proxmox-ami-fixup-certs.sh

cat > /etc/systemd/system/proxmox-ami-fixup-certs.service << 'FIXUP_CERTS_UNIT'
[Unit]
Description=Regenerate Proxmox SSL certs with current IP
After=pve-cluster.service
Before=pvedaemon.service pveproxy.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/proxmox-ami-fixup-certs.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FIXUP_CERTS_UNIT

# Regenerate root password whenever the EC2 instance-id changes (i.e. on the
# build instance's first boot, and on every fresh launch from an AMI). Without
# this, every instance launched from a given AMI shares the password set during
# the build run, which leaks across launches as soon as one of the saved
# passwords is exposed.
cat > /usr/local/bin/proxmox-ami-fixup-password.sh << 'FIXUP_PASSWORD'
#!/bin/bash
set -euo pipefail
CURRENT_ID=$(/usr/local/bin/call-ec2-hypervisor latest/meta-data/instance-id)
SAVED_ID=$(cat /root/.last-instance-id 2>/dev/null || true)
if [ "$CURRENT_ID" = "$SAVED_ID" ] && [ -s /root/root-password ]; then
    exit 0
fi
( umask 077
  PASSWORD=$(openssl rand -base64 18)
  echo "root:$PASSWORD" | chpasswd
  echo "$PASSWORD" > /root/root-password
  echo "$CURRENT_ID" > /root/.last-instance-id
)
FIXUP_PASSWORD
chmod +x /usr/local/bin/proxmox-ami-fixup-password.sh

cat > /etc/systemd/system/proxmox-ami-fixup-password.service << 'FIXUP_PASSWORD_UNIT'
[Unit]
Description=Regenerate root password when EC2 instance-id changes
After=network-online.target
Wants=network-online.target
Before=pveproxy.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/proxmox-ami-fixup-password.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FIXUP_PASSWORD_UNIT

# Apply NAT/FORWARD rules at boot using the current management NIC.
# The NIC name (e.g. enp39s0, ens5) depends on instance family, so we can't
# bake it into persistent iptables rules at AMI build time.
cat > /usr/local/bin/proxmox-ami-fixup-nat.sh << 'FIXUP_NAT'
#!/bin/bash
set -euo pipefail
MGMT_NIC=$(ip route show default | awk '{print $5}' | head -1)
if [ -z "$MGMT_NIC" ]; then
    echo "ERROR: could not determine management NIC from default route" >&2
    exit 1
fi
iptables -t nat -C POSTROUTING -s 10.10.10.0/24 -o "$MGMT_NIC" -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -s 10.10.10.0/24 -o "$MGMT_NIC" -j MASQUERADE
iptables -C FORWARD -i vmbr0 -o "$MGMT_NIC" -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -i vmbr0 -o "$MGMT_NIC" -j ACCEPT
iptables -C FORWARD -i "$MGMT_NIC" -o vmbr0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
    || iptables -A FORWARD -i "$MGMT_NIC" -o vmbr0 -m state --state RELATED,ESTABLISHED -j ACCEPT
FIXUP_NAT
chmod +x /usr/local/bin/proxmox-ami-fixup-nat.sh

cat > /etc/systemd/system/proxmox-ami-fixup-nat.service << 'FIXUP_NAT_UNIT'
[Unit]
Description=Apply NAT/FORWARD iptables rules with current management NIC
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/proxmox-ami-fixup-nat.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FIXUP_NAT_UNIT

# IPv6 is not supported for sandbox guests. SDN vnet bridges are created per
# sample with generated names, so default.disable_ipv6 makes every interface
# created after boot (i.e. the vnets) come up with no IPv6; the management NIC,
# already up, keeps its own setting.
cat > /etc/sysctl.d/99-inspect-proxmox-disable-ipv6.conf << 'SYSCTL_V6'
net.ipv6.conf.default.disable_ipv6 = 1
SYSCTL_V6

# Confine sandbox guests at the host's forwarding layer. Kept identical to the
# on-first-boot heredoc in scripts/virtualized_proxmox/build_proxmox_auto.sh.
cat > /usr/local/bin/inspect-proxmox-block-cloud-metadata.sh << 'BLOCK_METADATA'
#!/bin/bash
set -euo pipefail

# Enforce RFC 3927: a router must not forward IPv4 link-local (169.254.0.0/16).
#
# Destination drop in raw PREROUTING (interface-agnostic, ahead of any FORWARD
# ACCEPT; host requests are OUTPUT so unaffected) -- this blocks the metadata vector.
iptables -w -t raw -C PREROUTING -d 169.254.0.0/16 -j DROP 2>/dev/null \
    || iptables -w -t raw -I PREROUTING 1 -d 169.254.0.0/16 -j DROP
# Source drop in FORWARD, not raw PREROUTING: belt-and-braces for full RFC
# conformance. FORWARD leaves the host's own on-link replies (IMDS/DNS, at INPUT)
# intact; a raw PREROUTING -s rule would drop them and break the host.
iptables -w -C FORWARD -s 169.254.0.0/16 -j DROP 2>/dev/null \
    || iptables -w -I FORWARD 1 -s 169.254.0.0/16 -j DROP

# Belt-and-braces for the unsupported IPv6 case: drop forwarded guest v6 outright.
# FORWARD only sees transit traffic, so the host's own v6 (INPUT/OUTPUT) is intact.
if command -v ip6tables >/dev/null; then
    ip6tables -w -C FORWARD -j DROP 2>/dev/null \
        || ip6tables -w -A FORWARD -j DROP
fi

# Keep pveproxy (8006) and ssh (22) reachable when a guest fills the host conntrack
# table (e.g. an in-range nmap): NOTRACK'd packets need no table slot. See root README.
for _port in 8006 22; do
    iptables -w -t raw -C PREROUTING -p tcp --dport "$_port" -j CT --notrack 2>/dev/null \
        || iptables -w -t raw -I PREROUTING 1 -p tcp --dport "$_port" -j CT --notrack
    iptables -w -t raw -C OUTPUT -p tcp --sport "$_port" -j CT --notrack 2>/dev/null \
        || iptables -w -t raw -I OUTPUT 1 -p tcp --sport "$_port" -j CT --notrack
done
BLOCK_METADATA
chmod +x /usr/local/bin/inspect-proxmox-block-cloud-metadata.sh

cat > /etc/systemd/system/inspect-proxmox-block-cloud-metadata.service << 'BLOCK_METADATA_UNIT'
[Unit]
Description=Confine sandbox guests (link-local forwarding block, IPv6 drop)
After=network-online.target pve-firewall.service proxmox-firewall.service
Wants=network-online.target
Before=proxmox-ami-fixup-nat.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/inspect-proxmox-block-cloud-metadata.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
BLOCK_METADATA_UNIT

# Host isolation. See root README.
# Re-applied every boot (no marker): the node name changes per launch, so any
# node-scoped rules baked into the AMI are orphaned under the old node name, and
# the NIC name depends on instance family. We delete our own rules (matched by
# comment) and recreate them, so the rule set converges regardless of prior state.
# NOTE: keep these rules in sync with the on-first-boot heredoc in
# scripts/virtualized_proxmox/build_proxmox_auto.sh.
cat > /usr/local/bin/proxmox-ami-fixup-firewall.sh << 'FIXUP_FIREWALL'
#!/bin/bash
set -euo pipefail
NIC=$(ip route show default | awk '{print $5}' | head -1)
[ -z "$NIC" ] && { echo "ERROR: no default route; cannot isolate host" >&2; exit 1; }
NODE=$(hostname)
C="inspect-proxmox-sandbox: host-isolation"
# Delete any of our existing rules first, descending pos (deletes renumber).
pvesh get /nodes/$NODE/firewall/rules --output-format json \
    | jq -r --arg c "$C" 'map(select(.comment == $c)) | sort_by(.pos) | reverse | .[].pos' \
    | while read -r pos; do pvesh delete /nodes/$NODE/firewall/rules/"$pos"; done
pvesh create /nodes/$NODE/firewall/rules --type in --action ACCEPT --proto tcp --dport 8006 --iface "$NIC" --enable 1 --comment "$C"
pvesh create /nodes/$NODE/firewall/rules --type in --action ACCEPT --proto tcp --dport 22 --iface "$NIC" --enable 1 --comment "$C"
pvesh create /nodes/$NODE/firewall/rules --type in --action ACCEPT --proto udp --dport 53 --enable 1 --comment "$C"
pvesh create /nodes/$NODE/firewall/rules --type in --action ACCEPT --proto tcp --dport 53 --enable 1 --comment "$C"
pvesh create /nodes/$NODE/firewall/rules --type in --action ACCEPT --proto udp --dport 67 --enable 1 --comment "$C"
pvesh set /nodes/$NODE/firewall/options --enable 1
pvesh set /cluster/firewall/options --enable 1
FIXUP_FIREWALL
chmod +x /usr/local/bin/proxmox-ami-fixup-firewall.sh

cat > /etc/systemd/system/proxmox-ami-fixup-firewall.service << 'FIXUP_FIREWALL_UNIT'
[Unit]
Description=Enable Proxmox host firewall isolation with current management NIC
# After the hostname fixup so rules are created under the final node name, not
# the transient EC2 ip-x-x-x-x hostname.
After=proxmox-ami-fixup-hostname.service pve-cluster.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/proxmox-ami-fixup-firewall.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
FIXUP_FIREWALL_UNIT

rm -vf /etc/apt/sources.list.d/{pve-enterprise,ceph}.sources

systemctl daemon-reload
systemctl enable proxmox-ami-fixup-hostname.service
systemctl enable proxmox-ami-fixup-certs.service
systemctl enable proxmox-ami-fixup-nat.service
systemctl enable proxmox-ami-fixup-password.service
systemctl enable proxmox-ami-fixup-firewall.service
systemctl enable inspect-proxmox-block-cloud-metadata.service

# Bound each sandbox VM's guest->host packet rate at its tap. A flooding guest (a
# high-rate nmap/masscan) otherwise drives host conntrack + softirq hard enough to
# starve the control plane -- a DoS the NOTRACK rules above do not stop. An ingress
# policer drops the excess BEFORE conntrack; a udev rule applies it to every tap as
# it appears (tap names are per-VM and dynamic, so this can't be a static rule). See
# root README.
cat > /usr/local/bin/inspect-proxmox-tap-policer.sh << 'TAP_POLICER'
#!/bin/bash
set -euo pipefail
IFACE="$1"
PPS=20000
# Proxmox's NIC bring-up wipes an ingress qdisc applied at the udev 'add' event, so
# re-assert for a few seconds until it sticks (verified: single-shot gets cleared).
for _ in $(seq 1 8); do
    tc qdisc add dev "$IFACE" handle ffff: ingress 2>/dev/null || true
    tc filter replace dev "$IFACE" parent ffff: matchall \
        action police pkts_rate "$PPS" pkts_burst "$PPS" drop 2>/dev/null || true
    sleep 1
done
TAP_POLICER
chmod +x /usr/local/bin/inspect-proxmox-tap-policer.sh

cat > /etc/systemd/system/inspect-proxmox-tap-policer@.service << 'TAP_POLICER_UNIT'
[Unit]
Description=Rate-limit sandbox VM tap %i (guest->host flood protection)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/inspect-proxmox-tap-policer.sh %i
TAP_POLICER_UNIT

cat > /etc/udev/rules.d/99-inspect-proxmox-tap-policer.rules << 'TAP_POLICER_UDEV'
SUBSYSTEM=="net", ACTION=="add", KERNEL=="tap*i*", TAG+="systemd", ENV{SYSTEMD_WANTS}+="inspect-proxmox-tap-policer@$name.service"
TAP_POLICER_UDEV

systemctl daemon-reload
udevadm control --reload 2>/dev/null || true

# ===== CloudWatch OTLP metrics collector =====
# Ship pvestatd's metrics to the CloudWatch OTLP endpoint via a localhost CloudWatch
# agent, SigV4-signed with the instance role (needs cloudwatch:PutMetricData; 403s
# harmlessly without it). cumulativetodelta is required -- PVE emits cumulative sums
# without StartTimeUnixNano, which CloudWatch rejects. The build only STAGES files;
# all runtime steps (region from IMDS, agent config + start) happen at first boot, so
# we never start the agent or connection-test the endpoint at build time.
#
# resourcedetection tags every datapoint with the EC2 instance-id and the instance
# Name tag, so metrics are distinguishable per box without renaming the PVE node.
# The Name is read from IMDS at boot (the launcher must enable InstanceMetadataTags)
# and injected via OTEL_RESOURCE_ATTRIBUTES, picked up by resourcedetection's env
# detector -- no ec2:DescribeTags IAM needed. Do NOT use the ec2 detector's
# tags_from_imds: the CloudWatch agent's embedded collector rejects that key.
#
# A filter processor trims pvestatd's ~1400 datapoints/cycle down to memory, CPU and
# disk (see its comment below) -- the full set exceeds CloudWatch's 1000-per-request
# limit; the batch processor also hard-caps each request at 1000 as a backstop.
curl -fsSL "https://amazoncloudwatch-agent.s3.amazonaws.com/debian/amd64/latest/amazon-cloudwatch-agent.deb" \
    -o /tmp/cwagent.deb
dpkg -i /tmp/cwagent.deb

cat > /opt/aws/amazon-cloudwatch-agent/etc/otel-metrics.yaml << 'OTELYAML'
receivers:
  otlp/cwagent:
    protocols:
      http:
        endpoint: 127.0.0.1:4318
processors:
  resourcedetection/cwagent:
    # env reads ec2.tag.Name from OTEL_RESOURCE_ATTRIBUTES (set at boot from IMDS);
    # ec2 adds instance-id/region/AZ/type.
    detectors: [env, ec2]
    timeout: 5s
  # Keep only memory, CPU and disk (capacity + I/O) for guests and host. pvestatd emits
  # ~1400 datapoints/cycle and the bulk is per-VM-per-disk proxmox_vm_blockstat_*, which
  # blows past CloudWatch's 1000-datapoints-per-request limit. Of blockstat we keep only
  # rd/wr operations + total_time_ns per device (IOPS, and latency = Dtime/Dops) -- the
  # [a-z]+[0-9]+ device match deliberately excludes failed_/invalid_ op counters. io PSI
  # (pressureio*) shows tasks stalled on I/O. Everything else is dropped.
  filter/cwagent:
    error_mode: ignore
    metrics:
      metric:
        - 'not IsMatch(name, "^(proxmox_vm_(cpu|cpus|mem|maxmem|memhost|balloon|freemem|disk|maxdisk)|proxmox_vm_pressureio(full|some)|proxmox_vm_blockstat_[a-z]+[0-9]+_(rd|wr)_(operations|total_time_ns)_total|proxmox_node_(memory|cpustat|blockstat)_.+|proxmox_storage_(used|total|avail))$")'
  cumulativetodelta/cwagent: {}
  # Hard cap so a request can never exceed CloudWatch's 1000-datapoint limit, even if
  # the guest count grows past what the filter trims to.
  batch/cwagent:
    send_batch_size: 1000
    send_batch_max_size: 1000
exporters:
  otlphttp/cwagent:
    metrics_endpoint: "https://monitoring.${env:AWS_REGION}.amazonaws.com/v1/metrics"
    compression: gzip
    auth: { authenticator: sigv4auth/cwagent }
extensions:
  sigv4auth/cwagent: { service: monitoring, region: "${env:AWS_REGION}" }
service:
  extensions: [sigv4auth/cwagent]
  pipelines:
    metrics/cwagent:
      receivers: [otlp/cwagent]
      processors: [filter/cwagent, resourcedetection/cwagent, cumulativetodelta/cwagent, batch/cwagent]
      exporters: [otlphttp/cwagent]
OTELYAML
echo '{"agent":{}}' > /opt/aws/amazon-cloudwatch-agent/etc/cw-base.json

# pvestatd -> local collector. Write status.cfg directly: `pvesh create` connection-tests
# the endpoint, which isn't running at build time. Persists in pmxcfs (baked in).
printf 'opentelemetry: cloudwatch-otel\n\tserver 127.0.0.1\n\tport 4318\n\totel-protocol http\n\totel-path /v1/metrics\n\totel-compression gzip\n' >> /etc/pve/status.cfg

# Boot-time setup (runs every boot): resolve region from IMDS, then translate +
# start the agent. Done at boot (not build) so ${env:AWS_REGION} resolves to the
# launch region and the agent is never started (nor the endpoint tested) at build
# time. fetch-config(base) + append-config is idempotent, so re-running each boot
# re-resolves region/Name (e.g. on AMI relaunch) without duplicating the pipeline.
cat > /usr/local/bin/cloudwatch-otel-apply.sh << 'OTELAPPLY'
#!/bin/bash
set -euo pipefail
REGION=$(/usr/local/bin/call-ec2-hypervisor latest/meta-data/placement/region)
echo "AWS_REGION=${REGION}" > /etc/default/amazon-cloudwatch-agent-otel
# Read the instance Name tag from IMDS (present only if the launcher enabled
# InstanceMetadataTags) and hand it to the collector's env detector as a resource
# attribute. Absent tag -> no Name label, instance-id still identifies the box.
NAME=$(/usr/local/bin/call-ec2-hypervisor latest/meta-data/tags/instance/Name 2>/dev/null || true)
if [ -n "${NAME}" ]; then
    echo "OTEL_RESOURCE_ATTRIBUTES=ec2.tag.Name=${NAME}" >> /etc/default/amazon-cloudwatch-agent-otel
fi
export AWS_REGION="${REGION}"
ctl=/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl
"$ctl" -a fetch-config -c file:/opt/aws/amazon-cloudwatch-agent/etc/cw-base.json
"$ctl" -a append-config -c file:/opt/aws/amazon-cloudwatch-agent/etc/otel-metrics.yaml -s
OTELAPPLY
chmod +x /usr/local/bin/cloudwatch-otel-apply.sh

# The agent service reads AWS_REGION (written above) so otelcol resolves ${env:AWS_REGION}.
mkdir -p /etc/systemd/system/amazon-cloudwatch-agent.service.d
cat > /etc/systemd/system/amazon-cloudwatch-agent.service.d/otel-region.conf << 'OTELDROPIN'
[Service]
EnvironmentFile=/etc/default/amazon-cloudwatch-agent-otel
OTELDROPIN

cat > /etc/systemd/system/cloudwatch-otel-apply.service << 'OTELAPPLYUNIT'
[Unit]
Description=Configure and start the CloudWatch agent OTel pipeline
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/cloudwatch-otel-apply.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
OTELAPPLYUNIT
systemctl daemon-reload
systemctl enable cloudwatch-otel-apply.service

echo "PROXMOX INSTALL COMPLETE: $(pveversion)"

# Final reboot: pvenetcommit.service will promote interfaces.new -> interfaces
# and ifreload will bring up vmbr0 cleanly before networking.service runs.
echo "Stage 2 complete. Rebooting to apply network config..."
systemctl reboot
STAGE2

chmod +x /root/proxmox-install-stage2.sh
systemctl enable proxmox-install-stage2.service

echo "Stage 1 complete. Rebooting into Proxmox kernel..."
systemctl reboot
