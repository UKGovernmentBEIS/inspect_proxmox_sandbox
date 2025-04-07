#!/bin/bash
# This script runs inside a Docker container, so don't try to run it yourself

SOURCE_DIR="/source"
OUTPUT_DIR="/output"
TEMP_DIR="/tmp/source_disk_to_ova_conversion"
SOURCE_TYPE=$1
UEFI_MODE=$2

set -eu
mkdir -p "$TEMP_DIR"

for source_disk_file in "$SOURCE_DIR"/*."$SOURCE_TYPE"; do
    if [ ! -f "$source_disk_file" ]; then
        echo "No $SOURCE_TYPE files found in $SOURCE_DIR"
        exit 1
    fi

    filename=$(basename "$source_disk_file" .$SOURCE_TYPE)
    echo "Processing: $filename"

    qemu-img convert -f $SOURCE_TYPE -O vdi "$source_disk_file" "$TEMP_DIR/$filename.vdi"
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
