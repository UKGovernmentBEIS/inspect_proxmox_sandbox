#!/bin/bash
# SSH ProxyCommand wrapper for AWS SSM port forwarding.
# Usage: ssh -o ProxyCommand="ssm-proxy.sh %h %p" user@instance-id
# Honors REGION env var (default: eu-west-2).
exec aws ssm start-session \
    --region "${REGION:-eu-west-2}" \
    --target "$1" \
    --document-name AWS-StartSSHSession \
    --parameters "portNumber=$2"
