#!/usr/bin/env bash
# Monolithic script to install a virtualized proxmox instance.
# It's all in one file so that you can run it in e.g. cloud-init.
# Note for EC2 users: this is deprecated in favour of the ec2 method.
#
# NOTE: The on-first-boot.sh heredoc below shares setup logic with scripts/ec2/userdata.sh.
# If you change shared logic here, update that file too and vice versa.
#
# What it does:
# Using docker, builds a Proxmox auto-install ISO per https://pve.proxmox.com/wiki/Automated_Installation
# Using virt-manager, installs a template Proxmox VM using that auto-install ISO.
# Leaves you with a script vend.sh which you can use to create up to 10 clones of the template VM when you need a Proxmox instance.
# e.g. 
# ./vend.sh 1
# The clones will be accessible on the host at ports 11001, 11002, etc.
# Each clone will have a different root password, which is printed out by vend.sh.

set -eu

cat << 'EOFCAPACITY' > capacity.sh
TOTAL_CPUS=$(nproc)
TOTAL_MEM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')

# Use 75% of available resources for the VM
VM_CPUS=$((TOTAL_CPUS * 75 / 100))
VM_MEM_MB=$((TOTAL_MEM_KB * 75 / 100 / 1024))

VM_CPUS=$((VM_CPUS < 2 ? 2 : VM_CPUS))
VM_MEM_MB=$((VM_MEM_MB < 4096 ? 4096 : VM_MEM_MB))
EOFCAPACITY

cat << 'EOFVEND' > vend.sh
#!/usr/bin/env bash
set -eu

if [ $# -lt 1 ]; then
    echo "Usage: $0 <VM_ID> [SOURCE_QCOW_DISK]"
    echo "  VM_ID: Numeric ID for the new VM (e.g., 1, 2, 3)"
    echo "  SOURCE_QCOW_DISK: Optional path to source qcow2 disk to use as backing file"
    exit 1
fi

source ./capacity.sh

VM_ID=$1
SOURCE_QCOW_DISK=${2:-}
VM_ORIG=proxmox-auto
VM_NEW="proxmox-clone-$VM_ID"
VM_NEW_DISK="/var/lib/libvirt/images/$VM_NEW.qcow2"
PROXMOX_EXPOSED_PORT=$(( 11000 + $VM_ID ))

SKIP=""

if [ -n "$SOURCE_QCOW_DISK" ]; then
    SKIP="--skip-copy vda"
fi

virt-clone --original "$VM_ORIG" \
               --name "$VM_NEW" \
               --file "$VM_NEW_DISK" $SKIP \
              --check disk_size=off

if [ -n "$SOURCE_QCOW_DISK" ]; then

    SOURCE_DIR=$(dirname "$SOURCE_QCOW_DISK")
    SOURCE_BASENAME=$(basename "$SOURCE_QCOW_DISK" .qcow2)
    LINKED_CLONE="$SOURCE_DIR/${SOURCE_BASENAME}-linked-$VM_ID.qcow2"
    # Get the actual disk path that virt-clone created
    CLONED_DISK=$(virsh domblklist "$VM_NEW" | grep vda | awk '{print $2}')

    echo "Creating linked clone: $LINKED_CLONE"
    qemu-img create -f qcow2 -b "$SOURCE_QCOW_DISK" -F qcow2 "$LINKED_CLONE"

    # Update the VM definition to use the linked clone
    EDITOR="sed -i \"s|$CLONED_DISK|$LINKED_CLONE|g\"" virsh edit "$VM_NEW"
fi

root_password=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c 20)

# Set root password using guestfish with an explicit mount of the root LV.
# The previous method using virt-sysprep, while cleaner than this, was too slow
# virt-sysprep runs full disk inspection (including LVM PV scan) which is
# slow on large disks. guestfish with -m skips inspection entirely.
DISK_TO_EDIT="${LINKED_CLONE:-$VM_NEW_DISK}"
hashed_password=$(openssl passwd -6 "$root_password")
SHADOW_TMP=$(sudo mktemp)
sudo guestfish --rw -a "$DISK_TO_EDIT" -m /dev/pve/root download /etc/shadow "$SHADOW_TMP"
sudo sed -i "s|^root:[^:]*:|root:${hashed_password}:|" "$SHADOW_TMP"
sudo guestfish --rw -a "$DISK_TO_EDIT" -m /dev/pve/root upload "$SHADOW_TMP" /etc/shadow
sudo rm -f "$SHADOW_TMP"

EDITOR="sed -i 's/hostfwd=tcp::[0-9]\+-:8006/hostfwd=tcp::$PROXMOX_EXPOSED_PORT-:8006/'" virsh edit "$VM_NEW"

virsh setmaxmem "$VM_NEW" ${VM_MEM_MB}M --config
virsh setmem "$VM_NEW" ${VM_MEM_MB}M --config
virsh setvcpus "$VM_NEW" $VM_CPUS  --maximum  --config

virsh autostart "$VM_NEW"
virsh start "$VM_NEW"

