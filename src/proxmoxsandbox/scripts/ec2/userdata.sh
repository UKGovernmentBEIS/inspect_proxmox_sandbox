#!/bin/bash
# Installs the SSM agent on Debian 13 (not included by default), then Proxmox VE, then the
# inspect-proxmox-host packages. Follows
# https://pve.proxmox.com/wiki/Install_Proxmox_VE_on_Debian_13_Trixie with workarounds for
# non-interactive EC2 environments. Everything a sandbox host needs beyond a stock Proxmox
# lives in the debs (see host/README.md in the repo), so this file is only the install.
set -euxo pipefail
exec > >(while IFS= read -r line; do echo "$(date '+%H:%M:%S') $line"; done | tee /root/install-proxmox.log) 2>&1

# Bump with each host/debian/changelog entry. The bundle holds inspect-proxmox-host,
# inspect-proxmox-host-ec2 and the AISI rebuilds of pve-qemu-kvm and libpve-network-perl.
INSPECT_PROXMOX_HOST_RELEASE=host-v3
INSPECT_PROXMOX_HOST_BUNDLE_URL="${INSPECT_PROXMOX_HOST_BUNDLE_URL:-https://github.com/UKGovernmentBEIS/inspect_proxmox_sandbox/releases/download/$INSPECT_PROXMOX_HOST_RELEASE/inspect-proxmox-host-debs.tar}"
echo "INSPECT_PROXMOX_HOST_BUNDLE_URL='$INSPECT_PROXMOX_HOST_BUNDLE_URL'" > /root/proxmox-install.env

apt-get update -y
apt-get install -y wget curl

imds() {
    local token
    token=$(curl -sf -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" \
        http://169.254.169.254/latest/api/token)
    curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/$1"
}

# Pull from the in-region bucket so the build doesn't pay cross-region S3 egress.
REGION=$(imds latest/meta-data/placement/region)
wget -q "https://s3.${REGION}.amazonaws.com/amazon-ssm-${REGION}/latest/debian_amd64/amazon-ssm-agent.deb" \
    -O /tmp/amazon-ssm-agent.deb
dpkg -i /tmp/amazon-ssm-agent.deb
systemctl enable amazon-ssm-agent
systemctl start amazon-ssm-agent

# EC2 Instance Connect has no Debian 13 package, so sshd fetches the pushed keys itself.
cat > /usr/local/bin/eic_authorized_keys << 'EICSCRIPT'
#!/bin/bash
set -euo pipefail
TOKEN=$(curl -sf -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" \
    http://169.254.169.254/latest/api/token)
exec curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
    "http://169.254.169.254/latest/meta-data/managed-ssh-keys/active-keys/${1}/"
EICSCRIPT
chmod 755 /usr/local/bin/eic_authorized_keys
cat > /etc/ssh/sshd_config.d/60-ec2-instance-connect.conf << 'SSHDCONF'
AuthorizedKeysCommand /usr/local/bin/eic_authorized_keys %u
AuthorizedKeysCommandUser nobody
SSHDCONF
systemctl restart ssh

PRIVATE_IP=$(hostname -I | awk '{print $1}')

hostnamectl set-hostname proxmox
echo "$PRIVATE_IP proxmox.localdomain proxmox" >> /etc/hosts

wget -q https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
    -O /usr/share/keyrings/proxmox-archive-keyring.gpg
echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" \
    | sha256sum -c

cat > /etc/apt/sources.list.d/pve-install-repo.sources << 'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y

# Preseed grub-pc install device to avoid interactive prompt on NVMe-based EC2 instances
echo "grub-pc grub-pc/install_devices string /dev/nvme0n1" | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-default-kernel

# Rebooting mid-install wedges amazon-guardduty-agent at dpkg state `iF` (its postinst
# `systemctl start` fails with reboot.target queued, and configure.sh is not idempotent),
# which makes every later apt-get in stage 2 exit non-zero.
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
export DEBIAN_FRONTEND=noninteractive
source /root/proxmox-install.env

echo "postfix postfix/main_mailer_type select Local only" | debconf-set-selections
echo "postfix postfix/mailname string proxmox.localdomain" | debconf-set-selections
apt-get install -y proxmox-ve postfix open-iscsi chrony

rm -vf /etc/apt/sources.list.d/{pve-enterprise,ceph}.sources

apt-get remove -y linux-image-amd64 'linux-image-6.12*' os-prober
update-grub

# Third-party binary; the ec2 deb configures it (pvestatd -> OTLP -> CloudWatch).
curl -fsSL "https://amazoncloudwatch-agent.s3.amazonaws.com/debian/amd64/latest/amazon-cloudwatch-agent.deb" \
    -o /tmp/cwagent.deb
dpkg -i /tmp/cwagent.deb

# Everything beyond a stock Proxmox. The postinst refuses unless the rebuilt Proxmox
# packages are in place, so a partial bundle fails here rather than producing a host
# that reports contract 0.
mkdir -p /tmp/inspect-proxmox-host
cd /tmp/inspect-proxmox-host
curl -fsSL "$INSPECT_PROXMOX_HOST_BUNDLE_URL" -o debs.tar
tar xf debs.tar
apt-get install -y ./*.deb
cd /
rm -rf /tmp/inspect-proxmox-host

inspect-proxmox-host-seal

echo "PROXMOX INSTALL COMPLETE: $(pveversion)"

# Final reboot: vmbr0 was created by inspect-proxmox-ec2-network at install; a clean boot
# runs every boot-time unit once before the host is snapshotted.
echo "Stage 2 complete. Rebooting..."
systemctl reboot
STAGE2

chmod +x /root/proxmox-install-stage2.sh
systemctl enable proxmox-install-stage2.service

echo "Stage 1 complete. Rebooting into Proxmox kernel..."
systemctl reboot
