# Experimental scripts

Optional helpers for poking at a running Proxmox EC2 host. None of these are
needed for the build-AMI / launch-from-AMI workflow in the parent README.

| Script                  | Purpose                                                                                                  |
|-------------------------|----------------------------------------------------------------------------------------------------------|
| `connect.sh`            | Push a temporary SSH key via EC2 Instance Connect, open SSH-via-SSM with port 8006 forwarded for the Proxmox web UI. |
| `ssm-proxy.sh`          | SSH `ProxyCommand` helper used by `connect.sh`. Not for direct use.                                       |
| `run-on-host.sh`        | Run a single shell command on the host via SSM `send-command`. 60s default timeout.                       |
| `run-script-on-host.sh` | Upload + run a local script on the host via SSM. 10 min timeout.                                          |
| `create-test-vm.sh`     | Run *on the host* (via `run-script-on-host.sh`) to bring up an Ubuntu 24.04 cloud VM in an SDN zone and verify DNS + HTTPS. |
| `check-host-isolation.sh` | Run *on the host* to check the isolation **mechanism** from `userdata.sh`: units installed, enabled and last run OK (guards against stale AMIs), plus the rules they install. |
| `check-guest-isolation.sh` | Run *inside a Linux guest* to check the **effect**: what the guest can actually reach. |

## Isolation checks

Run the host script first, then the guest one — the host script ends with a
ready-to-paste `--aws-endpoint` argument line, so per-VPC addresses are never
hand-typed or committed.

```bash
export REGION=eu-west-2
./run-script-on-host.sh <instance-id> check-host-isolation.sh --expect-egress-lockdown --inventory
# then, in a guest console / via the guest agent:
bash check-guest-isolation.sh --expect-no-egress --aws-endpoint <ip> ...
```

Both print one `PASS`/`FAIL`/`SKIP` line per probe and exit non-zero on any FAIL.
Shared flags:

- `--expect-egress-lockdown` (host) / `--expect-no-egress` (guest) — the host was
  launched `--no-internet`. On the guest this *inverts* the egress probes rather
  than skipping them: without it, the internet, package-registry and
  DNS-tunnelling targets must be reachable, which is the negative control proving
  the blocked results elsewhere in the run mean something.
- `--strict` — a SKIP counts as a failure. Use it for a red-team pass, where an
  unsupplied target is a gap rather than a non-issue.

Guest-only: `--aws-endpoint IP`, `--peer-target IP[:PORT]`, `--egress-target
HOST:PORT`, `--dns-name NAME`, `--internal-name NAME`, `--peer-guest IP|NAME` —
all repeatable except the last two, all site-specific, all SKIP when not given.
Host-only: `--inventory` prints PVE/QEMU/kernel/package versions for a CVE scan.

`tests/proxmoxsandboxtest/test_host_isolation_e2e.py` runs both scripts, so they
stay the single source of truth for what "isolated" means here.

All scripts honour `REGION` (default `eu-west-2`). `connect.sh` also honours
`SSH_KEY` (default `~/.ssh/id_ed25519`).

> **Footgun**: `REGION` must be **exported**, not just set, since these are
> separate scripts. If you `REGION=us-east-1 ./run-on-host.sh ...`, the var
> doesn't propagate; use `export REGION=us-east-1` first. The visible
> symptom is `InvalidInstanceId: Instances not in a valid state for account`.
