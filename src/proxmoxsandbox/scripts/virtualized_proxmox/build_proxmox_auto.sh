#!/usr/bin/env bash
# Monolithic script to install a virtualized proxmox instance.
# It's all in one file so that you can run it in e.g. cloud-init.
# Note for EC2 users: AWS does not support nested virtualization so you will
# need a metal instance for this to work.
#
# What it does:
# Using docker, builds a Proxmox auto-install ISO per https://pve.proxmox.com/wiki/Automated_Installation
# Using virt-manager, installs a template Proxmox VM using that auto-install ISO.
# Leaves you with a script vend.sh which you can use to create up to 10 clones of the template VM when you need a Proxmox instance.
# e.g. 
# ./vend.sh 1
# The clones will be accessible on the host at ports 11001, 11002, etc.
# Each clone will have a different root password, which is printed out by vend.sh.
#
# It's also possible to use pre-prepared qcow2 disks containing a
# Proxmox installation with existing VMs and other configuration.
# In that case, this script can be used to prepare the outer machine
# without actually installing Proxmox.
# To do that, pass `--no-install-proxmox-auto`.

set -eu

docker ps || echo 'You must have Docker installed and be in the correct docker group(s) to use this script.'

sudo apt update
sudo apt install -y virt-manager libvirt-clients libvirt-daemon-system qemu-system-x86 virtinst guestfs-tools
sudo usermod --append --groups libvirt $(whoami)

cat << 'EOFVEND' > vend.sh
#!/usr/bin/env bash
set -eu

if [ $# -lt 1 ]; then
    echo "Usage: $0 <VM_ID> [SOURCE_QCOW_DISK]"
    echo "  VM_ID: Numeric ID for the new VM (e.g., 1, 2, 3)"
    echo "  SOURCE_QCOW_DISK: Optional path to source qcow2 disk to use as backing file"
    exit 1
fi

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

# for some reason the hostkeys are not regenerated and proxmox complains about missing /etc/ssh/ssh_host_rsa_key.pub
# virt-sysprep needs root to be able to access the kernel so we need sudo; see https://bugs.launchpad.net/ubuntu/+source/linux/+bug/759725
sudo virt-sysprep -d "$VM_NEW" \
    --root-password "password:$root_password" \
    --operations "defaults,-ssh-hostkeys" \

EDITOR="sed -i 's/hostfwd=tcp::[0-9]\+-:8006/hostfwd=tcp::$PROXMOX_EXPOSED_PORT-:8006/'" virsh edit "$VM_NEW"

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
# even if we do not want to install proxmox, we still want the latest vend script, above ^

if [ "${1:-}" = "--no-install-proxmox-auto" ]; then
    echo "Skipping Proxmox auto-install setup."
    exit 0
fi

virsh destroy proxmox-auto || echo "not removing proxmox-auto; not found"
virsh undefine --nvram --remove-all-storage proxmox-auto || true

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
apt install -y dnsmasq xterm
systemctl disable --now dnsmasq

touch /var/local/inspect-proxmox-on-first-boot.done

# shut down to signal to virt-install that installation is complete
# in theory this isn't necessary because of 'reboot-mode = "power-off"' but that doesn't seem to work.
poweroff
EOFONFIRSTBOOT

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

TOTAL_CPUS=$(nproc)
TOTAL_MEM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')

# Use 75% of available resources for the VM
VM_CPUS=$((TOTAL_CPUS * 75 / 100))
VM_MEM_MB=$((TOTAL_MEM_KB * 75 / 100 / 1024))

VM_CPUS=$((VM_CPUS < 2 ? 2 : VM_CPUS))
VM_MEM_MB=$((VM_MEM_MB < 4096 ? 4096 : VM_MEM_MB))

# Previously there were loads of problems with permissions here when attempting to use the ubuntu user.
# Something to do with running in cloud-init; it worked fine when logged in with ubuntu in a normal termainl.
# I gave up and just used sudo.
# Disk size is hard-coded, but because check disk_size=off is used, it will not take up the full amount at the start.
cat << EOFVIRTINST > virt-inst-proxmox.sh
virt-install --name proxmox-auto \\
    --memory ${VM_MEM_MB} \\
    --vcpus ${VM_CPUS} \\
    --disk size=2000 \\
    --cdrom '/var/lib/libvirt/images/proxmox-auto-from-iso.iso' \\
    --os-variant debian12 \\
    --network none \\
    --graphics none \\
    --console pty,target_type=serial \\
    --boot uefi \\
    --cpu host \\
    --qemu-commandline='-device virtio-net,netdev=user.0,addr=8 -netdev user,id=user.0,hostfwd=tcp::10000-:8006' \\
    --check disk_size=off
EDITOR="sed -i '/<disk type=.*device=.cdrom/,/<\/disk>/d'" virsh edit proxmox-auto
touch virt-inst-proxmox.complete
chmod go+r virt-inst-proxmox.complete
EOFVIRTINST

chmod +x virt-inst-proxmox.sh
sudo tmux new-session -d -s virt-inst-proxmox -x 80 -y 10 "./virt-inst-proxmox.sh | tee virt-inst-proxmox.log"

yes | watch --errexit --exec sudo tmux capture-pane -pt virt-inst-proxmox:0.0 || true

if [ -f virt-inst-proxmox.complete ];
then
    echo 'Script complete. Run ./vend.sh 1 to create a fresh clone of the Proxmox VM.'
else
    echo 'Error building proxmox-auto. Check virt-inst-proxmox.log'
fi