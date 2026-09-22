#!/bin/bash
# pve-qemu-kvm 11.0.3-3 plus two scsi-disk fixes from pve-qemu master (upstream qemu
# commits merged 2026-08-27), ahead of the official 11.1.1-1 release. Drop this rebuild
# once >= 11.1.1-1 is in pve-no-subscription.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

PATCHED_VERSION="11.0.3-3+aisi1"
BASE_COMMIT="c3b7a675a52c11a1c4a5873ff2bd1696df7bf98c"      # 11.0.3-3
PATCHES_COMMIT="5e08c14024a6646711fc88529942e5296b9cd676"   # master, carries 0007/0008

git clone https://git.proxmox.com/git/pve-qemu.git pve-qemu
cd pve-qemu
git checkout "$BASE_COMMIT"
git checkout "$PATCHES_COMMIT" -- \
    debian/patches/extra/0007-scsi-disk-fix-out-of-bound-read-in-WRITE-SAME.patch \
    debian/patches/extra/0008-scsi-hide-MODE-SELECT-block-size-change-behind-a-qui.patch
mv debian/patches/extra/0007-scsi-disk-fix-out-of-bound-read-in-WRITE-SAME.patch \
    debian/patches/extra/0027-scsi-disk-fix-out-of-bound-read-in-WRITE-SAME.patch
mv debian/patches/extra/0008-scsi-hide-MODE-SELECT-block-size-change-behind-a-qui.patch \
    debian/patches/extra/0028-scsi-hide-MODE-SELECT-block-size-change-behind-a-qui.patch
sed -i '/^extra\/0026-hw-scsi-lsi53c895a-gracefully-handle-re-entrant-DMA\.patch$/a extra/0027-scsi-disk-fix-out-of-bound-read-in-WRITE-SAME.patch\nextra/0028-scsi-hide-MODE-SELECT-block-size-change-behind-a-qui.patch' debian/patches/series
grep -qF extra/0027-scsi-disk-fix-out-of-bound-read-in-WRITE-SAME.patch debian/patches/series
grep -qF extra/0028-scsi-hide-MODE-SELECT-block-size-change-behind-a-qui.patch debian/patches/series
sed -i 's|url = ../mirror_qemu|url = https://git.proxmox.com/git/mirror_qemu.git|' .gitmodules
# Nope, can't answer an interactive clean in a container
sed -i 's/clean -xdfi/clean -xdff/' Makefile

mv debian/changelog debian/changelog.orig
cat > debian/changelog <<CHANGELOG_END
pve-qemu-kvm ($PATCHED_VERSION) trixie; urgency=high

  * backport scsi-disk WRITE SAME out-of-bounds read fix and MODE SELECT
    block size quirk from pve-qemu master (upstream qemu commits merged
    2026-08-27), ahead of the official 11.1.1-1 release.

 -- UK AI Security Institute <153507562+art-dsit@users.noreply.github.com>  Thu, 17 Sep 2026 13:55:04 +0000

CHANGELOG_END
cat debian/changelog.orig >> debian/changelog
rm debian/changelog.orig

apt-get update
mk-build-deps -ir -t 'apt-get -y --no-install-recommends' debian/control
make deb
cp -v ./*.deb /out/
# Source package for the GPL obligation. After the binaries: `make dsc` recreates the
# build dir and its clean step drops the meson subprojects the binary build needs.
make dsc
cp -v ./*.dsc ./*.tar.* /out/
