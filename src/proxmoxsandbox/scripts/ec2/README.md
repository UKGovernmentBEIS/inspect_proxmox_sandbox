# Proxmox VE on EC2 (Nested Virtualization)

Scripts for running Proxmox VE on AWS EC2 **m8i** instances with nested
virtualization — a cheaper alternative to bare-metal instance types.

## Prerequisites

- AWS CLI v2 (must support `--cpu-options NestedVirtualization=enabled`)
- An EC2 subnet with outbound internet access (for apt and Proxmox repos)
- A security group allowing your access pattern (SSM requires no inbound rules)
- An IAM instance profile with `AmazonSSMManagedInstanceCore` (for SSM access)

## Quick Start

```bash
# 1. Set environment variables (see Configuration below)
export SUBNET_ID=subnet-xxxx
export SECURITY_GROUP_ID=sg-xxxx
export INSTANCE_PROFILE=your-ssm-instance-profile

# 2. Launch (auto-resolves latest Debian 13 AMI)
./launch.sh

# 3. Wait for install to complete (~15 min)
./wait-for-install.sh <instance-id>

# 4. Connect with port forwarding for Proxmox web UI
./connect.sh <instance-id>
# Then browse to https://localhost:8006
# Login: root / PAM / ProxmoxAdmin2026!

# 5. Run a quick command on the host
./run-on-host.sh <instance-id> "pveversion"

# 6. Run a script on the host (e.g. create a test VM)
./run-script-on-host.sh <instance-id> ./create-test-vm.sh
```

## Installation

Fully automated via `userdata.sh`, passed as EC2 user-data at launch:

- **Stage 1**: SSM agent, EC2 Instance Connect, hostname, Proxmox repo,
  full-upgrade, `proxmox-default-kernel`, then reboot.
- **Stage 2** (automatic via systemd oneshot): `proxmox-ve`, SDN dependencies
  (`dnsmasq`, `frr`), IPAM bug patch, `vmbr0` bridge on 10.10.10.1/24,
  iptables NAT, then reboot.
- **Stage 3** (automatic): `pvenetcommit.service` promotes network config.

## VM Networking

VMs use a private 10.10.10.0/24 network on `vmbr0` (host-only bridge). The host
NATs VM traffic via iptables MASQUERADE for outbound internet access.

- VM gateway: `10.10.10.1`
- DNS: `169.254.169.253` (VPC resolver)

## Scripts

| Script                      | Purpose                                                                                                                                                            |
|-----------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `launch.sh`                 | Resolves latest Debian 13 AMI and launches a Proxmox EC2 instance with nested virtualization.                                                                     |
| `userdata.sh`               | EC2 user-data that fully automates the Proxmox installation across two reboots. Includes IPAM bug patch for static DHCP IP reservations.                          |
| `wait-for-install.sh`       | Polls SSM until the instance is reachable, then tails the install log until `PROXMOX INSTALL COMPLETE` appears.                                                   |
| `connect.sh`                | Pushes a temporary SSH key via EC2 Instance Connect, then opens an SSH session tunnelled through SSM with port 8006 forwarded.                                    |
| `ssm-proxy.sh`              | SSH `ProxyCommand` helper used by `connect.sh`. Not intended for direct use.                                                                                      |
| `run-on-host.sh`            | Runs a single shell command on the host via SSM `send-command`. Times out after 60s.                                                                              |
| `run-script-on-host.sh`     | Uploads and executes a local script on the host via SSM. Times out after 10 min.                                                                                  |
| `create-test-vm.sh`         | **(Run on host.)** Creates an SDN zone/vnet/subnet, boots an Ubuntu 24.04 cloud VM, and verifies DNS + HTTPS. Idempotent.                                         |

## Creating a Proxmox AMI

To skip the ~15 min install on future launches:

```bash
# 1. Create AMI from a running instance
aws ec2 create-image --region us-east-1 \
    --instance-id <instance-id> \
    --name "proxmox-ami-$(date +%Y%m%d)" \
    --description "Proxmox VE pre-installed"

# 2. Launch from AMI (no --user-data needed, boots in ~1 min)
aws ec2 run-instances --region us-east-1 \
    --image-id <ami-id> \
    --instance-type m8i.2xlarge \
    --iam-instance-profile Name=$INSTANCE_PROFILE \
    --cpu-options "NestedVirtualization=enabled" \
    --subnet-id $SUBNET_ID \
    --security-group-ids $SECURITY_GROUP_ID
```

## Configuration

Environment variables for `launch.sh`:

| Variable            | Req? | Default       | Description                                                              |
|---------------------|------|---------------|--------------------------------------------------------------------------|
| `SUBNET_ID`         | yes  |               | EC2 subnet ID to launch into                                             |
| `SECURITY_GROUP_ID` | yes  |               | Security group ID for the instance                                       |
| `INSTANCE_PROFILE`  | yes  |               | IAM instance profile name (must include `AmazonSSMManagedInstanceCore`)  |
| `REGION`            | no   | `us-east-1`   | AWS region                                                               |
| `INSTANCE_TYPE`     | no   | `m8i.2xlarge` | EC2 instance type (must support nested virtualization)                   |
| `INSTANCE_NAME`     | no   | `proxmox`     | Name tag for the instance                                                |
| `LAUNCH_EXTRA_TAGS` | no   | *(none)*      | Extra tags in AWS CLI shorthand, e.g. `'{Key=team,Value=infra}'`.        |

`LAUNCH_EXTRA_TAGS` must be single-quoted in shell to prevent brace expansion.

For `connect.sh`:

| Variable  | Required | Default             | Description             |
|-----------|----------|---------------------|-------------------------|
| `SSH_KEY` | no       | `~/.ssh/id_ed25519` | Path to SSH private key |
| `REGION`  | no       | `us-east-1`         | AWS region              |

## EC2-Specific Workarounds

Handled automatically by `userdata.sh`:

- **SDN dnsmasq DNS forwarding** — `/run/dnsmasq/resolv.conf` doesn't exist on
  EC2; a `tmpfiles.d` drop-in creates it pointing at the VPC resolver.
- **SSM agent** — not included in Debian 13; installed in Stage 1.
- **EC2 Instance Connect** — package not in Debian 13 repos; sshd configured
  manually with an `AuthorizedKeysCommand` that fetches temporary keys from IMDS.
- **grub-pc** — preseed install device to avoid interactive prompt on NVMe instances.
- **postfix** — preseed mailer type / mailname before `proxmox-ve` install.
- **IPAM patch** — fixes static DHCP IP reservations by MAC address.
