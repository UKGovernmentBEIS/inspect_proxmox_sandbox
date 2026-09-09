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
> `eu-west-2` if `REGION` is unset (or not exported across script
> boundaries). If you work in another region, `export REGION=...` once at
> the top of your shell and keep it for the whole flow — silent
> wrong-region errors are easy to misdiagnose.

## One-time: build the AMI

```bash
# Required:
export SUBNET_ID=subnet-xxxx
export SECURITY_GROUP_ID=sg-xxxx

# Optional (defaults shown):
export REGION=eu-west-2
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
    --security-group-ids "$SECURITY_GROUP_ID" \
    --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=1,HttpProtocolIpv6=disabled,InstanceMetadataTags=enabled"
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

Sandbox forwarding to the EC2 metadata endpoints (`169.254.169.254` and
`fd00:ec2::254`) is blocked on the Proxmox host. Processes running directly on
the host retain IMDS access. `HttpPutResponseHopLimit=1` is a backstop.

## Metrics (CloudWatch)

The instance ships a localhost CloudWatch agent that forwards Proxmox's `pvestatd`
metrics to CloudWatch using OpenTelemetry.

**IAM**: the instance role needs `cloudwatch:PutMetricData` for metrics to be
accepted. Without it the agent still runs, but the endpoint returns 403 —
harmless, you just get no metrics.

**Per-instance `Name` dimension**: every datapoint is labelled with the EC2
instance-id. It is additionally labelled with the instance `Name` tag **only if
the launcher enables instance metadata tags**. Like the hostname and root
password (see fixup services below), this is a per-launch attribute set at
`run-instances` time — it is **not** baked into the AMI. Pass
`InstanceMetadataTags=enabled` in `--metadata-options` (as `launch.sh` does for
the build instance, and as the everyday-launch example above does) or you get
instance-id only.

## Properly isolating the host

The AMI's rules stop a *sandbox VM* reaching the host and IMDS, and — with the egress
lockdown armed — the internet. They say nothing about what the *host* can reach, and a
guest that escapes to the host inherits all of it. For untrusted workloads the VPC has
to close that half.

`experimental/check-host-isolation.sh` asserts everything below; on a host with ordinary
internet access it fails by design.

**Host layer.** Arm the egress lockdown: `touch
/etc/inspect-proxmox-egress-lockdown` and start
`inspect-proxmox-egress-lockdown.service` (see CONTRIBUTING.md). It drops forwarded
traffic across the management NIC in both directions and strips SDN dnsmasq's upstream
resolver, so guests still get leases and DHCP but no recursion. A timer re-arms it, and
its `OnFailure` halts the Proxmox API rather than leaving guests connected.

**VPC layer**, five pieces:

1. **A subnet with no route to the internet** — no `0.0.0.0/0` via an internet gateway,
   no NAT gateway. The Prerequisites' "subnet with outbound internet access" is for the
   *build* instance, which needs apt and the Proxmox repos; launch the baked AMI
   somewhere with no such route.

2. **Interface endpoints with private DNS enabled** for `com.amazonaws.<region>.ssm`,
   `.ssmmessages` and `.ec2messages` — Session Manager is now the only way in — plus
   `.monitoring` if you want the CloudWatch metrics above. Their security group needs
   inbound 443 from the host's.

3. **A Route 53 Resolver DNS Firewall rule group associated with the VPC**, walled-garden
   style: an ALLOW rule listing the endpoint domains at a *lower* numeric priority than a
   BLOCK rule matching `*`. First match wins, so the ordering is the whole mechanism.
   Give the block rule the `NXDOMAIN` response — that's what the host script's
   `deb.debian.org does not resolve` check expects.

   The allow rule is not optional: DNS Firewall filters private hosted zone names too,
   including VPC endpoint names, so a bare block-all takes SSM down with everything else.
   It also matches on the domain name only and never sees the resolved address, so it
   stops resolution, not traffic to an IP literal — that's what item 1 is for. It is the
   only lever here: you [cannot filter the Amazon DNS server with security groups or
   network ACLs](https://docs.aws.amazon.com/vpc/latest/userguide/AmazonDNS-concepts.html#amazon-dns-rules).
   See the [DNS Firewall
   docs](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resolver-dns-firewall.html).

4. **A security group with no inbound rules** (SSM needs none) and outbound 443 to the
   endpoints' security group.

5. **`HttpPutResponseHopLimit=1`** — already in the launch example's
   `--metadata-options`, and a backstop rather than the primary control.

Routes to peered VPCs, transit gateways and on-prem survive all of this, and a guest that
reaches one is off the host. Nothing in the AMI knows those addresses; pass them to
`check-guest-isolation.sh` as positional `IP[:PORT]` arguments and it asserts they're
dead.

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
- A boot-time firewall rule blocking sandbox forwarding to EC2 instance metadata.
- CloudWatch OTLP metrics collector for `pvestatd` metrics — see "Metrics (CloudWatch)" above.

## Other scripts

`experimental/` — optional helpers for interacting with a running host
(SSH-via-SSM tunnel for the Proxmox web UI, run-command-on-host, a test-VM
bring-up). See `experimental/README.md`. Not needed for the build/launch flow.
