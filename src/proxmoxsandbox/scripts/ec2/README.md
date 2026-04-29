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
  - Default Host Management Configuration (DHMC) enabled in the account/region.
    To check (DHMC is enabled if `SettingValue` is a role ARN, not `$None`):

    ```bash
    aws ssm get-service-setting --region "$REGION" \
        --setting-id "arn:aws:ssm:$REGION:$(aws sts get-caller-identity --query Account --output text):servicesetting/ssm/managed-instance/default-ec2-instance-management-role" \
        --query 'ServiceSetting.SettingValue' --output text
    ```

Each instance gets a 1024 GB gp3 EBS root volume (Proxmox needs space for VM
images). Plan for that in your cost / quota budget.

> **Region footgun**: every script in this directory falls back to
> `us-east-1` if `REGION` is unset (or not exported across script
> boundaries). If you work in another region, `export REGION=...` once at
> the top of your shell and keep it for the whole flow — silent
> wrong-region errors are easy to misdiagnose.

## One-time: build the AMI

```bash
# Required:
export SUBNET_ID=subnet-xxxx
export SECURITY_GROUP_ID=sg-xxxx

# Optional (defaults shown):
export REGION=us-east-1
export INSTANCE_TYPE=m8i.2xlarge              # must support nested virtualization
export INSTANCE_NAME=proxmox                  # Name tag for the instance
# export INSTANCE_PROFILE=...                 # required unless DHMC is enabled in this account/region
# export LAUNCH_EXTRA_TAGS='{Key=team,Value=infra}'   # AWS CLI shorthand; single-quote to prevent brace expansion

# Launches m8i.2xlarge, runs the full Proxmox install via user-data, and tails
# the install log on the host until it reports complete (~5-15 min). Prints
# the instance ID and root password when done.
./launch.sh
```

Once the build instance reports complete, snapshot it:

```bash
aws ec2 create-image --region "$REGION" \
    --instance-id <instance-id> \
    --name "proxmox-ami-$(date +%Y%m%d)" \
    --description "Proxmox VE pre-installed"
```

`create-image` returns immediately with an AMI ID; the snapshot itself takes
~10–30 min to reach `available` (it's a 1024 GB EBS volume, mostly empty).
Wait for it, then terminate the build instance:

```bash
aws ec2 wait image-available --region "$REGION" --image-ids <ami-id>
aws ec2 terminate-instances --region "$REGION" --instance-ids <instance-id>
```

## Everyday: launch from the AMI

Find the AMI ID for the latest Proxmox AMI you built:

```bash
aws ec2 describe-images --region "$REGION" --owners self \
    --filters 'Name=name,Values=proxmox-ami-*' \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text
```

Launch:

```bash
aws ec2 run-instances --region "$REGION" \
    --image-id <ami-id> \
    --instance-type m8i.2xlarge \
    --cpu-options "NestedVirtualization=enabled" \
    --subnet-id "$SUBNET_ID" \
    --security-group-ids "$SECURITY_GROUP_ID"
    # add --iam-instance-profile Name=<profile> if SSM access doesn't come from DHMC
```

Boots in ~1 min, with SSM coming online ~1 min after that. Boot-time fixup
services in the AMI regenerate the hostname and SSL certificates for the new
private IP, and regenerate the root password on every fresh launch (detected
by EC2 instance-id change). Read the password via SSM (the Proxmox web UI
login is `root` against the `Linux PAM` realm with this password):

```bash
INSTANCE_ID=i-xxx

CMD_ID=$(aws ssm send-command --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters 'commands=["cat /root/root-password"]' \
    --query 'Command.CommandId' --output text)
aws ssm wait command-executed --region "$REGION" \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID"
aws ssm get-command-invocation --region "$REGION" \
    --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" \
    --query 'StandardOutputContent' --output text
```

To open the Proxmox web UI, use `experimental/connect.sh` to forward port 8006
over SSM (no inbound SG rules required); see `experimental/README.md`.

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
- AMI fixup services for hostname + SSL cert + root password regeneration on every boot.

## Other scripts

`experimental/` — optional helpers for interacting with a running host
(SSH-via-SSM tunnel for the Proxmox web UI, run-command-on-host, a test-VM
bring-up). See `experimental/README.md`. Not needed for the build/launch flow.
