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

All scripts honour `REGION` (default `us-east-1`). `connect.sh` also honours
`SSH_KEY` (default `~/.ssh/id_ed25519`).

> **Footgun**: `REGION` must be **exported**, not just set, since these are
> separate scripts. If you `REGION=eu-west-2 ./run-on-host.sh ...`, the var
> doesn't propagate; use `export REGION=eu-west-2` first. The visible
> symptom is `InvalidInstanceId: Instances not in a valid state for account`.
