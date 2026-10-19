# inspect-proxmox-host

Debian packages holding everything a Proxmox VE node needs beyond a stock install to host
inspect_proxmox_sandbox evaluations. `scripts/ec2/userdata.sh` and
`scripts/virtualized_proxmox/build_proxmox_auto.sh` install a stock Proxmox and then these.

| Package | Contents |
|---|---|
| `inspect-proxmox-host` | Host-isolation firewall rules and storage content types (`inspect-proxmox-host-configure.service`, every boot), the pre-conntrack guard on guest traffic to host services (`inspect-proxmox-block-cloud-metadata.service`, policy in `/etc/inspect-proxmox/host-local.rules`) with the link-local/IPv6 forwarding block, the marker-gated egress lockdown with its timer and fail-deadly halt unit, the `.aisiN` contract stamp with its apt hook, `inspect-proxmox-host-seal`. Depends on the exact rebuilt `pve-qemu-kvm` and `libpve-network-perl` versions and pins them above upstream, so `apt upgrade` holds back anything that needs a newer one until this package is rebased. |
| `inspect-proxmox-host-ec2` | Boot-time units for AMI-launched hosts: node name, certs, root password keyed on instance-id, `vmbr0` plus NAT on the current management NIC, the SDN dnsmasq resolver shim, and the CloudWatch OTel pipeline for `pvestatd` metrics. |
| `pve-qemu-kvm 11.0.5-0+aisi1` | pve-qemu 11.0.3-3's packaging on upstream QEMU 11.0.5 (virtio-net RSC, VAPIC, io-channel-socket and scsi-disk fixes) plus two AHCI fixes from QEMU master. Drop once a pve-no-subscription build carries all of these. Built by `rebuilds/pve-qemu/build.sh`. |
| `libpve-network-perl 1.6.7+aisi1` | Upstream plus the IPAM fix from [pve-devel, November 2025](https://lists.proxmox.com/pipermail/pve-devel/2025-November/076472.html). Rebase per upstream release until it lands. Built by `rebuilds/pve-network/build.sh`. |

## Contract

The package version is the contract number: `inspect-proxmox-host 4` stamps `.aisi4` onto
the version `pveversion` prints and the API serves. `stamp-contract` checks that the
patched behaviour is present before stamping, so a host that has lost a patch reports
contract 0 rather than lying. Bump `debian/changelog` whenever something an off-host caller
asserts changes; `tests/proxmoxsandboxtest/test_provisioning_security.py` checks the
changelog, `HOST_CONTRACT` in the tests and the release pinned by both provisioners agree.

## Building

```bash
host/build.sh                        # the two debs, into host/build/; shellcheck + lintian gate
host/rebuilds/build.sh pve-network   # a couple of minutes
host/rebuilds/build.sh pve-qemu      # 15 min on 4 cores
```

All three run in a `debian:trixie` container, so any box with Docker will do. CI does the same
and, on a `host-v<N>` tag, attaches the debs, the source packages and the
`inspect-proxmox-host-debs.tar` bundle the provisioners download to a GitHub release.

## Releasing

1. Bump `debian/changelog` and `INSPECT_PROXMOX_HOST_RELEASE` in both provisioners; set
   `HOST_CONTRACT` in the tests to match. Bump a `PATCHED_VERSION` and the `Depends` in
   `debian/control` if a rebuild changed. Leave `INSPECT_PROXMOX_HOST_BUNDLE_SHA256` empty.
2. Merge, then tag `host-v<N>` on main. The workflow builds and publishes the release.
   Until it exists, bakes from main fail at the download.
3. Put the released bundle's sha256 (from the release's `SHA256SUMS`) into
   `INSPECT_PROXMOX_HOST_BUNDLE_SHA256` in both provisioners and merge that. From then on a
   replaced release asset fails the bake instead of being installed.
4. Re-bake any AMI built from the previous release (`scripts/ec2/README.md`).

To install a bundle from somewhere else, e.g. an S3 presigned URL while testing, set
`INSPECT_PROXMOX_HOST_BUNDLE_URL` in the provisioner (the virtualized one bakes it into the
ISO, so don't use a URL you'd mind others reading). The sha256 check is skipped while the
variable is empty.

## Upgrading and removing on a live host

`inspect-proxmox-block-cloud-metadata.service` is `RequiredBy` pvedaemon, pveproxy and
pve-guests, so restarting or stopping it stops them and every running VM. The package
therefore reloads it on upgrade and leaves it running on remove (`debian/rules`,
`debian/inspect-proxmox-host.postinst`); the other units get debhelper's usual restart.
Removing the package drops the stamp and the `.requires` links; the kernel rules it
installed stay until reboot.

## Layout

The payload directories mirror the target filesystem: `inspect-proxmox-host/usr/libexec/...`
lands at `/usr/libexec/...`. Units go in `usr/lib/systemd/system`; debhelper enables and
starts every unit with an `[Install]` section on install, which is why the halt unit has none.
`etc/` holds conffiles, so an admin's edits to `host-local.rules` survive upgrades.
