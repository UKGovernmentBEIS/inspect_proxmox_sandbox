#!/bin/bash
# Rebuild a Proxmox package with the AISI patches, inside a trixie container.
# Usage: build.sh pve-qemu|pve-network   -> debs, dsc and source tarballs land in out/<name>/
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${1:?usage: build.sh pve-qemu|pve-network}
[ -f "$HERE/$NAME/build.sh" ] || { echo "unknown package $NAME" >&2; exit 1; }
OUT="$HERE/out/$NAME"
mkdir -p "$OUT"
docker build -q -t inspect-proxmox-rebuild "$HERE" >/dev/null
docker run --rm -v "$HERE/$NAME:/src:ro" -v "$OUT:/out" inspect-proxmox-rebuild bash /src/build.sh
ls -l "$OUT"
