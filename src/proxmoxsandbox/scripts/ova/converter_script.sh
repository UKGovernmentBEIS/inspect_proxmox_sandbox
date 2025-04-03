#!/bin/bash
# This script runs inside a Docker container, so don't try to run it yourself
set -eu

SOURCE_DIR="/source"
OUTPUT_DIR="/output"
TEMP_DIR="/tmp/qcow2_to_ova_conversion"
UEFI_MODE=$1

mkdir -p "$TEMP_DIR"

for qcow2_file in "$SOURCE_DIR"/*.qcow2; do
    if [ ! -f "$qcow2_file" ]; then
        echo "No qcow2 files found in $SOURCE_DIR"
        exit 1
    fi

    filename=$(basename "$qcow2_file" .qcow2)
    echo "Processing: $filename"

    qemu-img convert -f qcow2 -O vdi "$qcow2_file" "$TEMP_DIR/$filename.vdi"
    VBoxManage createvm --name "$filename" --ostype Linux26_64 --register
    VBoxManage modifyvm "$filename" --memory 2048 --cpus 2 --acpi on --boot1 disk
    if [ $UEFI_MODE -eq 1 ]; then
        VBoxManage modifyvm "$filename" --firmware efi
    fi
    
    VBoxManage storagectl "$filename" --name "SATA Controller" --add sata --controller IntelAhci
    VBoxManage storageattach "$filename" --storagectl "SATA Controller" --port 0 --device 0 --type hdd --medium "$TEMP_DIR/$filename.vdi"
    VBoxManage export "$filename" --output "$OUTPUT_DIR/$filename.ova"
    VBoxManage unregistervm "$filename" --delete

    echo "Converted: $filename.ova"
done

rm -rf "$TEMP_DIR"
echo "All conversions completed!"
