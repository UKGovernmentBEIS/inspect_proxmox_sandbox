#!/bin/bash
# Build inspect-proxmox-host and inspect-proxmox-host-ec2 into build/, in a trixie
# container, with shellcheck, lintian and systemd-analyze verify as gates.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# build/ contents are root-owned from the previous container run
[ -d "$HERE/build" ] && docker run --rm -v "$HERE/build:/build" debian:trixie rm -rf /build/src /build/inspect-proxmox-host_* /build/inspect-proxmox-host-ec2_*
mkdir -p "$HERE/build/src"
cp -a "$HERE/debian" "$HERE/inspect-proxmox-host" "$HERE/inspect-proxmox-host-ec2" "$HERE/build/src/"
docker build -q -t inspect-proxmox-host-build "$HERE" >/dev/null
docker run --rm -v "$HERE/build:/build" -w /build/src -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" inspect-proxmox-host-build bash -euo pipefail -c '
    find . -type f \( -path "*/usr/libexec/*" -o -path "*/usr/sbin/*" \) -print0 \
        | xargs -0 shellcheck -S warning
    shellcheck -S warning debian/*.postinst debian/*.postrm
    # verify wants ExecStart targets to exist, so stage the payload into the throwaway container
    cp -a inspect-proxmox-host/. inspect-proxmox-host-ec2/. /
    systemd-analyze verify --man=no /usr/lib/systemd/system/inspect-proxmox-*.service /usr/lib/systemd/system/inspect-proxmox-*.timer
    dpkg-buildpackage -us -uc -b
    lintian --fail-on error,warning --tag-display-limit 0 ../*.changes
    chown -R "$HOST_UID:$HOST_GID" /build
'
ls -l "$HERE"/build/*.deb
