#!/usr/bin/env python3
"""
Monolithic script to install a virtualized proxmox instance.
It's all in one file so that you can run it in e.g. cloud-init.
Note for EC2 users: AWS does not support nested virtualization so you will
need a metal instance for this to work.

What it does:
Using docker, builds a Proxmox auto-install ISO per https://pve.proxmox.com/wiki/Automated_Installation
Using virt-manager, installs a template Proxmox VM using that auto-install ISO.
Leaves you with a script vend.sh which you can use to create up to 10 clones of the template VM when you need a Proxmox instance.
e.g.
./vend.sh 1
The clones will be accessible on the host at ports 11001, 11002, etc.
Each clone will have a different root password, which is printed out by vend.sh.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path


def run_command(cmd, check=True, shell=False, capture_output=False):
    """Run a command and return result."""
    if isinstance(cmd, str) and not shell:
        # If string command but shell=False, split it
        cmd = cmd.split()

    try:
        result = subprocess.run(
            cmd,
            check=check,
            shell=shell,
            capture_output=capture_output,
            text=True
        )
        return result
    except Exception as e:
        if not check:
            return e
        raise


def cleanup_existing_vm():
    """Clean up any existing proxmox-auto VM."""
    print("Cleaning up existing VM...")
    run_command("virsh destroy proxmox-auto", check=False)
    run_command("virsh undefine --nvram --remove-all-storage proxmox-auto", check=False)


def check_docker():
    """Verify Docker is available."""
    print("Checking Docker...")
    result = run_command("docker ps", check=False, capture_output=True)
    if result.returncode != 0:
        print("ERROR: You must have Docker installed and be in the correct docker group(s) to use this script.")
        sys.exit(1)


def install_dependencies():
    """Install required system packages."""
    print("Installing system dependencies...")
    run_command("sudo apt update", shell=True)
    packages = [
        "virt-manager",
        "libvirt-clients",
        "libvirt-daemon-system",
        "qemu-system-x86",
        "virtinst",
        "guestfs-tools"
    ]
    run_command(f"sudo apt install -y {' '.join(packages)}", shell=True)

    # Add current user to libvirt group
    username = os.environ.get("USER", os.environ.get("USERNAME", "ubuntu"))
    run_command(f"sudo usermod --append --groups libvirt {username}", shell=True)


def create_answers_toml():
    """Create the answers.toml file for Proxmox auto-install."""
    print("Creating answers.toml...")
    content = """[global]
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
"""
    with open("answers.toml", "w") as f:
        f.write(content)


def create_on_first_boot_sh():
    """Create the on-first-boot.sh script."""
    print("Creating on-first-boot.sh...")
    content = """#!/usr/bin/env bash
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
"""
    with open("on-first-boot.sh", "w") as f:
        f.write(content)


def create_dockerfile():
    """Create the Dockerfile for building the auto-install ISO."""
    print("Creating Dockerfile...")
    content = """FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \\
     gnupg \\
     wget \\
     xorriso \\
     && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /iso

RUN wget -q -O /iso/proxmox.iso https://enterprise.proxmox.com/iso/proxmox-ve_9.0-1.iso

# Confusingly, although Proxmox 9 is based on trixie (Debian 13), this Docker build
# must continue to use bookworm (Debian 12) because there are not yet any trixie proxmox packages.
RUN echo "deb http://download.proxmox.com/debian/pve/ bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve.list
RUN wget -O- http://download.proxmox.com/debian/proxmox-release-bookworm.gpg | apt-key add -

