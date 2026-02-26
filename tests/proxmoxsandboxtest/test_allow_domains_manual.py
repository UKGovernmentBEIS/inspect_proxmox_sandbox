"""Manual test coverage for allow_domains gaps not covered by test_allow_domains.

Covers:
1. Proxmox host IP is blocked  — the host's RFC1918 IP is never in allowed_ips,
   so a sandbox VM cannot reach the Proxmox API directly.
2. Subdomain gap (apex only) — specifying "gnu.org" does NOT give access to
   ftp.gnu.org because subdomain IPs are not pre-seeded.
3. Subdomain explicit listing — adding the subdomain explicitly does work.
4. Non-HTTP ports to allowed IPs — all ports are permitted (IP-level filter).

Pre-requisites:
    A running Proxmox instance configured as described in README.md.
    Environment variables set (see README.md "Requirements" section):
        PROXMOX_HOST, PROXMOX_PORT, PROXMOX_USER, PROXMOX_REALM,
        PROXMOX_PASSWORD, PROXMOX_NODE, PROXMOX_VERIFY_TLS

Run with:
    set -a; source .env; set +a
    uv run pytest tests/proxmoxsandboxtest/test_allow_domains_manual.py -v -s

Note: each test uses a distinct /24 subnet (10.77.10–13.0/24) so tests do not
conflict with each other or with stale zones from interrupted previous runs.
If CIDR conflicts are still reported, run `inspect sandbox cleanup proxmox` first.
"""

import os
from ipaddress import ip_address, ip_network
from typing import Dict

from inspect_ai.util import SandboxEnvironment

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import (
    DhcpRange,
    ProxmoxSandboxEnvironmentConfig,
    SdnConfig,
    SubnetConfig,
    VmConfig,
    VmSourceConfig,
    VnetConfig,
)

from .proxmox_sandbox_utils import setup_sandbox

PROXMOX_HOST_IP = os.environ.get("PROXMOX_HOST", "")


