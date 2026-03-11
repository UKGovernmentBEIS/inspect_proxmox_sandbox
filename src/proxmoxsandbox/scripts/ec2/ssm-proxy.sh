#!/bin/bash
# SSH ProxyCommand wrapper for AWS SSM port forwarding.
# Usage: ssh -o ProxyCommand="ssm-proxy.sh %h %p" user@instance-id
exec aws ssm start-session \
    --region us-east-1 \
    --target "$1" \
    --document-name AWS-StartSSHSession \
    --parameters "portNumber=$2"
