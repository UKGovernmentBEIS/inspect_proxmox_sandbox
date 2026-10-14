#!/bin/bash
# Launch a Proxmox EC2 instance and wait for the unattended install to finish.
# Prints the instance ID and Proxmox root password once done.
#
# Required environment variables:
#   SUBNET_ID          - EC2 subnet ID to launch into
#   SECURITY_GROUP_ID  - Security group ID for the instance
#
# Optional environment variables:
#   INSTANCE_PROFILE   - IAM instance profile name. If omitted, the instance
#                        relies on Default Host Management Configuration (DHMC)
#                        for SSM access.
#   REGION             - AWS region (default: eu-west-2)
#   INSTANCE_TYPE      - EC2 instance type (default: m8i.2xlarge)
#   INSTANCE_NAME      - Name tag for the instance (default: proxmox)
#   LAUNCH_EXTRA_TAGS  - Comma-separated AWS CLI shorthand tags, e.g.
#                        '{Key=team,Value=infra},{Key=env,Value=dev}'
#                        (single-quote in shell to prevent brace expansion)
#   BAKE_BUILT_INS     - A pip requirement for this package, e.g.
#                        'inspect-proxmox-sandbox' or
#                        'inspect-proxmox-sandbox @ git+https://...@<commit>'.
#                        If set, the host installs it and creates every built-in
#                        VM template after the install, so they are in the AMI.
#                        Adds ~40 min (kali2025.4 installs kali-linux-everything).
set -euo pipefail

REGION="${REGION:-eu-west-2}"
INSTANCE_TYPE="${INSTANCE_TYPE:-m8i.2xlarge}"
INSTANCE_NAME="${INSTANCE_NAME:-proxmox}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for var in SUBNET_ID SECURITY_GROUP_ID; do
    if [ -z "${!var:-}" ]; then
        echo "Error: $var is not set." >&2
        echo "Required: SUBNET_ID, SECURITY_GROUP_ID" >&2
        exit 1
    fi
done

# --- Resolve AMI and launch ---
echo "Resolving AMI (latest Debian 13 amd64)..."
AMI=$(aws ec2 describe-images \
    --region "$REGION" \
    --owners 136693071363 \
    --filters \
        "Name=name,Values=debian-13-amd64-*" \
        "Name=architecture,Values=x86_64" \
        "Name=state,Values=available" \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
    --output text)
echo "  AMI: $AMI"

# cloud-init transparently decompresses gzipped user-data; gzip keeps us well
# under EC2's 16 KiB limit as userdata.sh grows.
USERDATA_GZ="$(mktemp)"
trap 'rm -f "$USERDATA_GZ"' EXIT
# The bake request travels as an environment variable, exported just under the
# shebang so userdata.sh sees it however cloud-init runs the script.
{
    head -1 "$SCRIPT_DIR/userdata.sh"
    [ -z "${BAKE_BUILT_INS:-}" ] || printf 'export INSPECT_PROXMOX_BAKE_BUILT_INS=%q\n' "$BAKE_BUILT_INS"
    tail -n +2 "$SCRIPT_DIR/userdata.sh"
} | gzip -c > "$USERDATA_GZ"

INSTANCE_TAG_SPEC="ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}${LAUNCH_EXTRA_TAGS:+,$LAUNCH_EXTRA_TAGS}]"
TAG_SPECS=("$INSTANCE_TAG_SPEC")
if [[ -n "${LAUNCH_EXTRA_TAGS:-}" ]]; then
    TAG_SPECS+=(
        "ResourceType=volume,Tags=[$LAUNCH_EXTRA_TAGS]"
        "ResourceType=network-interface,Tags=[$LAUNCH_EXTRA_TAGS]"
    )
fi

RUN_ARGS=(
    --region "$REGION"
    --image-id "$AMI"
    --instance-type "$INSTANCE_TYPE"
    --cpu-options "NestedVirtualization=enabled"
    --subnet-id "$SUBNET_ID"
    --security-group-ids "$SECURITY_GROUP_ID"
    # Require IMDSv2 and keep token responses on the host. A hop limit above 1
    # allows nested sandbox VMs to retrieve host metadata credentials.
    # InstanceMetadataTags lets the OTel collector read the instance Name.
    --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=1,HttpProtocolIpv6=disabled,InstanceMetadataTags=enabled"
    --block-device-mappings
        "DeviceName=/dev/xvda,Ebs={VolumeSize=1024,VolumeType=gp3,DeleteOnTermination=true}"
    --user-data "fileb://$USERDATA_GZ"
    --tag-specifications "${TAG_SPECS[@]}"
    --query 'Instances[0].InstanceId'
    --output text
)
if [[ -n "${INSTANCE_PROFILE:-}" ]]; then
    RUN_ARGS+=(--iam-instance-profile "Name=$INSTANCE_PROFILE")
else
    echo "WARNING: No INSTANCE_PROFILE set. SSM access requires Default Host" >&2
    echo "  Management Configuration (DHMC) to be enabled in this account/region." >&2
fi

echo "Launching instance..."
INSTANCE_ID=$(aws ec2 run-instances "${RUN_ARGS[@]}")
echo "  Launched: $INSTANCE_ID"

# --- Wait for the host to report ready ---
# The SSM agent appears partway through stage 1 and drops out across the two
# reboots, so a failed send-command is just "not yet". The host writes
# /run/inspect-proxmox/ready once a boot has the fixups done and the API up,
# and (if requested) the built-in templates created; see README.md.
READY_SCRIPT='
for unit in proxmox-install-stage2 inspect-proxmox-bake-built-ins inspect-proxmox-egress-lockdown; do
    if systemctl is-failed --quiet $unit.service; then echo "FAILED $unit"; exit 0; fi
done
if [ -e /run/inspect-proxmox/ready ]; then echo "READY $(cat /root/root-password)"; exit 0; fi
echo "WAITING $(tail -n1 /root/install-proxmox.log 2>/dev/null)"
'
echo "Waiting for the host to report ready (checking every 30s; the install takes ~15 min, a bake ~40 more)..."
PASSWORD=""
while true; do
    cmd_id=$(aws ssm send-command \
        --region "$REGION" \
        --instance-ids "$INSTANCE_ID" \
        --document-name "AWS-RunShellScript" \
        --parameters "$(jq -nc --arg cmd "$READY_SCRIPT" '{commands:[$cmd]}')" \
        --query 'Command.CommandId' --output text 2>/dev/null) || { echo "--- $(date +%H:%M:%S) SSM agent not reachable yet"; sleep 30; continue; }
    status=Pending
    for _ in $(seq 1 12); do
        sleep 5
        status=$(aws ssm get-command-invocation --region "$REGION" \
            --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
            --query 'Status' --output text 2>/dev/null || echo Pending)
        [[ "$status" != InProgress && "$status" != Pending && "$status" != Delayed ]] && break
    done
    out=$(aws ssm get-command-invocation --region "$REGION" \
        --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
        --query 'StandardOutputContent' --output text 2>/dev/null || true)
    echo "--- $(date +%H:%M:%S) $status: $out"
    case "$out" in
        READY\ *) PASSWORD=${out#READY }; break ;;
        FAILED\ *) echo "${out#FAILED } failed on $INSTANCE_ID; see /root/install-proxmox.log" >&2; exit 1 ;;
    esac
    sleep 30
done

# --- Print the password ---
echo
echo "Instance ready: $INSTANCE_ID"
echo "  Proxmox web UI: https://localhost:8006 (after port-forwarding via experimental/connect.sh)"
echo "  Login: root / PAM"
echo "  Password: $PASSWORD"