def _base_config(
    allow_domains: tuple[str, ...], third_octet: int
) -> ProxmoxSandboxEnvironmentConfig:
    """Build a test config using 10.77.<third_octet>.0/24.

    Each test passes a distinct third_octet (10–13) so concurrent or sequential
    runs do not collide on the same CIDR.  The gateway VM is assigned .2 by the
    provisioning logic; DHCP runs from .50–.100, safely clear of .2.
    """
    base = f"10.77.{third_octet}"
    return ProxmoxSandboxEnvironmentConfig(
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(
                    alias="manual-test",
                    subnets=(
                        SubnetConfig(
                            cidr=ip_network(f"{base}.0/24"),
                            gateway=ip_address(f"{base}.1"),
                            snat=True,
                            dhcp_ranges=(
                                DhcpRange(
                                    start=ip_address(f"{base}.50"),
                                    end=ip_address(f"{base}.100"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            use_pve_ipam_dnsnmasq=True,
            allow_domains=allow_domains,
        ),
        vms_config=(
            VmConfig(
                name="manual-test-vm",
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                ram_mb=512,
                vcpus=1,
            ),
        ),
    )


async def test_proxmox_host_ip_is_blocked() -> None:
    """Sandbox cannot reach the Proxmox host's RFC1918 IP directly.

    The host IP is never added to allowed_ips (it's not a DNS response for any
    allowed domain), so it must be blocked by the FORWARD chain's default drop.
    This closes the VM→Proxmox→other-VM lateral escape path.
    """
    assert PROXMOX_HOST_IP, "PROXMOX_HOST env var not set"

    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = _base_config(allow_domains=("cloudflare.com",), third_octet=10)
    task_name = "tphiblocked"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandbox = envs_dict["default"]

        result = await sandbox.exec(
            ["curl", "--fail", "--max-time", "5", f"http://{PROXMOX_HOST_IP}"],
            timeout=10,
        )
        assert not result.success, (
            f"curl to Proxmox host {PROXMOX_HOST_IP} should be blocked "
            f"by the gateway FORWARD chain, but succeeded: {result=}"
        )
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=config,
                environments=envs_dict,
                interrupted=False,
            )


async def test_subdomain_blocked_when_only_apex_listed() -> None:
    """Subdomain IPs are not pre-seeded; only apex domain IPs are.

    "gnu.org" in the allowlist lets dnsmasq resolve ftp.gnu.org (DNS works),
    but the returned IP (.20) was never added to allowed_ips (only gnu.org's .116
    was pre-seeded), so the FORWARD chain drops the traffic.

    Note: domains that share CDN IPs (e.g. debian.org and deb.debian.org both
    resolve to the same Fastly anycast IPs from 8.8.8.8) do NOT demonstrate this
    gap — allowing the apex inadvertently allows the subdomain.  gnu.org and
    ftp.gnu.org use distinct, non-CDN IPs, making the gap observable.
    """
    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = _base_config(allow_domains=("gnu.org",), third_octet=11)
    task_name = "tsubblocked"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandbox = envs_dict["default"]

        # DNS resolves (dnsmasq forwards subdomain queries for allowed apex).
        # Query the gateway VM directly — it is always at network-address+2.
        dns_result = await sandbox.exec(
            ["bash", "-c", "dig +short ftp.gnu.org @10.77.11.2 | head -3"],
            timeout=15,
        )
        print(f"\n[subdomain DNS] ftp.gnu.org: {dns_result.stdout.strip()!r}")

        # But HTTP connection is dropped — ftp.gnu.org's IP is not in allowed_ips
        curl_result = await sandbox.exec(
            ["curl", "--fail", "--max-time", "5", "http://ftp.gnu.org"],
            timeout=10,
        )
        assert not curl_result.success, (
            "curl to ftp.gnu.org should be blocked (subdomain IPs not pre-seeded), "
            f"but succeeded: {curl_result=}"
        )
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=config,
                environments=envs_dict,
                interrupted=False,
            )


async def test_subdomain_allowed_when_listed_explicitly() -> None:
    """Explicitly listing a subdomain in allow_domains pre-seeds its IPs correctly.

    Paired with test_subdomain_blocked_when_only_apex_listed: adding ftp.gnu.org
    explicitly causes its IP (.20) to be pre-seeded alongside gnu.org's (.116),
    so HTTP to ftp.gnu.org succeeds.
    """
    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = _base_config(allow_domains=("gnu.org", "ftp.gnu.org"), third_octet=12)
    task_name = "tsuballow"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandbox = envs_dict["default"]

        curl_result = await sandbox.exec(
            ["curl", "--fail", "--max-time", "15", "http://ftp.gnu.org"],
            timeout=20,
        )
        assert curl_result.success, (
            "curl to ftp.gnu.org should succeed when explicitly listed "
            f"in allow_domains: {curl_result=}"
        )
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=config,
                environments=envs_dict,
                interrupted=False,
            )


async def test_all_ports_open_to_allowed_ips() -> None:
    """Filtering is IP-level: all TCP ports to allowed-domain IPs are reachable.

    cloudflare.com listens on port 443 (HTTPS) and port 80 (HTTP).
    We test port 443 directly via TCP connect (not HTTPS handshake) to confirm
    the FORWARD chain does not restrict by port.
    """
    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = _base_config(allow_domains=("cloudflare.com",), third_octet=13)
    task_name = "tallports"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandbox = envs_dict["default"]

        # TCP connect to port 443 — allowed because cloudflare.com IPs are in set
        nc_result = await sandbox.exec(
            ["bash", "-c", "nc -zw5 cloudflare.com 443 && echo OPEN || echo CLOSED"],
            timeout=15,
        )
        assert "OPEN" in nc_result.stdout, (
            f"TCP port 443 to cloudflare.com (allowed domain) should be reachable: "
            f"{nc_result=}"
        )

        # TCP connect to a blocked domain's port 443 — should fail
        nc_blocked = await sandbox.exec(
            ["bash", "-c", "nc -zw5 google.com 443 && echo OPEN || echo CLOSED"],
            timeout=10,
        )
        # nc can't resolve google.com (SERVFAIL from dnsmasq) so it fails
        assert "OPEN" not in nc_blocked.stdout, (
            f"TCP port 443 to google.com (blocked) should not be reachable: "
            f"{nc_blocked=}"
        )
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=config,
                environments=envs_dict,
                interrupted=False,
            )
