---
name: check-pve-qemu-backports
description: Check whether a stock Proxmox pve-qemu-kvm release already contains every QEMU fix that the custom pve-qemu-kvm rebuild in the provisioning scripts (patch-pve-qemu.sh in scripts/ec2/userdata.sh and scripts/virtualized_proxmox/build_proxmox_auto.sh) backports, so the rebuild can be dropped. Use when a new pve-qemu-kvm is released, when asked "can we drop the QEMU rebuild/patch", or when bumping the rebuild.
---

# Check a stock pve-qemu-kvm against the rebuild's backports

`patch-pve-qemu.sh` (embedded in `src/proxmoxsandbox/scripts/ec2/userdata.sh` and
`src/proxmoxsandbox/scripts/virtualized_proxmox/build_proxmox_auto.sh`) rebuilds
`pve-qemu-kvm` with a `+aisiN` version and `apt-mark hold`s it. The rebuild can go
only when a released stock package contains every fix it carries. Decide by
content, never by commit titles: stable backports get reworded, and words like
"rsc" or "ahci" match unrelated commits.

The script skips the rebuild when the installed version is `ge` its
`PATCHED_VERSION`. Once a stock release outranks `PATCHED_VERSION`, new hosts get
that stock build whether or not it has the fixes. Run this check as soon as that
happens.

Work in a scratch directory outside the repo. Set `$SKILL` to this skill's directory.

## 1. List what the rebuild carries

Read the script (both copies should match) and its embedded `debian/changelog`.
The fixes come from three places:

- **QEMU bump**: commits between the submodule of `PVE_QEMU_BASE_COMMIT` and `QEMU_COMMIT`.
- **Kept Proxmox patches**: `debian/patches/extra/*` from the base series that
  survive the script's `sed` on `debian/patches/series`.
- **Added cherry-picks**: patches the script fetches itself, e.g. `AHCI_FIX_COMMIT`
  and its parent via `format-patch`.

Tie every changelog bullet to a commit in one of these; if one won't tie, find
the upstream commit through the CVE or QEMU issue before going on. `pve/*` and
`bitmap-mirror/*` are Proxmox's own feature patches, refreshed on every rebase.
They're out of scope unless the script changes them.

```bash
git clone https://git.proxmox.com/git/pve-qemu.git
git clone --filter=blob:none --no-checkout https://gitlab.com/qemu-project/qemu.git
BASE=<PVE_QEMU_BASE_COMMIT>  QEMU_COMMIT=<QEMU_COMMIT>
BASE_QEMU=$(git -C pve-qemu ls-tree "$BASE" qemu | awk '{print $3}')
git -C qemu describe --tags "$BASE_QEMU" "$QEMU_COMMIT"    # e.g. v11.0.3 v11.0.5

mkdir rb && git -C pve-qemu archive "$BASE" debian/patches | tar -x -C rb
# apply the script's exact sed to rb/debian/patches/series, then:
grep '^extra/' rb/debian/patches/series
```

## 2. Find the released version

Read the apt index for the suite: `bookworm` is PVE 8, `trixie` is PVE 9.
The index lists every version still published, so take the highest.

```bash
SUITE=trixie
curl -s http://download.proxmox.com/debian/pve/dists/$SUITE/pve-no-subscription/binary-amd64/Packages.gz \
  | gunzip | awk -v RS= '/^Package: pve-qemu-kvm\n/' | grep -E '^(Version|Filename|SHA256):'
```

Packages go `pve-test` → `pve-no-subscription` → `pve-enterprise`.
`enterprise.proxmox.com` returns 401 without a subscription key, so this check
can't see the enterprise index. A version the no-subscription index doesn't
have yet won't be in enterprise either. To confirm what an enterprise host
runs, read `apt-cache policy pve-qemu-kvm` on that host.

## 3. Map the version to source

pve-qemu has no release tags. Each `.deb` records the commit it was built from:

```bash
VER=<version>
curl -sO http://download.proxmox.com/debian/pve/dists/$SUITE/pve-no-subscription/binary-amd64/pve-qemu-kvm_${VER}_amd64.deb
sha256sum pve-qemu-kvm_${VER}_amd64.deb          # must match the index
dpkg-deb --fsys-tarfile pve-qemu-kvm_${VER}_amd64.deb | tar -xO ./usr/share/doc/pve-qemu-kvm/SOURCE
```

