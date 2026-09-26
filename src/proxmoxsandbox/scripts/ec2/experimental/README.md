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
| `check-host-isolation.sh` | Run *on the host*: the isolation **mechanism** the `inspect-proxmox-host` debs install.                |
| `check-guest-isolation.sh` | Run *inside a Linux guest*: the **effect**, i.e. what the guest can actually reach.                    |

## Isolation checks

Both check one configuration, the one the parent README's "Properly isolating
the host" describes: egress lockdown armed, no route off the VPC. On an ordinary
host they fail by design — that host is not isolated. The host script also
refuses to run against a host older than the `.aisi<N>` contract it expects,
rather than reporting the difference as failed checks.

Run the host script first as it'll give you the correct invocation of the guest script.