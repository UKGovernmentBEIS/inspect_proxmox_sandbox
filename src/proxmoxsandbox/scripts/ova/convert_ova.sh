#!/bin/bash
# note: the uefi param doesn't actually work in proxmox, though it seems fine in VirtualBox

if [ -z "$1" ]; then
    echo "Usage: $0 <source_directory> [uefi]"
    echo "Add 'uefi' as second parameter to enable UEFI boot mode"
    exit 1
fi

if [ "$2" = "uefi" ]; then
    UEFI_MODE=1
    echo "UEFI boot mode enabled"
fi

SOURCE_DIR=$(realpath "$1")
OUTPUT_DIR="$SOURCE_DIR/converted_ovas"
UEFI_MODE=0
DOCKER_IMAGE="qcow2-ova-converter"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

docker ps || echo 'You must have Docker installed and be in the correct docker group(s) to use this script.'

set -eu

mkdir -p "$OUTPUT_DIR"

echo "Building Docker image for OVA conversion..."
docker build -t "$DOCKER_IMAGE" -f "$SCRIPT_DIR/Dockerfile.converter" "$SCRIPT_DIR"

echo "Starting conversion process in Docker container..."
docker run --rm --privileged \
    -v "$SOURCE_DIR:/source" \
    -v "$OUTPUT_DIR:/output" \
    "$DOCKER_IMAGE" "$UEFI_MODE"

set +eu
# this may fail
sudo chown $USER:$USER -R $OUTPUT_DIR || echo "Could not change ownership of $OUTPUT_DIR"

echo "Conversion complete! OVA files are available in $OUTPUT_DIR"