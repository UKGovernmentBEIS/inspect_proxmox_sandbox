#!/bin/bash
# Upload a local script to the Proxmox EC2 host and run it via SSM.
# Usage: ./run-script-on-host.sh <instance-id> <script-file> [args...]
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <instance-id> <script-file> [args...]" >&2
    exit 1
fi

INSTANCE_ID="$1"
SCRIPT_FILE="$2"
shift 2
ARGS="$*"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SCRIPT_FILE" ]; then
    echo "Error: $SCRIPT_FILE not found" >&2
    exit 1
fi

SCRIPT_B64=$(base64 -w0 "$SCRIPT_FILE")
REMOTE_PATH="/tmp/_run_script_$$.sh"

COMMAND="echo '$SCRIPT_B64' | base64 -d > $REMOTE_PATH && chmod +x $REMOTE_PATH && $REMOTE_PATH $ARGS; RC=\$?; rm -f $REMOTE_PATH; exit \$RC"

"$SCRIPT_DIR/run-on-host.sh" "$INSTANCE_ID" "$COMMAND" 600
