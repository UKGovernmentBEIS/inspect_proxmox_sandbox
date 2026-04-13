#!/bin/bash
# Wait for Proxmox install to complete on an EC2 instance.
# Usage: ./wait-for-install.sh <instance-id>
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <instance-id>" >&2
    exit 1
fi

INST="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EC2_PROXMOX_PORT="${EC2_PROXMOX_PORT:-8006}"

echo "Waiting for SSM agent to come online..."
for i in {1..30}; do
    STATUS=$(aws ssm describe-instance-information --region us-east-1 \
        --filters "Key=InstanceIds,Values=$INST" \
        --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
    if [ "$STATUS" = "Online" ]; then
        echo "SSM agent online"
        break
    fi
    sleep 10
done

echo "Monitoring install log (checking every 30s)..."
while true; do
    OUTPUT=$("$SCRIPT_DIR/run-on-host.sh" "$INST" "tail -5 /root/install-proxmox.log 2>/dev/null || echo 'log not yet available'" 2>/dev/null || echo "SSM not ready")
    echo "--- $(date +%H:%M:%S) ---"
    echo "$OUTPUT"
    if echo "$OUTPUT" | grep -q "PROXMOX INSTALL COMPLETE"; then
        echo ""
        echo "Install complete! Waiting for final reboot..."
        sleep 60
        # Wait for SSM to come back after final reboot
        for i in {1..12}; do
            STATUS=$(aws ssm describe-instance-information --region us-east-1 \
                --filters "Key=InstanceIds,Values=$INST" \
                --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
            if [ "$STATUS" = "Online" ]; then
                echo "Instance ready after final reboot."
                echo ""
                PASSWORD=$("$SCRIPT_DIR/run-on-host.sh" "$INST" "cat /root/root-password" 2>/dev/null || true)
                if [ -n "$PASSWORD" ]; then
                    echo "Proxmox web UI: https://localhost:$EC2_PROXMOX_PORT"
                    echo "  Login: root / PAM"
                    echo "  Password: $PASSWORD"
                fi
                exit 0
            fi
            sleep 10
        done
        echo "Warning: SSM agent didn't come back after final reboot"
        exit 1
    fi
    sleep 30
done