RUN apt-get update && apt-get install -y \\
     proxmox-auto-install-assistant \\
     && rm -rf /var/lib/apt/lists/*

COPY answers.toml /iso/answers.toml
COPY on-first-boot.sh /iso/on-first-boot.sh

RUN cd /iso && proxmox-auto-install-assistant prepare-iso /iso/proxmox.iso --fetch-from iso --answer-file /iso/answers.toml --on-first-boot /iso/on-first-boot.sh
# Set volume to access the ISO
VOLUME /output

# Default command to copy the ISO to the output volume
CMD ["cp", "/iso/proxmox-auto-from-iso.iso", "/output/"]
"""
    with open("Dockerfile", "w") as f:
        f.write(content)


def build_docker_image():
    """Build the Docker image and extract the ISO."""
    print("Building Docker image...")
    run_command("docker build -t proxmox-auto-install .", shell=True)

    print("Running Docker container to extract ISO...")
    cwd = os.getcwd()
    run_command(f"docker run --rm -v {cwd}:/output proxmox-auto-install", shell=True)

    print("Copying ISO to libvirt images directory...")
    run_command("sudo cp -v proxmox-auto-from-iso.iso /var/lib/libvirt/images", shell=True)


def calculate_vm_resources():
    """Calculate VM resources based on system capacity."""
    print("Calculating VM resources...")

    # Get total CPUs
    total_cpus = os.cpu_count()

    # Get total memory in KB
    with open("/proc/meminfo", "r") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                total_mem_kb = int(line.split()[1])
                break

    # Use 75% of available resources
    vm_cpus = max(2, (total_cpus * 75) // 100)
    vm_mem_mb = max(4096, (total_mem_kb * 75) // (100 * 1024))

    print(f"Allocating {vm_cpus} CPUs and {vm_mem_mb} MB RAM to VM")
    return vm_cpus, vm_mem_mb


def create_virt_install_script(vm_cpus, vm_mem_mb):
    """Create the virt-install script."""
    print("Creating virt-install script...")
    content = f"""virt-install --name proxmox-auto \\
    --memory {vm_mem_mb} \\
    --vcpus {vm_cpus} \\
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
EDITOR="sed -i '/<disk type=.*device=.cdrom/,/<\\/disk>/d'" virsh edit proxmox-auto
touch virt-inst-proxmox.complete
chmod go+r virt-inst-proxmox.complete
"""
    with open("virt-inst-proxmox.sh", "w") as f:
        f.write(content)
    os.chmod("virt-inst-proxmox.sh", 0o755)


def create_vend_script():
    """Create the vend.sh script."""
    print("Creating vend.sh script...")
    content = """#!/usr/bin/env bash
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

virt-clone --original "$VM_ORIG" \\
               --name "$VM_NEW" \\
               --file "$VM_NEW_DISK" $SKIP \\
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
    EDITOR="sed -i \\"s|$CLONED_DISK|$LINKED_CLONE|g\\"" virsh edit "$VM_NEW"
fi

root_password=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c 20)

# for some reason the hostkeys are not regenerated and proxmox complains about missing /etc/ssh/ssh_host_rsa_key.pub
# virt-sysprep needs root to be able to access the kernel so we need sudo; see https://bugs.launchpad.net/ubuntu/+source/linux/+bug/759725
sudo virt-sysprep -d "$VM_NEW" \\
    --root-password "password:$root_password" \\
    --operations "defaults,-ssh-hostkeys" \\

EDITOR="sed -i 's/hostfwd=tcp::[0-9]\\+-:8006/hostfwd=tcp::$PROXMOX_EXPOSED_PORT-:8006/'" virsh edit "$VM_NEW"

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
"""
    with open("vend.sh", "w") as f:
        f.write(content)
    os.chmod("vend.sh", 0o755)


def start_virt_install():
    """Start the virt-install process in tmux."""
    print("Starting virt-install in tmux session...")
    run_command(
        'sudo tmux new-session -d -s virt-inst-proxmox -x 80 -y 10 "./virt-inst-proxmox.sh | tee virt-inst-proxmox.log"',
        shell=True
    )


def wait_for_completion():
    """Wait for virt-install to complete."""
    print("Monitoring installation progress...")
    # Use watch to monitor tmux output
    run_command(
        "yes | watch --errexit --exec sudo tmux capture-pane -pt virt-inst-proxmox:0.0",
        shell=True,
        check=False
    )

    # Check if installation completed successfully
    if os.path.exists("virt-inst-proxmox.complete"):
        print("\nScript complete. Run ./vend.sh 1 to create a fresh clone of the Proxmox VM.")
        return True
    else:
        print("\nError building proxmox-auto. Check virt-inst-proxmox.log")
        return False


def main():
    """Main function to orchestrate the installation."""
    print("Starting Proxmox virtualized installation...")

    try:
        cleanup_existing_vm()
        check_docker()
        install_dependencies()

        create_answers_toml()
        create_on_first_boot_sh()
        create_dockerfile()

        build_docker_image()

        vm_cpus, vm_mem_mb = calculate_vm_resources()
        create_virt_install_script(vm_cpus, vm_mem_mb)
        create_vend_script()

        start_virt_install()
        success = wait_for_completion()

        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
