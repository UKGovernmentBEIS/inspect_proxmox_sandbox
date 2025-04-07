#!/bin/bash
# note: the uefi param doesn't actually work in proxmox, though it seems fine in VirtualBox

if [ -z "$1" ]; then
    echo "Usage: $0 <source_directory> [source image type] [uefi]"
    echo "Add the source image type as second parameter to specify the type of source image (qcow2, vmdk, etc.)".
    echo "Add 'uefi' as third parameter to enable UEFI boot mode"
    exit 1
fi

if [ "$2" == "" ]; then
    echo "Defaulting to qcow2 source image type"
    SOURCE_TYPE="qcow2"
else
    echo "Using $2 source image type"
    SOURCE_TYPE=$2
fi
if [ "$3" = "uefi" ]; then
    UEFI_MODE=1
    echo "UEFI boot mode enabled"
fi

SOURCE_DIR=$(realpath "$1")
OUTPUT_DIR="$SOURCE_DIR/converted_ovas"
UEFI_MODE=0
DOCKER_IMAGE="proxmox-ova-converter"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"


docker ps

if [ $? -ne 0 ]; then
    echo 'You must have Docker installed and be in the correct docker group(s) to use this script.'
    exit 1
fi


set -eu

mkdir -p "$OUTPUT_DIR"

echo "Building Docker image for OVA conversion..."
docker build -t "$DOCKER_IMAGE" -f "$SCRIPT_DIR/Dockerfile.converter" "$SCRIPT_DIR"

echo "Starting conversion process in Docker container..."
docker run --rm --privileged \
    -v "$SOURCE_DIR:/source" \
    -v "$OUTPUT_DIR:/output" \
    "$DOCKER_IMAGE" "$SOURCE_TYPE" "$UEFI_MODE"

set +eu
# this may fail
sudo chown $USER:$USER -R $OUTPUT_DIR || echo "Could not change ownership of $OUTPUT_DIR"

echo "Conversion complete! OVA files are available in $OUTPUT_DIR"