echo "Created VM $VM_NEW on port $PROXMOX_EXPOSED_PORT with root password $root_password"
echo "You can remove it with the following command:"
echo "virsh destroy $VM_NEW; virsh undefine --nvram --remove-all-storage $VM_NEW"

# only full "which" supports the -s flag, hence use of "command"
if ! command which -s ec2-metadata; then
    echo "ec2-metadata not found; you need to figure out PROXMOX_HOST yourself"
else
    echo "PROXMOX_HOST=$(ec2-metadata  --local-ipv4 | cut -d ' ' -f 2)"
fi
echo "PROXMOX_PORT=$PROXMOX_EXPOSED_PORT"
echo "PROXMOX_USER=root"
echo "PROXMOX_REALM=pam"
echo "PROXMOX_PASSWORD=$root_password"
echo "PROXMOX_NODE=proxmox"
echo "PROXMOX_VERIFY_TLS=0"
EOFVEND
chmod +x ./vend.sh

if [ "${VEND_VEND_SCRIPT:-}" = "1" ]; then
    echo "VEND_VEND_SCRIPT=1: wrote vend.sh and capacity.sh, skipping full build."
    exit 0
fi

virsh destroy proxmox-auto || echo "not removing proxmox-auto; not found"
virsh undefine --nvram --remove-all-storage proxmox-auto || true

docker ps || echo 'You must have Docker installed and be in the correct docker group(s) to use this script.'

sudo apt update
sudo apt install -y virt-manager libvirt-clients libvirt-daemon-system qemu-system-x86 virtinst guestfs-tools
sudo usermod --append --groups libvirt $(whoami)

cat << 'EOFANSWERS' > answers.toml
[global]
keyboard = "en-gb"
country = "gb"
fqdn = "proxmox.local"
mailto = "root@localhost"
timezone = "Europe/London"
root-password = "Password2.0"
reboot-mode = "power-off"

[network]
source = "from-dhcp"

[disk-setup]
filesystem = "ext4"
disk-list = ["vda"]
lvm.maxroot = 250

[first-boot]
source = "from-iso"
ordering = "fully-up"
EOFANSWERS

cat << 'EOFONFIRSTBOOT' > on-first-boot.sh
#!/usr/bin/env bash
set -eu

# should not be necessary as this should only be run once
# but for some reason this is not always working
# possibly related to why reboot-mode = "power-off" also isn't working
if [ -f /var/local/inspect-proxmox-on-first-boot.done ]; then
  exit 0
fi

# enable serial console
systemctl enable serial-getty@ttyS0
systemctl start serial-getty@ttyS0

# fix up local to allow things we need
pvesh set /storage/local -content iso,vztmpl,backup,snippets,images,rootdir,import

# set to no-subscription PVE repo
echo 'Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Signed-By: /etc/apt/trusted.gpg.d/proxmox-release-trixie.gpg
Components: pve-no-subscription' > /etc/apt/sources.list.d/pve-no-subscription.sources
rm -f /etc/apt/sources.list.d/{pve-enterprise,ceph}.sources

# install dnsmasq for SDN, and xterm so we can use the resize command in terminal windows
apt update
apt upgrade -y
apt install -y dnsmasq xterm patch jq
systemctl disable --now dnsmasq

# Fix IPAM bug, see https://forum.proxmox.com/threads/ipam-reserving-dhcp-leases-via-mac-addresses.174704/
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

# modify version to indicate we patched
sed -i "s/\('version' => '[0-9]\+\.[0-9]\+\.[0-9]\+\)',/\1.aisi1',/" /usr/share/perl5/PVE/pvecfg.pm

# Host isolation - see README
# Delete our own rules (matched by comment) then recreate, so the rule set
# converges regardless of prior state.
# NOTE: keep these rules in sync with the fixup-firewall service in
# scripts/ec2/userdata.sh.
NIC=$(ip route show default | awk '{print $5}' | head -1)
[ -z "$NIC" ] && { echo "ERROR: no default route; cannot isolate host" >&2; exit 1; }
C="inspect-proxmox-sandbox: host-isolation"
pvesh get /nodes/proxmox/firewall/rules --output-format json \
    | jq -r --arg c "$C" 'map(select(.comment == $c)) | sort_by(.pos) | reverse | .[].pos' \
    | while read -r pos; do pvesh delete /nodes/proxmox/firewall/rules/"$pos"; done
pvesh create /nodes/proxmox/firewall/rules --type in --action ACCEPT --proto tcp --dport 8006 --iface "$NIC" --enable 1 --comment "$C"
pvesh create /nodes/proxmox/firewall/rules --type in --action ACCEPT --proto tcp --dport 22 --iface "$NIC" --enable 1 --comment "$C"
pvesh create /nodes/proxmox/firewall/rules --type in --action ACCEPT --proto udp --dport 53 --enable 1 --comment "$C"
pvesh create /nodes/proxmox/firewall/rules --type in --action ACCEPT --proto tcp --dport 53 --enable 1 --comment "$C"
pvesh create /nodes/proxmox/firewall/rules --type in --action ACCEPT --proto udp --dport 67 --enable 1 --comment "$C"
pvesh set /nodes/proxmox/firewall/options --enable 1
pvesh set /cluster/firewall/options --enable 1