Cross-check with the commit that added the changelog entry. It's normally
`bump version to $VER`:

```bash
git -C pve-qemu log --format='%H %s' -1 -S"pve-qemu-kvm ($VER)" -- debian/changelog
STOCK=<commit from SOURCE>
STOCK_QEMU=$(git -C pve-qemu ls-tree "$STOCK" qemu | awk '{print $3}')
git -C qemu describe --tags "$STOCK_QEMU"
mkdir st && git -C pve-qemu archive "$STOCK" debian/patches | tar -x -C st
```

## 4. Check each fix by content

`check-commits.sh` checks every commit in a range against a ref. A commit is
present if one of these holds: it is an ancestor of the ref; its `cherry picked from` master
commit is an ancestor; a commit in the ref cites the same master commit; or a
commit in the ref has the same `git patch-id`. Every line it prints says which
of these matched.

```bash
cd qemu
$SKILL/check-commits.sh "$BASE_QEMU..$QEMU_COMMIT" "$STOCK_QEMU" | tee ../bump.txt
$SKILL/check-commits.sh "$AHCI_FIX_COMMIT~2..$AHCI_FIX_COMMIT" "$STOCK_QEMU"    # one line per added cherry-pick
```

A blobless clone fetches blobs on demand for the patch-id step. That took
about 8 minutes for a 300-commit range; a full clone is faster when there's
a lot to check.

Stock may carry a fix as a Proxmox patch rather than in its QEMU submodule, so
compare patch files too:

```bash
cd ..
for p in $(cat st/debian/patches/series); do git patch-id --stable < st/debian/patches/$p; done > st.pid
for p in $(grep '^extra/' rb/debian/patches/series); do
  pid=$(git patch-id --stable < rb/debian/patches/$p | cut -d' ' -f1)
  grep -q "^$pid " st.pid && echo "SAME $p" || echo "CHECK $p $(grep -o 'cherry picked from commit [0-9a-f]*' rb/debian/patches/$p)"
done
```

For each `CHECK` line:

- If the patch names a `cherry picked from` commit, run `check-commits.sh "<sha>^!" "$STOCK_QEMU"` on it.
- If stock has a patch with the same name but a different patch-id, Proxmox
  rebased it. Diff the `+`/`-` lines of the two and decide whether the fix survived.
- Run each added cherry-pick through the same patch-id comparison against `st.pid`.

## 5. Look at every MISSING line by hand

`Update version for X release` commits are expected to show up MISSING. For anything else:

- Read the code at the stock ref: `git -C qemu show "$STOCK_QEMU":<file>`. Is the
  fix's change there, maybe written differently?
- `grep -l <file-or-function> st/debian/patches/*/*.patch`
- Find the upstream master commit; `git describe --contains <sha>` shows which
  release first has it.
- Optionally, check `pve-qemu` master past `$STOCK` for an unreleased fix. That's
  information only, and doesn't make the fix present in the released package.

Treat target-specific fixes such as riscv or s390x as part of the
rebuild, too: the rebuild carries them and nobody has decided they don't matter.
Call them out separately rather than skipping them.

## 6. Record the result and decide

Write one row per fix: the fix, the commit the rebuild carries, the upstream
master commit, the evidence in stock (`ancestor`, `backport=<sha>`,
`patch-id=<sha>`, a patch file, or a code reference), and the stock version and
`SOURCE` commit you checked. Call the rebuild removable only when every row has
evidence.

If it is removable:

- Delete the `patch-pve-qemu.sh` block from both provisioning scripts.
- On existing hosts, run `apt-mark unhold pve-qemu-kvm`, then
  `apt-get install pve-qemu-kvm=<stock version>`. Add `--allow-downgrades` if
  the `+aisiN` version sorts above the stock one.
- Running VMs keep the old QEMU binary until they're restarted or migrated.

If it isn't removable, keep the rebuild. If stock already outranks
`PATCHED_VERSION`, rebase the rebuild onto the new stock release and raise
`PATCHED_VERSION`; otherwise new hosts silently skip it.
