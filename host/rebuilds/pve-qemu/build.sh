#!/bin/bash
# pve-qemu-kvm 11.0.3-3's packaging rebased onto upstream QEMU 11.0.5 (which carries the
# virtio-net RSC, VAPIC and io-channel-socket fixes, and the scsi-disk quirk that
# stamp-contract checks for), plus two AHCI fixes not yet in a QEMU release. Drop this
# rebuild once a pve-qemu-kvm with all of these is in pve-no-subscription.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

PATCHED_VERSION="11.0.5-0+aisi1"
BASE_COMMIT="c3b7a675a52c11a1c4a5873ff2bd1696df7bf98c"      # pve-qemu 11.0.3-3
QEMU_COMMIT="19def23adfcece2bc138cda371aa08299d574fe4"      # qemu v11.0.5
AHCI_FIX_COMMIT="a2fbf1785b7a464d61c0f60c4ad0a86d82925e5d"  # qemu master; its parent is the other fix

git clone https://git.proxmox.com/git/pve-qemu.git pve-qemu
cd pve-qemu
git checkout "$BASE_COMMIT"
# Point the submodule at upstream's own repo and at 11.0.5 instead of Proxmox's mirror.
sed -i 's|url = ../mirror_qemu|url = https://gitlab.com/qemu-project/qemu.git|' .gitmodules
git update-index --cacheinfo "160000,$QEMU_COMMIT,qemu"
# Stable fixes 11.0.3-3 carried as patches are in 11.0.5 itself; the rest still apply.
sed -i -E '/^extra\/00(03|0[6-9]|1[1-9]|2[0-5])-/d' debian/patches/series
patches="debian/patches/extra"
git init -q /tmp/qemu-ahci-fix
git -C /tmp/qemu-ahci-fix fetch -q --depth=3 \
    https://gitlab.com/qemu-project/qemu.git "$AHCI_FIX_COMMIT"
git -C /tmp/qemu-ahci-fix format-patch -1 --stdout --no-signature FETCH_HEAD~1 \
    > "$patches/0027-hw-ide-ahci-refuse-a-PIO-transfer-with-no-command-he.patch"
git -C /tmp/qemu-ahci-fix format-patch -1 --stdout --no-signature FETCH_HEAD \
    > "$patches/0028-hw-ide-ahci-clear-cur_cmd-when-the-command-list-is-u.patch"
rm -rf /tmp/qemu-ahci-fix

anchor="extra/0026-hw-scsi-lsi53c895a-gracefully-handle-re-entrant-DMA.patch"
grep -qxF "$anchor" debian/patches/series
sed -i "\|^$anchor\$|a\\
extra/0027-hw-ide-ahci-refuse-a-PIO-transfer-with-no-command-he.patch\\
extra/0028-hw-ide-ahci-clear-cur_cmd-when-the-command-list-is-u.patch" debian/patches/series
# Nope, can't answer an interactive clean in a container
sed -i 's/clean -xdfi/clean -xdff/' Makefile

mv debian/changelog debian/changelog.orig
cat > debian/changelog <<CHANGELOG_END
pve-qemu-kvm ($PATCHED_VERSION) trixie; urgency=high

  * rebase onto upstream QEMU 11.0.5, which carries the virtio-net RSC
    fixes (CVE-2026-66900, CVE-2026-63321), the VAPIC fixes and the
    io-channel-socket zero-length write fix (CVE-2026-84788); drop the
    stable patches it already includes.
  * backport the AHCI fixes refusing a PIO transfer with no command header
    and clearing cur_cmd when the command list is unmapped (upstream qemu
    commits d7f16bad8f and a2fbf1785b).

 -- AISI <coretech@dsit.gov.uk>  Wed, 30 Sep 2026 14:00:00 +0000

pve-qemu-kvm (11.0.3-3+aisi2) trixie; urgency=high

  * backport VAPIC region confinement, the matching MemoryRegion unref fix
    and the io-channel-socket zero-length write fix (CVE-2026-84788) from
    pve-qemu master, ahead of the official 11.1.1-1 release.

 -- AISI <coretech@dsit.gov.uk>  Wed, 30 Sep 2026 10:00:00 +0000

pve-qemu-kvm (11.0.3-3+aisi1) trixie; urgency=high

  * backport scsi-disk WRITE SAME out-of-bounds read fix and MODE SELECT
    block size quirk from pve-qemu master (upstream qemu commits merged
    2026-08-27), ahead of the official 11.1.1-1 release.

 -- AISI <coretech@dsit.gov.uk>  Thu, 17 Sep 2026 13:55:04 +0000

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