# IPv6 is not supported for sandbox guests on this provider. SDN vnet bridges are
# created per sample with generated names, so we can't pin a rule to them; instead
# default.disable_ipv6 makes every interface created after boot (i.e. the vnets)
# come up with no IPv6. The already-up management NIC keeps its own setting.
cat > /etc/sysctl.d/99-inspect-proxmox-disable-ipv6.conf << 'SYSCTL_V6'
net.ipv6.conf.default.disable_ipv6 = 1
SYSCTL_V6

# Confine sandbox guests at the host's forwarding layer. Re-applied every boot
# because the template is powered off below and later cloned into fresh instances;
# iptables rules live in kernel state and don't survive the clone/reboot.
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

[Service]
Type=oneshot
ExecStart=/usr/local/bin/inspect-proxmox-block-cloud-metadata.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
BLOCK_METADATA_UNIT

systemctl daemon-reload
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

touch /var/local/inspect-proxmox-on-first-boot.done

# shut down to signal to virt-install that installation is complete
# in theory this isn't necessary because of 'reboot-mode = "power-off"' but that doesn't seem to work.
poweroff
EOFONFIRSTBOOT

# Pre-pull base image from a pull-through cache if configured, e.g.:
# DEBIAN_BASE_IMAGE=123456789.dkr.ecr.eu-west-2.amazonaws.com/docker-hub/library/debian:bookworm-slim
if [ -n "${DEBIAN_BASE_IMAGE:-}" ]; then
    docker pull "$DEBIAN_BASE_IMAGE"
    docker tag "$DEBIAN_BASE_IMAGE" debian:bookworm-slim
fi

cat << 'EOFDOCKER' > Dockerfile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
     gnupg \
     wget \
     xorriso \
     && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /iso

RUN wget -q -O /iso/proxmox.iso https://enterprise.proxmox.com/iso/proxmox-ve_9.0-1.iso

# Confusingly, although Proxmox 9 is based on trixie (Debian 13), this Docker build
# must continue to use bookworm (Debian 12) because there are not yet any trixie proxmox packages.
RUN echo "deb http://download.proxmox.com/debian/pve/ bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve.list
RUN wget -O- http://download.proxmox.com/debian/proxmox-release-bookworm.gpg | apt-key add -

RUN apt-get update && apt-get install -y \
     proxmox-auto-install-assistant \
     && rm -rf /var/lib/apt/lists/*

COPY answers.toml /iso/answers.toml
COPY on-first-boot.sh /iso/on-first-boot.sh

RUN cd /iso && proxmox-auto-install-assistant prepare-iso /iso/proxmox.iso --fetch-from iso --answer-file /iso/answers.toml --on-first-boot /iso/on-first-boot.sh
# Set volume to access the ISO
VOLUME /output

# Default command to copy the ISO to the output volume
CMD ["cp", "/iso/proxmox-auto-from-iso.iso", "/output/"]
EOFDOCKER

docker build -t proxmox-auto-install .
docker run --rm -v $(pwd):/output proxmox-auto-install
sudo cp -v proxmox-auto-from-iso.iso /var/lib/libvirt/images


# Previously there were loads of problems with permissions here when attempting to use the ubuntu user.
# Something to do with running in cloud-init; it worked fine when logged in with ubuntu in a normal termainl.
# I gave up and just used sudo.
# Disk size is hard-coded, but because check disk_size=off is used, it will not take up the full amount at the start.
cat << 'EOFVIRTINST' > virt-inst-proxmox.sh
source ./capacity.sh

virt-install --name proxmox-auto \
    --memory $VM_MEM_MB \
    --vcpus $VM_CPUS \
    --disk size=2000 \
    --cdrom '/var/lib/libvirt/images/proxmox-auto-from-iso.iso' \
    --os-variant debian12 \
    --network none \
    --graphics none \
    --console pty,target_type=serial \
    --boot uefi \
    --cpu host-passthrough \
    --qemu-commandline='-device virtio-net,netdev=user.0,addr=8 -netdev user,id=user.0,hostfwd=tcp::10000-:8006' \
    --check disk_size=off
EDITOR="sed -i '/<disk type=.*device=.cdrom/,/<\/disk>/d'" virsh edit proxmox-auto
touch virt-inst-proxmox.complete
chmod go+r virt-inst-proxmox.complete
EOFVIRTINST

chmod +x virt-inst-proxmox.sh
sudo tmux new-session -d -s virt-inst-proxmox -x 80 -y 10 "./virt-inst-proxmox.sh 2>&1 | tee virt-inst-proxmox.log"

yes | watch --errexit --exec sudo tmux capture-pane -pt virt-inst-proxmox:0.0 || true

if [ -f virt-inst-proxmox.complete ];
then
    echo 'Script complete. Run ./vend.sh 1 to create a fresh clone of the Proxmox VM.'
else
    echo 'Error building proxmox-auto. Check virt-inst-proxmox.log'
fi
