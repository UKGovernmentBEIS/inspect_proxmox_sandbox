#!/bin/bash
# SSH ProxyCommand wrapper for AWS SSM port forwarding.
# Usage: ssh -o ProxyCommand="ssm-proxy.sh %h %p" user@instance-id
# Honors REGION env var (default: us-east-1).
exec aws ssm start-session \
    --region "${REGION:-us-east-1}" \
    --target "$1" \
    --document-name AWS-StartSSHSession \
    --parameters "portNumber=$2"
