#!/bin/bash
# Usage: check-pve-qemu.sh [provisioner] [report.md]
#
# Compares the newest pve-qemu-kvm in Proxmox's pve-test and pve-no-subscription
# repositories against STOCK_VERSION in the provisioner's pve-qemu-kvm rebuild.
# For each other version it finds, reports which QEMU_FIXES that build already
# carries. Exits 0 when every repository is at STOCK_VERSION, 1 when the report
# needs acting on (rebase the rebuild, or drop it), 2 on error.
set -euo pipefail

provisioner=${1:-src/proxmoxsandbox/scripts/ec2/userdata.sh}
report=${2:-/dev/stdout}
SUITE=trixie
MIRROR=http://download.proxmox.com/debian/pve
trap 'echo "check-pve-qemu: failed at line $LINENO" >&2; exit 2' ERR

have() { git -C "$1" cat-file -e "$2^{commit}" 2>/dev/null || git -C "$1" fetch -q origin "$2"; }
var() { sed -nE "s/^$1=\"([^\"]*)\"$/\1/p" "$provisioner" | head -1; }
STOCK_VERSION=$(var STOCK_VERSION)
QEMU_FIXES=$(var QEMU_FIXES)
[ -n "$STOCK_VERSION" ] && [ -n "$QEMU_FIXES" ]

work=$(mktemp -d)
cd "$work"

# Highest pve-qemu-kvm in each repository: "<repo> <version> <filename> <sha256>"
for repo in pve-test pve-no-subscription; do
    curl -fsS "$MIRROR/dists/$SUITE/$repo/binary-amd64/Packages.gz" | gunzip |
        awk -v RS= -v repo="$repo" '/^Package: pve-qemu-kvm\n/ {
            match($0, /\nVersion: [^\n]+/);  v = substr($0, RSTART + 10, RLENGTH - 10)
            match($0, /\nFilename: [^\n]+/); f = substr($0, RSTART + 11, RLENGTH - 11)
            match($0, /\nSHA256: [^\n]+/);   s = substr($0, RSTART + 9, RLENGTH - 9)
            print repo, v, f, s }' |
        while read -r r v f s; do
            if dpkg --compare-versions "$v" gt "${best:-0}"; then best=$v; line="$r $v $f $s"; fi
            echo "$line"
        done | tail -1
done > latest.txt

if ! awk -v want="$STOCK_VERSION" '$2 != want { found = 1 } END { exit !found }' latest.txt; then
    echo "pve-qemu-kvm: every repository is at STOCK_VERSION $STOCK_VERSION"
    exit 0
fi

git clone -q --filter=blob:none --no-checkout https://git.proxmox.com/git/pve-qemu.git pve-qemu
git clone -q --filter=blob:none --bare https://gitlab.com/qemu-project/qemu.git qemu.git

{
    echo "The pve-qemu-kvm rebuild in the provisioning scripts is based on stock"
    echo "\`$STOCK_VERSION\`. Proxmox now publishes another build:"
    echo
    # One line per version: "<version> <filename> <sha256> <repo>[,<repo>]"
    awk -v want="$STOCK_VERSION" '$2 != want {
            if ($2 in repos) repos[$2] = repos[$2] " and " $1
            else { repos[$2] = $1; rest[$2] = $3 " " $4 } }
        END { for (v in repos) print v, rest[v], repos[v] }' latest.txt |
    while read -r ver file sha repo; do
        curl -fsS -o pkg.deb "$MIRROR/$file"
        echo "$sha  pkg.deb" | sha256sum -c --quiet
        src=$(dpkg-deb --fsys-tarfile pkg.deb | tar -xO ./usr/share/doc/pve-qemu-kvm/SOURCE |
            sed -nE 's/^git checkout ([0-9a-f]{40})$/\1/p')
        [ -n "$src" ]
        have pve-qemu "$src"
        stock_qemu=$(git -C pve-qemu ls-tree "$src" qemu | awk '{print $3}')
        have qemu.git "$stock_qemu"
        rm -rf st && mkdir st && git -C pve-qemu archive "$src" debian/patches | tar -x -C st
        while read -r p; do git patch-id --stable < "st/debian/patches/$p"; done < st/debian/patches/series > st.pid

        echo "### \`$ver\` in $repo"
        echo
        echo "Built from pve-qemu \`$src\`, QEMU \`$(git -C qemu.git describe --tags "$stock_qemu")\`."
        echo
        echo "| QEMU_FIXES commit | In \`$ver\` |"
        echo "| -- | -- |"
        missing=0
        for c in $QEMU_FIXES; do
            have qemu.git "$c"
            base=$(git -C qemu.git merge-base "$c" "$stock_qemu")
            pid=$(git -C qemu.git show "$c" | git patch-id --stable | cut -d' ' -f1)
            if git -C qemu.git merge-base --is-ancestor "$c" "$stock_qemu"; then
                how="yes, in QEMU history"
            elif bp=$(git -C qemu.git log -1 --format=%h --fixed-strings \
                    --grep="cherry picked from commit $c" "$base..$stock_qemu") && [ -n "$bp" ]; then
                how="yes, backported as \`$bp\`"
            elif grep -q "^$pid " st.pid; then
                how="yes, as a Proxmox patch"
            else
                how="**no**"; missing=$((missing + 1))
            fi
            echo "| \`${c:0:10}\` $(git -C qemu.git log -1 --format=%s "$c") | $how |"
        done
        echo
        if [ "$missing" = 0 ]; then
            echo "Every fix is in \`$ver\`: the rebuild can be dropped once \`$ver\` is in pve-no-subscription."
        else
            echo "Rebase the rebuild onto \`$ver\` (\`STOCK_VERSION\`, \`PVE_QEMU_BASE_COMMIT\`), keeping the fixes marked **no**."
        fi
        echo
    done
} > "$report"
exit 1
