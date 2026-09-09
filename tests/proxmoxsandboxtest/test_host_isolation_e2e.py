"""End-to-end check that the Proxmox host actually isolates sandbox VMs.

Host firewall isolation is configured by the provisioning scripts (see the
README's "Host firewall isolation" section). Every supported way of standing
up a test host applies it, so this runs unconditionally as part of the
integration suite — if it fails, the host you're testing against wasn't
provisioned correctly (e.g. a hand-rolled Proxmox missing the firewall config).

The fuller probes live in scripts/ec2/experimental/check-{host,guest}-isolation.sh,
which assume a --no-internet host — not the connected host the rest of the suite
needs. What is asserted here is the subset that holds on any host.
"""

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

# Catches a host whose units fired once and are now disabled, or a stale AMI that
# never had them: the guest probes below would pass on such a host until the rules
# were next reloaded.
HOST_UNITS_SCRIPT = """
set -eu
for unit in proxmox-ami-fixup-firewall.service \
            inspect-proxmox-block-cloud-metadata.service \
            proxmox-ami-fixup-nat.service; do
    systemctl is-enabled -q "$unit"
    [ "$(systemctl show -p Result --value "$unit")" = success ]
done
pve-firewall status | grep -q enabled/running
iptables -w -t raw -S PREROUTING | grep -q -- '-d 169.254.0.0/16 -j DROP'
iptables -w -S FORWARD | grep -q -- '-s 169.254.0.0/16 -j DROP'
"""


async def test_sandbox_vm_cannot_reach_host_or_cloud_metadata(
    async_proxmox_api: AsyncProxmoxAPI, instance_config: ProxmoxInstanceConfig
) -> None:
    """A sandbox VM can't reach host services or cloud instance metadata.

    The VM reaches the host over its SDN bridge, so its packets never ingress
    on the host's management interface and hit the default-deny policy — even
    when aimed at the SDN gateway IP where pveproxy also listens. Metadata
    traffic is forwarded rather than host-bound, so provisioning also installs
    an explicit forwarding block for the fixed metadata endpoints.
    """
    rc, output = await run_script_on_host(
        async_proxmox_api, instance_config.node, HOST_UNITS_SCRIPT
    )
    assert rc == 0, f"host isolation units not healthy (rc={rc}):\n{output}"

    task_name = "test_host_isolation_e2e"
    config = ProxmoxSandboxEnvironmentConfig()

    _, envs_dict = await setup_sandbox(task_name, config)
    try:
        env = envs_dict["default"]
        assert isinstance(env, ProxmoxSandboxEnvironment)

        gw_res = await env.exec(
            ["sh", "-c", "ip route show default | awk '{print $3}'"],
            timeout=10,
        )
        assert gw_res.returncode == 0
        gw = gw_res.stdout.strip()
        assert gw, "no default gateway found inside the sandbox VM"

        # Without isolation this returns HTTP 401 in ~30ms; with it, the TCP
        # connection never completes and curl exits 28 (http_code 000).
        api_res = await env.exec(
            [
                "curl",
                "-sk",
                "--max-time",
                "5",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                f"https://{gw}:8006/api2/json/version",
            ],
            timeout=15,
        )
        assert api_res.stdout.strip() == "000", (
            "pveproxy reachable from sandbox VM "
            f"(curl returned http_code={api_res.stdout.strip()!r}). "
            "Was the host provisioned with firewall isolation?"
        )

        ssh_res = await env.exec(
            [
                "sh",
                "-c",
                f'timeout 3 bash -c "</dev/tcp/{gw}/22" && echo open || echo blocked',
            ],
            timeout=10,
        )
        assert ssh_res.stdout.strip() == "blocked", (
            f"SSH on {gw}:22 reachable from sandbox VM: {ssh_res.stdout!r}"
        )

        # A token PUT can time out solely because HttpPutResponseHopLimit=1,
        # even when IMDS is reachable. A tokenless GET returns 401 when IMDSv2
        # is required (or 200 when optional), so 000 specifically verifies that
        # the host forwarding rule blocked the request.
        metadata_get_res = await env.exec(
            [
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "5",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "http://169.254.169.254/latest/meta-data/instance-id",
            ],
            timeout=15,
        )
        assert metadata_get_res.stdout.strip() == "000", (
            "cloud instance metadata reachable from sandbox VM "
            f"(curl returned http_code={metadata_get_res.stdout.strip()!r}). "
            "Was the metadata forwarding block installed?"
        )

        metadata_token_res = await env.exec(
            [
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "5",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-X",
                "PUT",
                "-H",
                "X-aws-ec2-metadata-token-ttl-seconds: 60",
                "http://169.254.169.254/latest/api/token",
            ],
            timeout=15,
        )
        assert metadata_token_res.stdout.strip() == "000", (
            "cloud instance metadata token endpoint reachable from sandbox VM "
            f"(curl returned http_code={metadata_token_res.stdout.strip()!r}). "
            "Was the metadata forwarding block installed?"
        )

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=config,
            environments=envs_dict,
            interrupted=False,
        )
