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
| `check-host-isolation.sh` | Run *on the host* to check the isolation **mechanism** from `userdata.sh`: units installed, enabled and last run OK, plus the rules they install. Refuses to run at all against a host older than the `.aisi<N>` contract it expects, rather than reporting the difference as failed checks. |
| `check-guest-isolation.sh` | Run *inside a Linux guest* to check the **effect**: what the guest can actually reach. |
| `scenarios-host-isolation.sh` | Run *on the host* to drive it **between** states — arm and disarm the marker, re-run the unit, unstamp the contract — and check what it does on the way. |

## Isolation checks

Both scripts check one configuration, the one the parent README's "Properly
isolating the host" describes: egress lockdown armed, no route off the VPC. On
an ordinary host they fail by design — that host is not isolated.

Run the host script first, then the guest one; the host script takes no
arguments and ends with a ready-to-paste guest command line, so per-VPC
addresses are never hand-typed or committed.

```bash
export REGION=eu-west-2
./run-script-on-host.sh <instance-id> check-host-isolation.sh
# then, in a guest console / via the guest agent:
bash check-guest-isolation.sh --host-addr 10.0.1.4 --unreachable 10.0.1.6 ...
```

Both print one line per probe — `PASS`, `FAIL`, `SKIP` or `INFO` — run every
probe, and end with a summary line. The exit status is nonzero if any probe
failed.

The guest script's two flags both repeat. `--host-addr IP` is an address the
host holds, and each one gets the full host-service port battery; passing the
gateways of vnets the guest is *not* on is the only way it can probe another
sample's segment, so run the host script while this sample is up.
`--unreachable IP[:PORT]` is an address that must not be reachable — a VPC
interface endpoint, a host across a peering link; the port defaults to 443, and
an IPv6 literal needs brackets to carry one (`[fd00::1]:8443`). Both are
site-specific, so nothing is hardcoded: with no `--host-addr` the guest's
default gateway stands in for the host, and the `--unreachable` probes `SKIP`.

## Transition scenarios

`check-host-isolation.sh` asserts a state. `scenarios-host-isolation.sh` changes
the state and asserts the behaviour: that removing the marker reopens port 53
within the timer's minute, that re-running the unit is a no-op, that an
unstamped `pvecfg.pm` makes the check script refuse to run. It picks its
scenarios from the host's current marker state, so run it on a host launched
locked down *and* on one launched open — each is the other's negative control.
It restores the marker, the stamp and any masked unit on exit.

```bash
./run-script-on-host.sh <instance-id> scenarios-host-isolation.sh
```

Two scenarios are opt-in because they break the host. `--fail-deadly` stops
`pve-cluster`, so every `pvesh` call fails, and starts the lockdown with and
without the marker: only the marked run trips `OnFailure` and masks the API. It
then checks the other direction — that a run killed mid-flight, or a burst of
restarts, does *not* trip it. Everything is unmasked afterwards.
`--arm-ordering-test` delays `pve-cluster` by 45s, arms the marker and reboots —
the only way to exercise `After=pve-cluster.service`, since that ordering does
nothing outside boot. Re-run with `--check-ordering-test` once it is back.

For a CVE scan of the baked AMI, get the inventory separately:

```bash
./run-on-host.sh <instance-id> "pveversion -v; uname -r; dpkg-query -W -f='\${Package}\t\${Version}\n'"
```

Do that over SSH rather than SSM if you want the whole list: SSM
`send-command` truncates output at 24k and the package list alone exceeds it.

These are operator tools, run by no test. CI runs `pytest -m "not req_proxmox"`,
and the manual `req_proxmox` suite needs a host with internet access, so
`test_host_isolation_e2e.py` asserts only the part that holds without the
lockdown and the VPC controls: the AMI's own blocks on guests reaching host
services and cloud metadata.

All scripts honour `REGION` (default `eu-west-2`). `connect.sh` also honours
`SSH_KEY` (default `~/.ssh/id_ed25519`).

> **Footgun**: `REGION` must be **exported**, not just set, since these are
> separate scripts. If you `REGION=us-east-1 ./run-on-host.sh ...`, the var
> doesn't propagate; use `export REGION=us-east-1` first. The visible
> symptom is `InvalidInstanceId: Instances not in a valid state for account`.
