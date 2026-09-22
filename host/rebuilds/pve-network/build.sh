#!/bin/bash
# libpve-network-perl 1.6.7 plus the IPAM fix from
# https://lists.proxmox.com/pipermail/pve-devel/2025-November/076472.html
# (reuse the IP already recorded for a MAC instead of allocating a new one).
# Rebase onto each upstream release until the fix lands there.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

PATCHED_VERSION="1.6.7+aisi1"
BASE_COMMIT="7c6c5d8f53292c311aa07cbdb8e7f59f50af38f5"   # bump version to 1.6.7

git clone https://git.proxmox.com/git/pve-network.git pve-network
cd pve-network
git checkout "$BASE_COMMIT"
patch -p1 < /src/ipam-reuse-ip-for-known-mac.patch

mv debian/changelog debian/changelog.orig
cat > debian/changelog <<CHANGELOG_END
libpve-network-perl ($PATCHED_VERSION) trixie; urgency=medium

  * subnets: in add_next_free_ip, reuse the IP already recorded in IPAM for
    the requesting MAC when it lies in the subnet, so static-DHCP-by-MAC
    survives VM re-creation. From the pve-devel patch of November 2025, with
    the lookup wrapped in eval so a failing IPAM query falls back to a fresh
    allocation instead of failing the VM start.

 -- UK AI Security Institute <153507562+art-dsit@users.noreply.github.com>  Mon, 21 Sep 2026 12:00:00 +0000

CHANGELOG_END
cat debian/changelog.orig >> debian/changelog
rm debian/changelog.orig

apt-get update
mk-build-deps -ir -t 'apt-get -y --no-install-recommends' debian/control
make dsc
make deb
cp -v ./*.deb ./*.dsc ./*.tar.* /out/
