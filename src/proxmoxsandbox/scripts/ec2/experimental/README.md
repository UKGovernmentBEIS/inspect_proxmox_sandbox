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

Run the host script first, then the guest one — on an isolated host the host
script ends with a ready-to-paste guest command line, so per-VPC addresses are
never hand-typed or committed.

```bash
export REGION=eu-west-2
./run-script-on-host.sh <instance-id> check-host-isolation.sh isolated
# then, in a guest console / via the guest agent:
bash check-guest-isolation.sh isolated <endpoint-ip> ...
```

Both print one `PASS`/`SKIP` line per probe and exit at the first failure. Both
take the same mode word, which is all the configuration they have:

| Mode | Meaning |
|---|---|
| `connected` (default) | An ordinary host. The guest's internet, package-registry and DNS-tunnelling targets must be **reachable** — that is the negative control proving the blocked results elsewhere in the run mean something. |
| `lockdown` | The egress lockdown marker is armed, by hand on a connected host (see CONTRIBUTING.md). Those targets must now be blocked. |
| `isolated` | Launched `--no-internet`: `lockdown`, plus the isolated VPC's AWS-level controls. |

The guest script also takes any number of `IP[:PORT]` addresses that must be
unreachable — VPC interface endpoints, a host across a peering link. Port
defaults to 443. They're site-specific, so nothing is hardcoded and the probes
`SKIP` when none are given.

For a CVE scan of the baked AMI, get the inventory separately:

```bash
./run-on-host.sh <instance-id> "pveversion -v; uname -r; dpkg-query -W -f='\${Package}\t\${Version}\n'"
```

Do that over SSH rather than SSM if you want the whole list: SSM
`send-command` truncates output at 24k and the package list alone exceeds it.

`tests/proxmoxsandboxtest/test_host_isolation_e2e.py` runs both scripts, so they
stay the single source of truth for what "isolated" means here.

All scripts honour `REGION` (default `eu-west-2`). `connect.sh` also honours
`SSH_KEY` (default `~/.ssh/id_ed25519`).

> **Footgun**: `REGION` must be **exported**, not just set, since these are
> separate scripts. If you `REGION=us-east-1 ./run-on-host.sh ...`, the var
> doesn't propagate; use `export REGION=us-east-1` first. The visible
> symptom is `InvalidInstanceId: Instances not in a valid state for account`.
