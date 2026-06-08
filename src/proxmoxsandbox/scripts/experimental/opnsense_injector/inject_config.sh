#!/bin/bash
# Inject a single file into an OPNsense nano qcow2 image using UFS2Tool.
#
# Usage: inject_config.sh <stock.qcow2> <source_file> <output.qcow2> <dest_path>
#
# The stock image is not modified. A copy with the injected file is
# written to the output path.
set -euo pipefail

STOCK_QCOW2="$1"
SOURCE_FILE="$2"
OUTPUT_QCOW2="$3"
DEST_PATH="$4"

WORK_RAW="/tmp/opnsense_work.raw"
UFS2TOOL="/opt/linux-x64/UFS2Tool"

echo "Converting qcow2 → raw..."
qemu-img convert -f qcow2 -O raw "$STOCK_QCOW2" "$WORK_RAW"

echo "Injecting $SOURCE_FILE → $DEST_PATH ..."
"$UFS2TOOL" add "$WORK_RAW" "$DEST_PATH" "$SOURCE_FILE"

echo "Setting execute permission..."
"$UFS2TOOL" chmod "$WORK_RAW" 755 "$DEST_PATH" 2>/dev/null || true

echo "Converting raw → qcow2..."
qemu-img convert -f raw -O qcow2 "$WORK_RAW" "$OUTPUT_QCOW2"

rm -f "$WORK_RAW"
echo "Done. Output: $OUTPUT_QCOW2"
