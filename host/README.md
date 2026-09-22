# inspect-proxmox-host

Debian packages holding everything a Proxmox VE node needs beyond a stock install to host
inspect_proxmox_sandbox evaluations. `scripts/ec2/userdata.sh` and
`scripts/virtualized_proxmox/build_proxmox_auto.sh` install a stock Proxmox and then these.

| Package | Contents |
|---|---|
| `inspect-proxmox-host` | Host-isolation firewall rules and storage content types (`inspect-proxmox-host-configure.service`, every boot), link-local/IPv6 forwarding block, the marker-gated egress lockdown with its timer and fail-deadly halt unit, the `.aisiN` contract stamp with its apt hook, `inspect-proxmox-host-seal`. Depends on the rebuilt `pve-qemu-kvm` and `libpve-network-perl` and pins them above upstream. |
| `inspect-proxmox-host-ec2` | Boot-time units for AMI-launched hosts: node name, certs, root password keyed on instance-id, `vmbr0` plus NAT on the current management NIC, the SDN dnsmasq resolver shim, and the CloudWatch OTel pipeline for `pvestatd` metrics. |
| `pve-qemu-kvm 11.0.3-3+aisi1` | Upstream plus two scsi-disk fixes from pve-qemu master. Drop once `>= 11.1.1-1` is in pve-no-subscription. Built by `rebuilds/pve-qemu/build.sh`. |
| `libpve-network-perl 1.6.7+aisi1` | Upstream plus the IPAM fix from [pve-devel, November 2025](https://lists.proxmox.com/pipermail/pve-devel/2025-November/076472.html). Rebase per upstream release until it lands. Built by `rebuilds/pve-network/build.sh`. |

## Contract

The package version is the contract number: `inspect-proxmox-host 3` stamps `.aisi3` onto
the version `pveversion` prints and the API serves. `stamp-contract` checks that the
patched behaviour is present before stamping, so a host that has lost a patch reports
contract 0 rather than lying. Bump `debian/changelog` whenever something an off-host caller
asserts changes; `tests/proxmoxsandboxtest/test_provisioning_security.py` checks the
changelog, `HOST_CONTRACT` in the tests and the release pinned by both provisioners agree.

## Building

```bash
host/build.sh                        # the two debs, into host/build/; shellcheck + lintian gate
host/rebuilds/build.sh pve-network   # a couple of minutes
host/rebuilds/build.sh pve-qemu      # an hour or so
```

All three run in a `debian:trixie` container, so any box with Docker will do. CI does the same
and, on a `host-v<N>` tag, attaches the debs, the source packages and the
`inspect-proxmox-host-debs.tar` bundle the provisioners download to a GitHub release.

## Releasing

1. Bump `debian/changelog` and `INSPECT_PROXMOX_HOST_RELEASE` in both provisioners; set
   `HOST_CONTRACT` in the tests to match. Bump a `PATCHED_VERSION` and the `Depends` in
   `debian/control` if a rebuild changed.
2. Merge, then tag `host-v<N>` on main. The workflow builds and publishes the release.
3. Re-bake any AMI built from the previous release (`scripts/ec2/README.md`).

To install a bundle from somewhere else, e.g. an S3 presigned URL while testing, set
`INSPECT_PROXMOX_HOST_BUNDLE_URL` in the provisioner.

## Layout

The payload directories mirror the target filesystem: `inspect-proxmox-host/usr/libexec/...`
lands at `/usr/libexec/...`. Units go in `usr/lib/systemd/system`; debhelper enables and
starts every unit with an `[Install]` section on install, which is why the halt unit has none.
