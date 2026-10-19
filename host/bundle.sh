#!/bin/bash
# Assemble inspect-proxmox-host-debs.tar, the one file the provisioners download: the two
# host debs plus the rebuilt Proxmox packages. Args: directories holding .deb files
# (default: build/ and rebuilds/out/*). Output: build/inspect-proxmox-host-debs.tar
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
if [ $# -eq 0 ]; then
    set -- "$HERE/build" "$HERE"/rebuilds/out/*
fi
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
for dir in "$@"; do
    find "$dir" -maxdepth 1 -name '*.deb' ! -name '*-build-deps_*' ! -name '*-dbgsym_*' -exec cp {} "$STAGE/" \;
done
for required in inspect-proxmox-host_ inspect-proxmox-host-ec2_ pve-qemu-kvm_ libpve-network-perl_ libpve-network-api-perl_; do
    ls "$STAGE"/"$required"*.deb >/dev/null 2>&1 || { echo "bundle is missing $required*.deb" >&2; exit 1; }
done
mkdir -p "$HERE/build"
tar -C "$STAGE" -cf "$HERE/build/inspect-proxmox-host-debs.tar" .
tar -tf "$HERE/build/inspect-proxmox-host-debs.tar" | sort
