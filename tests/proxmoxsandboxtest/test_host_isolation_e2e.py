"""End-to-end check that the Proxmox host actually isolates sandbox VMs.

Host firewall isolation is configured by the provisioning scripts (see the
README's "Host firewall isolation" section). Every supported way of standing
up a test host applies it, so this runs unconditionally as part of the
integration suite — if it fails, the host you're testing against wasn't
provisioned correctly (e.g. a hand-rolled Proxmox missing the firewall config).
"""

from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)

from .proxmox_sandbox_utils import setup_sandbox


async def test_sandbox_vm_cannot_reach_host_or_cloud_metadata() -> None:
    """A sandbox VM can't reach host services or cloud instance metadata.

    The VM reaches the host over its SDN bridge, so its packets never ingress
    on the host's management interface and hit the default-deny policy — even
    when aimed at the SDN gateway IP where pveproxy also listens. Metadata
    traffic is forwarded rather than host-bound, so provisioning also installs
    an explicit forwarding block for the fixed metadata endpoints.
    """
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

        metadata_res = await env.exec(
            [
                "curl",
                "-sS",
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
        assert metadata_res.stdout.strip() == "000", (
            "cloud instance metadata reachable from sandbox VM "
            f"(curl returned http_code={metadata_res.stdout.strip()!r}). "
            "Was the metadata forwarding block installed?"
        )

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=config,
            environments=envs_dict,
            interrupted=False,
        )
