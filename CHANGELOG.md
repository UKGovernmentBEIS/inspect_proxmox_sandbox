# Changelog

## Unreleased

- Storage uploads and built-in image downloads use `httpx` instead of `pycurl`, so the package no longer needs libcurl; uploads are still streamed from disk and cancel cleanly
- Fix: run large storage uploads off the asyncio event loop again (in a worker thread, while staying cancellable), so concurrent VM provisioning no longer starves the loop and times out other Proxmox API calls with `ConnectTimeout`
- `VmConfig.depends_on`: a VM is created only once the named VMs are ready. See "Dependency-based VM startup" in the README
- `VmConfig.healthcheck`: compose-style guest command that gates a VM's readiness. See "Healthchecks" in the README
- **Breaking:** every VM except the first `is_sandbox` VM must be named; that one defaults to `default`. See "VM Names" in the README
- VM readiness polls are at most 5 s apart, and a VM that never runs or whose guest agent never answers fails with `VmNotRunningError` / `GuestAgentUnavailableError`
- Reject malformed guest-agent replies with `GuestAgentTamperError`. Limit accepted file data, command output and quoted error text; limit how long commands wait; and close cancelled uploads with a time limit on cleanup.
- Bundled provisioning scripts: under the egress lockdown the host now REJECTs guest DNS to port 53 instead of accepting it; the lockdown unit only halts the API on its own verdict; hosts carry a `.aisi<N>` contract stamp in the API version that survives `pve-manager` upgrades
- Every VM now gets a serial port (`serial0: socket`)
- Clamp file-read at 16MB and hence allow Inspect's agent bridge to work
- Anchor the ephemeral SDN zone regex so automatic cleanup is less likely to delete unrelated pre-existing zones
- Enable extra custom headers in requests to Proxmox API.
- Move the `image_storage` field from `ProxmoxSandboxEnvironmentConfig` to `ProxmoxInstanceConfig`.
- Remove the unused single-instance fields (`host` etc.) from `ProxmoxSandboxEnvironmentConfig`; infrastructure is configured via `PROXMOX_CONFIG_FILE` or `PROXMOX_*` environment variables only. Passing these fields is now silently ignored (pydantic drops extra kwargs), and old `.eval` logs still deserialize.
- Log the acquired pool instance (`host`/`port`/`node`) at `INFO`
- Opt logger into `INFO` level for consistency with Inspect core, also supporting `debug`/`trace` output when a lower `--log-level` is set
- Prevent VMs from accessing cloud instance metadata credentials, disable IPv6 for sandbox guests (when using the bundled provisioning scripts)
- Optional egress lockdown for sandbox guests (when using the bundled provisioning scripts): drops forwarded guest traffic and stops the SDN `dnsmasq` instances recursing upstream. Installed inert, so nothing changes until `/etc/inspect-proxmox-egress-lockdown` is created; see the README
- Fix: guest file / command-output reads work again on Proxmox < 9.2.
- Fix: reading a large or binary guest file / command output no longer crashes with HTTP 597 "Broken pipe" (on Proxmox >= 9.2).
- Security: redact Proxmox passwords in configuration representations and validation error messages, and omit credentials from cleanup logs
- Fix: `exec()` no longer aborts the sample when a command kills its own command-runner wrapper process
- Fix: "500 QEMU guest agent is not running" is retried for much longer (~45s -> 8m25s)
- VMs boot concurrently rather than one after another

## 0.11.0 - 2026-06-01

- Faster large `write_file` on Linux via hot-plugged ISO (reserves `sata5` on sandbox VMs); falls back to the guest-agent path on failure
- EC2 build scripts for running Proxmox on m8i instances via nested virtualization
- Don't sweep pre-existing user SDN zones/VNets during cleanup
- Fix: failed Proxmox tasks no longer polled until timeout; allow OVA VMs to omit `os_type` and set `cpu`

## 0.10.0 - 2026-04-08

- Multi-instance pool-based allocation: run evals across multiple Proxmox servers
  with automatic instance acquisition/release via `PROXMOX_CONFIG_FILE`
- Windows VM support: exec, read_file, write_file via QEMU guest agent on Windows guests
- Retry transient QEMU guest agent errors (500s from virtio-serial channel drops)
- `firewall` field on `VmConfig` to enable per-NIC Proxmox firewall
- Clean up partial infrastructure when `sample_init` fails
- Per-instance failure isolation in `task_cleanup`
- Dirty-instance detection before sample start

## 0.9.5 - 2025-11-28

- Support static IPv4 allocation

## 0.9.4 - 2025-11-25

- OOM and AMD CPU fixes in build_proxmox_auto

## 0.9.3 - 2025-11-18

- Size clones correctly in build_proxmox_auto

## 0.9.2 - 2025-11-10

- Allow vending of arbitrary qcow images in build_proxmox_auto

## 0.9.1 - 2025-11-06

- Fix occasional failure of vended instance to stay started

## 0.9.0 - 2025-10-27

- Kali and Debian 13 built-in VMs

## 0.8.3 - 2025-10-21

- Proxmox 9.0 in build_proxmox_auto.sh; should not affect any functionality

## 0.8.2 - 2025-10-15

- Change how VM names are registered as Inspect environments

## 0.8.1 - 2025-08-06

- Fix initialization of built-in VM in certain cases
- Ensure autogenerated zone IDs do not collide with existing Proxmox zones

## 0.8.0 - 2025-07-22

- Allow pre-existing VNet in VM config

## 0.7.0 - 2025-07-14

- Increase timeout for VM CRUD operations
- Remove "restore from backup" functionality as it was unused

## 0.6.1 - 2025-05-16

- Bugfix: if no timeout is specified by Inspect for long-running exec, RetryError happens after 30s.
- Docs: correct OVA docs to reflect recent 0.6.0 change

## 0.6.0 - 2025-05-14

- Performance enhancement: OVAs generate template VMs, per-sample VMs are now linked clones.

## 0.5.1 - 2025-05-13

- Fix sandbox_cleanup=False being ignored when sample setup fails

## 0.5.0 - 2025-04-17

- Add machine type option (Linux, Windows, etc.)
- Upgrade scripted Proxmox build to version 8.4

## 0.4.0 - 2025-04-07

- Allow vmdk in convert_ova.sh
- Allow ide disk controller and e1000 network controller
- Sample CTF eval

## 0.3.1 - 2025-04-04

- Create 250G root space by default
- Scripts moved into src/proxmoxsandbox/scripts

## 0.3.0 - 2025-03-31

Initial release
