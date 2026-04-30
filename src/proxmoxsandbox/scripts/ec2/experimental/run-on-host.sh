#!/bin/bash
# Run a command on the Proxmox EC2 host via SSM and print the output.
# Usage: ./run-on-host.sh <instance-id> <command> [timeout_seconds]
# Example: ./run-on-host.sh i-0123456789abcdef0 "tail -20 /root/install-proxmox.log"
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <instance-id> <command> [timeout_seconds]" >&2
    exit 1
fi

INSTANCE_ID="$1"
COMMAND="$2"
TIMEOUT="${3:-60}"
REGION="${REGION:-eu-west-2}"

PARAMS=$(jq -nc --arg cmd "$COMMAND" '{"commands":[$cmd]}')

CMD_ID=$(aws ssm send-command \
    --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters "$PARAMS" \
    --timeout-seconds "$TIMEOUT" \
    --query 'Command.CommandId' --output text)

# Poll until complete, up to timeout
MAX_POLLS=$(( TIMEOUT / 5 + 1 ))
for i in $(seq 1 "$MAX_POLLS"); do
    STATUS=$(aws ssm get-command-invocation \
        --region "$REGION" \
        --command-id "$CMD_ID" \
        --instance-id "$INSTANCE_ID" \
        --query 'Status' --output text 2>/dev/null || echo "InProgress")
    if [ "$STATUS" != "InProgress" ] && [ "$STATUS" != "Pending" ]; then
        break
    fi
    sleep 5
done

aws ssm get-command-invocation \
    --region "$REGION" \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --query 'StandardOutputContent' --output text

STDERR=$(aws ssm get-command-invocation \
    --region "$REGION" \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --query 'StandardErrorContent' --output text 2>/dev/null || true)
if [ -n "$STDERR" ] && [ "$STDERR" != "None" ]; then
    echo "--- STDERR ---" >&2
    echo "$STDERR" >&2
fi

if [ "$STATUS" = "Failed" ]; then
    exit 1
fi
