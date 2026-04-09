#!/bin/bash
# Connect to a Proxmox EC2 instance via EC2 Instance Connect + SSM.
# Pushes a temporary SSH key, then opens an SSH session with port forwarding.
# Usage: ./connect.sh <instance-id>
#
# Optional environment variables:
#   SSH_KEY  - Path to SSH private key (default: ~/.ssh/id_ed25519)
#   REGION   - AWS region (default: us-east-1)
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <instance-id>" >&2
    exit 1
fi

INSTANCE_ID="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SSH_KEY="${SSH_KEY:-~/.ssh/id_ed25519}"
SSH_PUBKEY="${SSH_KEY}.pub"
REGION="${REGION:-us-east-1}"

if [ ! -f "$SSH_PUBKEY" ]; then
    echo "Error: $SSH_PUBKEY not found (set SSH_KEY to override)" >&2
    exit 1
fi

echo "Pushing temporary SSH key to $INSTANCE_ID..."
aws ec2-instance-connect send-ssh-public-key \
    --region "$REGION" \
    --instance-id "$INSTANCE_ID" \
    --instance-os-user admin \
    --ssh-public-key "file://$SSH_PUBKEY"

exec ssh -i "$SSH_KEY" \
    -L 8006:localhost:8006 \
    -o StrictHostKeyChecking=no \
    -o ProxyCommand="$SCRIPT_DIR/ssm-proxy.sh %h %p" \
    "admin@$INSTANCE_ID"
