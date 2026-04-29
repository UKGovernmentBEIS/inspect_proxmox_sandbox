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
#   REGION             - AWS region (default: us-east-1)
#   INSTANCE_TYPE      - EC2 instance type (default: m8i.2xlarge)
#   INSTANCE_NAME      - Name tag for the instance (default: proxmox)
#   LAUNCH_EXTRA_TAGS  - Comma-separated AWS CLI shorthand tags, e.g.
#                        '{Key=team,Value=infra},{Key=env,Value=dev}'
#                        (single-quote in shell to prevent brace expansion)
set -euo pipefail

REGION="${REGION:-us-east-1}"
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
    --block-device-mappings
        "DeviceName=/dev/xvda,Ebs={VolumeSize=1024,VolumeType=gp3,DeleteOnTermination=true}"
    --user-data "file://$SCRIPT_DIR/userdata.sh"
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

# --- Wait for SSM agent to come online ---
echo "Waiting for SSM agent to come online..."
for i in {1..30}; do
    STATUS=$(aws ssm describe-instance-information --region "$REGION" \
        --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
        --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
    if [ "$STATUS" = "Online" ]; then
        echo "  SSM agent online"
        break
    fi
    sleep 10
done

# --- Run a shell command on the instance via SSM ---
_run_on_host() {
    local cmd="$1" timeout="${2:-60}"
    local cmd_id status
    cmd_id=$(aws ssm send-command \
        --region "$REGION" \
        --instance-ids "$INSTANCE_ID" \
        --document-name "AWS-RunShellScript" \
        --parameters "$(jq -nc --arg cmd "$cmd" '{commands:[$cmd]}')" \
        --timeout-seconds "$timeout" \
        --query 'Command.CommandId' --output text)
    for _ in $(seq 1 $((timeout / 5 + 1))); do
        status=$(aws ssm get-command-invocation --region "$REGION" \
            --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
            --query 'Status' --output text 2>/dev/null || echo InProgress)
        [[ "$status" != InProgress && "$status" != Pending ]] && break
        sleep 5
    done
    aws ssm get-command-invocation --region "$REGION" \
        --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
        --query 'StandardOutputContent' --output text
}

# --- Tail the install log on the instance until it reports complete ---
echo "Tailing install log on host (checking every 30s; install takes ~15 min)..."
while true; do
    out=$(_run_on_host "tail -5 /root/install-proxmox.log 2>/dev/null || echo 'log not yet available'" || echo "SSM not ready")
    echo "--- $(date +%H:%M:%S) ---"
    echo "$out"
    if echo "$out" | grep -q "PROXMOX INSTALL COMPLETE"; then
        echo "Install complete; waiting for final reboot..."
        sleep 60
        break
    fi
    sleep 30
done

# --- Wait for SSM to come back after the final reboot ---
for i in {1..12}; do
    STATUS=$(aws ssm describe-instance-information --region "$REGION" \
        --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
        --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
    if [ "$STATUS" = "Online" ]; then
        break
    fi
    sleep 10
done
if [ "$STATUS" != "Online" ]; then
    echo "Warning: SSM agent didn't come back after final reboot" >&2
    exit 1
fi

# --- Print the password ---
PASSWORD=$(_run_on_host "cat /root/root-password" || true)
echo
echo "Instance ready: $INSTANCE_ID"
if [ -n "$PASSWORD" ]; then
    echo "  Proxmox web UI: https://localhost:8006 (after port-forwarding via experimental/connect.sh)"
    echo "  Login: root / PAM"
    echo "  Password: $PASSWORD"
fi
