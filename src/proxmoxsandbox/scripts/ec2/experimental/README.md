# Experimental scripts

Optional helpers for poking at a running Proxmox EC2 host. None of these are
needed for the build-AMI / launch-from-AMI workflow in the parent README.

| Script                  | Purpose                                                                                                  |
|-------------------------|----------------------------------------------------------------------------------------------------------|
| `connect.sh`            | Push a temporary SSH key via EC2 Instance Connect, open SSH-via-SSM with port 8006 forwarded for the Proxmox web UI. |
| `ssm-proxy.sh`          | SSH `ProxyCommand` helper used by `connect.sh`. Not for direct use.                                       |
| `run-on-host.sh`        | Run a single shell command on the host via SSM `send-command`. 60s default timeout.                       |
| `run-script-on-host.sh` | Upload + run a local script on the host via SSM. 10 min timeout.                                          |
| `create-test-vm.sh`     | Run *on the host* to bring up an Ubuntu 24.04 cloud VM in an SDN zone and verify DNS + HTTPS.             |
| `check-host-isolation.sh` | Run *on the host*: the isolation **mechanism** from `userdata.sh`.                                      |
| `check-guest-isolation.sh` | Run *inside a Linux guest*: the **effect**, i.e. what the guest can actually reach.                    |

## Isolation checks

Both check one configuration, the one the parent README's "Properly isolating
the host" describes: egress lockdown armed, no route off the VPC. On an ordinary
host they fail by design — that host is not isolated. The host script also
refuses to run against a host older than the `.aisi<N>` contract it expects,
rather than reporting the difference as failed checks.

Run the host script first: it takes no arguments and ends with a ready-to-paste
guest command line, so per-VPC addresses are never hand-typed or committed. Each
script documents its own flags in its header.

```bash
export REGION=eu-west-2
./run-script-on-host.sh <instance-id> check-host-isolation.sh
```

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
