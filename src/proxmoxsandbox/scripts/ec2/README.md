# Proxmox VE on EC2 (Nested Virtualization)

Run Proxmox VE on AWS EC2 **m8i** instances with nested virtualization — a
cheaper alternative to bare-metal instance types. The intended workflow is
**build a Proxmox AMI once, then launch from it many times**.

## Prerequisites

- AWS CLI v2 (must support `--cpu-options NestedVirtualization=enabled`)
- `jq`
- A subnet with outbound internet access (apt + Proxmox repos)
- A security group (SSM requires no inbound rules)
- SSM access for the build instance, via either:
  - an IAM instance profile with `AmazonSSMManagedInstanceCore` (set `INSTANCE_PROFILE`), or
  - Default Host Management Configuration (DHMC) enabled in the account/region

## One-time: build the AMI

```bash
export SUBNET_ID=subnet-xxxx
export SECURITY_GROUP_ID=sg-xxxx
export REGION=eu-west-2                      # optional, default us-east-1
# export INSTANCE_PROFILE=...                 # optional if DHMC enabled

# Launches m8i.2xlarge, runs the full Proxmox install via user-data, and tails
# the install log on the host until it reports complete (~15 min). Prints the
# instance ID and root password when done.
./launch.sh
```

`launch.sh` env vars:

| Variable            | Req? | Default       | Description                                                              |
|---------------------|------|---------------|--------------------------------------------------------------------------|
| `SUBNET_ID`         | yes  |               | EC2 subnet ID to launch into                                             |
| `SECURITY_GROUP_ID` | yes  |               | Security group ID for the instance                                       |
| `INSTANCE_PROFILE`  | no   | *(none)*      | IAM instance profile name. If unset, SSM access relies on DHMC.          |
| `REGION`            | no   | `us-east-1`   | AWS region                                                               |
| `INSTANCE_TYPE`     | no   | `m8i.2xlarge` | EC2 instance type (must support nested virtualization)                   |
| `INSTANCE_NAME`     | no   | `proxmox`     | Name tag for the instance                                                |
| `LAUNCH_EXTRA_TAGS` | no   | *(none)*      | Extra tags in AWS CLI shorthand, e.g. `'{Key=team,Value=infra}'`. Single-quote to prevent brace expansion. |

Once the build instance reports complete, snapshot it:

```bash
aws ec2 create-image --region "$REGION" \
    --instance-id <instance-id> \
    --name "proxmox-ami-$(date +%Y%m%d)" \
    --description "Proxmox VE pre-installed"
# wait for the AMI to reach 'available', then terminate the build instance
```

## Everyday: launch from the AMI

```bash
aws ec2 run-instances --region "$REGION" \
    --image-id <ami-id> \
    --instance-type m8i.2xlarge \
    --cpu-options "NestedVirtualization=enabled" \
    --subnet-id "$SUBNET_ID" \
    --security-group-ids "$SECURITY_GROUP_ID"
    # add --iam-instance-profile Name=<profile> if SSM access doesn't come from DHMC
```

Boots in ~1 min. Boot-time fixup services in the AMI regenerate the hostname
and SSL certificates for the new private IP.

## VM networking (inside the Proxmox host)

VMs run on a private 10.10.10.0/24 bridge (`vmbr0`) with the host NATing
outbound traffic via iptables MASQUERADE. VM gateway: `10.10.10.1`. DNS:
`169.254.169.253` (VPC resolver). VMs can't bind directly to the VPC subnet
because EC2 only routes to IPs on attached ENIs.

## EC2-specific bits handled by `userdata.sh`

- SSM agent (not in Debian 13 by default) — installed in stage 1.
- EC2 Instance Connect (no Debian 13 package) — sshd configured manually with
  an `AuthorizedKeysCommand` that fetches keys from IMDS.
- `grub-pc` install device preseeded for NVMe.
- `postfix` mailer type / mailname preseeded before `proxmox-ve` installs.
- IPAM patch so static-DHCP-by-MAC works
  (see <https://forum.proxmox.com/threads/ipam-reserving-dhcp-leases-via-mac-addresses.174704/>).
- `/run/dnsmasq/resolv.conf` shim for SDN dnsmasq DNS forwarding.
- AMI fixup services for hostname + SSL cert regeneration on every boot.

## Other scripts

`experimental/` — optional helpers for interacting with a running host
(SSH-via-SSM tunnel for the Proxmox web UI, run-command-on-host, a test-VM
bring-up). See `experimental/README.md`. Not needed for the build/launch flow.
