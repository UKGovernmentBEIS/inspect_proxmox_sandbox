#!/bin/bash
# Rebuild a Proxmox package with the AISI patches, inside a trixie container.
# Usage: build.sh pve-qemu|pve-network   -> debs, dsc and source tarballs land in out/<name>/
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${1:?usage: build.sh pve-qemu|pve-network}
[ -f "$HERE/$NAME/build.sh" ] || { echo "unknown package $NAME" >&2; exit 1; }
OUT="$HERE/out/$NAME"
# Stale output is root-owned from the previous container run, and bundle.sh would pack it.
[ -d "$OUT" ] && docker run --rm -v "$OUT:/out" debian:trixie sh -c 'rm -rf /out/*'
mkdir -p "$OUT"
docker build -q -t inspect-proxmox-rebuild "$HERE" >/dev/null
docker run --rm -v "$HERE/$NAME:/src:ro" -v "$OUT:/out" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" inspect-proxmox-rebuild bash -c 'bash /src/build.sh && chown -R "$HOST_UID:$HOST_GID" /out'
ls -l "$OUT"
