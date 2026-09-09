"""End-to-end check that the Proxmox host actually isolates sandbox VMs.

Host firewall isolation is configured by the provisioning scripts (see the
README's "Host firewall isolation" section). Every supported way of standing
up a test host applies it, so this runs unconditionally as part of the
integration suite — if it fails, the host you're testing against wasn't
provisioned correctly (e.g. a hand-rolled Proxmox missing the firewall config).
"""

import os
from pathlib import Path

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)
from proxmoxsandbox.experimental.host_shell import run_script_on_host
from proxmoxsandbox.schema import ProxmoxInstanceConfig

from .proxmox_sandbox_utils import setup_sandbox

pytestmark = pytest.mark.req_proxmox

SCRIPTS = Path(__file__).parents[2] / "src/proxmoxsandbox/scripts/ec2/experimental"
HOST_SCRIPT = SCRIPTS / "check-host-isolation.sh"
GUEST_SCRIPT = SCRIPTS / "check-guest-isolation.sh"


async def test_sandbox_vm_cannot_reach_host_or_cloud_metadata(
    async_proxmox_api: AsyncProxmoxAPI, instance_config: ProxmoxInstanceConfig
) -> None:
    """A sandbox VM can't reach host services or cloud instance metadata.

    The VM reaches the host over its SDN bridge, so its packets never ingress
    on the host's management interface and hit the default-deny policy — even
    when aimed at the SDN gateway IP where pveproxy also listens. Metadata
    traffic is forwarded rather than host-bound, so provisioning also installs
    an explicit forwarding block for the fixed metadata endpoints.

    check-guest-isolation.sh proves the effect from inside the VM;
    check-host-isolation.sh checks the mechanism on the host, so a host that
    passes by accident (e.g. a unit that fired once and is now disabled) still
    gets caught. Both scripts are also what a human runs by hand from a console,
    so the probes live there rather than inline here.
    """
    # Not "isolated": that mode also asserts the AWS-level controls of a
    # --no-internet VPC, and the marker can be armed by hand on a connected host.
    mode = (
        "lockdown"
        if os.getenv("PROXMOX_EGRESS_LOCKDOWN_ENABLED") is not None
        else "connected"
    )

    rc, output = await run_script_on_host(
        async_proxmox_api, instance_config.node, HOST_SCRIPT.read_text(), [mode]
    )
    assert rc == 0, f"check-host-isolation.sh failed (rc={rc}):\n{output}"

    task_name = "test_host_isolation_e2e"
    config = ProxmoxSandboxEnvironmentConfig()

    _, envs_dict = await setup_sandbox(task_name, config)
    try:
        env = envs_dict["default"]
        assert isinstance(env, ProxmoxSandboxEnvironment)

        # No addresses to pass: CI has no interface endpoint or peering targets,
        # so those probes SKIP.
        guest_res = await env.exec(
            ["bash", "-s", "--", mode],
            input=GUEST_SCRIPT.read_text(),
            timeout=600,
        )
        assert guest_res.returncode == 0, (
            f"check-guest-isolation.sh failed (rc={guest_res.returncode}):\n"
            f"{guest_res.stdout}{guest_res.stderr}"
        )

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=config,
            environments=envs_dict,
            interrupted=False,
        )
