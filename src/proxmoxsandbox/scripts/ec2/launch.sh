#!/bin/bash
# Launch a Proxmox EC2 instance on an m8i type with nested virtualization.
#
# Required environment variables:
#   SUBNET_ID          - EC2 subnet ID to launch into
#   SECURITY_GROUP_ID  - Security group ID for the instance
#   INSTANCE_PROFILE   - IAM instance profile name (must include AmazonSSMManagedInstanceCore)
#
# Optional environment variables:
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

for var in SUBNET_ID SECURITY_GROUP_ID INSTANCE_PROFILE; do
    if [ -z "${!var:-}" ]; then
        echo "Error: $var is not set." >&2
        echo "Required: SUBNET_ID, SECURITY_GROUP_ID, INSTANCE_PROFILE" >&2
        exit 1
    fi
done

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

echo "Launching instance..."
INSTANCE_ID=$(aws ec2 run-instances \
    --region "$REGION" \
    --image-id "$AMI" \
    --instance-type "$INSTANCE_TYPE" \
    --iam-instance-profile "Name=$INSTANCE_PROFILE" \
    --cpu-options "NestedVirtualization=enabled" \
    --subnet-id "$SUBNET_ID" \
    --security-group-ids "$SECURITY_GROUP_ID" \
    --block-device-mappings \
        "DeviceName=/dev/xvda,Ebs={VolumeSize=1024,VolumeType=gp3,DeleteOnTermination=true}" \
    --user-data "file://$SCRIPT_DIR/userdata.sh" \
    --tag-specifications "${TAG_SPECS[@]}" \
    --query 'Instances[0].InstanceId' \
    --output text)

echo ""
echo "Launched: $INSTANCE_ID"
echo ""
echo "Monitor install progress:"
echo "  $SCRIPT_DIR/wait-for-install.sh $INSTANCE_ID"
echo ""
echo "Connect (once install complete):"
echo "  $SCRIPT_DIR/connect.sh $INSTANCE_ID"
