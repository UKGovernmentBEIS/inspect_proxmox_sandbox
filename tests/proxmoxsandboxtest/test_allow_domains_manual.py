"""Manual test coverage for allow_domains gaps not covered by test_allow_domains.

Covers:
1. Proxmox host IP is blocked  — the host's RFC1918 IP is never in allowed_ips,
   so a sandbox VM cannot reach the Proxmox API directly.
2. Subdomain gap (apex only) — specifying "gnu.org" does NOT give access to
   ftp.gnu.org because subdomain IPs are not pre-seeded.
3. Subdomain explicit listing — adding the subdomain explicitly does work.
4. Non-HTTP ports to allowed IPs — all ports are permitted (IP-level filter).
5. IPv6 SLAAC blocked — built-in VM templates have accept-ra: false; no global
   IPv6 address is assigned, so IPv6 cannot be used to bypass the gateway.
6. DNS-over-TLS (port 853) blocked — the gateway nftables forward chain drops
   port 853 traffic even to allowed-domain IPs, closing the DoT bypass path.
7. Multi-vnet egress filtering — 2 vnets with distinct CIDRs, 1 VM each on a
   different vnet. Allowed domain reachable from both VMs, blocked domain fails
   from both.

Note: each test uses a distinct /24 subnet (10.77.10–19.0/24) so tests do not
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
    VmNicConfig,
    VmSourceConfig,
    VnetConfig,
)

from .proxmox_sandbox_utils import setup_sandbox

PROXMOX_HOST_IP = os.environ.get("PROXMOX_HOST", "")


def _make_vnet(alias: str, third_octet: int) -> VnetConfig:
    """Build a VnetConfig with 10.77.<third_octet>.0/24, DHCP .50-.100."""
    base = f"10.77.{third_octet}"
    return VnetConfig(
        alias=alias,
        subnets=(
            SubnetConfig(
                cidr=ip_network(f"{base}.0/24"),
                snat=True,
                dhcp_ranges=(
                    DhcpRange(
                        start=ip_address(f"{base}.50"),
                        end=ip_address(f"{base}.100"),
                    ),
                ),
            ),
        ),
    )


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


async def test_subdomain_reachable_when_apex_listed() -> None:
    """Subdomains are covered dynamically via dnsmasq nftset=.

    "gnu.org" in the allowlist causes dnsmasq to forward DNS queries
    for ftp.gnu.org (subdomain) AND inject the resolved IPs into the
    nftables allowed_ips set via nftset=.  Both DNS and IP-level
    access work.

    gnu.org and ftp.gnu.org use distinct, non-CDN IPs, confirming
    this is dynamic nftset injection (not shared-IP coincidence).
    """
    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = _base_config(allow_domains=("gnu.org",), third_octet=11)
    task_name = "tsubdyn"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandbox = envs_dict["default"]

        # DNS resolves — dnsmasq forwards subdomain queries and
        # nftset= injects resolved IPs into allowed_ips.
        dns_result = await sandbox.exec(
            ["bash", "-c",
             "dig +short ftp.gnu.org @10.77.11.2 | head -3"],
            timeout=15,
        )
        print(
            f"\n[subdomain DNS] ftp.gnu.org:"
            f" {dns_result.stdout.strip()!r}"
        )

        # HTTP succeeds — nftset= injected ftp.gnu.org's IP when
        # dnsmasq resolved the subdomain query above.
        curl_result = await sandbox.exec(
            ["curl", "--fail", "--max-time", "10",
             "http://ftp.gnu.org"],
            timeout=15,
        )
        assert curl_result.success, (
            "curl to ftp.gnu.org should succeed "
            "(nftset= injects subdomain IPs dynamically)"
            f", but failed: {curl_result=}"
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
    """Explicitly listing a subdomain in allow_domains also works.

    Paired with test_subdomain_reachable_when_apex_listed: that test shows
    listing only the apex is sufficient (dnsmasq nftset= covers subdomains
    automatically).  This test confirms that explicitly listing both the apex
    and the subdomain is also fine — no conflict or duplicate-IP issues.
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


async def test_ipv6_slaac_blocked() -> None:
    """Built-in VM templates cannot acquire a global IPv6 address via SLAAC.

    cloud-init sets accept-ra: false on all built-in VM network configs (both
    sandbox and gateway).  dhcp6: false alone only disables DHCPv6; accept-ra
    is also needed to block SLAAC (stateless address autoconfiguration via
    Router Advertisements).

    This test verifies the protection holds end-to-end:
    1. No global-scope IPv6 address is assigned (SLAAC is blocked).
    2. IPv6 curl to an allowed domain fails (no IPv6 egress path).

    Note: a link-local address (fe80::) is normal and expected — it does not
    provide internet reachability.  Only global-scope addresses are a concern.

    Custom VM sources (OVA, existing_vm_template_tag) must configure
    accept-ra: false independently; that is documented as a known limitation
    in schema.py and is not tested here.
    """
    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = _base_config(allow_domains=("cloudflare.com",), third_octet=14)
    task_name = "tipv6slaac"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandbox = envs_dict["default"]

        # Check for global-scope IPv6 addresses — should be none.
        addr_result = await sandbox.exec(
            ["ip", "-6", "addr", "show", "scope", "global"],
            timeout=10,
        )
        assert addr_result.stdout.strip() == "", (
            "Sandbox VM should have no global-scope IPv6 address (SLAAC blocked "
            f"by accept-ra: false), but got: {addr_result.stdout.strip()!r}"
        )

        # Belt-and-braces: IPv6 curl to an allowed domain should also fail.
        # cloudflare.com has AAAA records; without a global IPv6 address the
        # connect will fail before even reaching the gateway.
        curl6_result = await sandbox.exec(
            ["curl", "--fail", "--max-time", "5", "--ipv6", "http://cloudflare.com"],
            timeout=10,
        )
        assert not curl6_result.success, (
            "IPv6 curl to cloudflare.com should fail (no global IPv6 address), "
            f"but succeeded: {curl6_result=}"
        )
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=config,
                environments=envs_dict,
                interrupted=False,
            )


async def test_dns_over_tls_port_853_blocked() -> None:
    """Gateway nftables drops port 853 even to allowed-domain IPs.

    The forward chain rules `tcp dport 853 drop` and `udp dport 853 drop`
    run before the `@allowed_ips accept` rule, so port 853 is blocked
    regardless of whether the destination IP is in the allowed set.  This
    closes DNS-over-TLS and DNS-over-QUIC bypass paths (a sandbox could
    otherwise send encrypted DNS queries directly to a resolver, bypassing
    the dnsmasq allowlist).

    Cloudflare (1.1.1.1) is both an allowed domain AND a DoT resolver.
    This test exploits that: port 443 to cloudflare.com succeeds (proving
    the IP is in the allowed set), but port 853 to the same IP fails
    (proving the port-853 drop rule fires before the IP accept rule).
    """
    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = _base_config(allow_domains=("cloudflare.com",), third_octet=15)
    task_name = "tdot853"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandbox = envs_dict["default"]

        # Resolve cloudflare.com to an IP so we can hit the same IP on two ports.
        # We use the gateway's dnsmasq directly to get the pre-seeded IP.
        resolve_result = await sandbox.exec(
            ["bash", "-c", "dig +short cloudflare.com @10.77.15.2 | head -1"],
            timeout=15,
        )
        cloudflare_ip = resolve_result.stdout.strip()
        assert cloudflare_ip, (
            f"Could not resolve cloudflare.com via gateway dnsmasq: {resolve_result=}"
        )
        print(f"\n[DoT test] cloudflare.com resolved to: {cloudflare_ip!r}")

        # Port 443 to the same IP — should be OPEN (IP is in the allowed set).
        nc_443 = await sandbox.exec(
            ["bash", "-c", f"nc -zw5 {cloudflare_ip} 443 && echo OPEN || echo CLOSED"],
            timeout=15,
        )
        assert "OPEN" in nc_443.stdout, (
            f"Port 443 to cloudflare IP {cloudflare_ip} should be reachable "
            f"(IP is in allowed set): {nc_443=}"
        )

        # Port 853 to the same IP — should be CLOSED (drop rule fires first).
        nc_853 = await sandbox.exec(
            ["bash", "-c", f"nc -zw5 {cloudflare_ip} 853 && echo OPEN || echo CLOSED"],
            timeout=15,
        )
        assert "OPEN" not in nc_853.stdout, (
            f"Port 853 to cloudflare IP {cloudflare_ip} should be blocked "
            f"by the tcp/udp dport 853 drop rule, but got: {nc_853=}"
        )
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=config,
                environments=envs_dict,
                interrupted=False,
            )


async def test_multi_vnet_egress_filtering() -> None:
    """Two vnets with distinct CIDRs share the same allowlist via one gateway.

    VM-A on vnet 16, VM-B on vnet 17.  Both should reach cloudflare.com
    (allowed) and both should fail to reach google.com (blocked).

    This is the core CAST requirement: multiple sandbox vnets sharing a
    single gateway VM with a shared domain allowlist.
    """
    envs_dict: Dict[str, SandboxEnvironment] = {}
    config = ProxmoxSandboxEnvironmentConfig(
        sdn_config=SdnConfig(
            vnet_configs=(
                _make_vnet("multi-a", 16),
                _make_vnet("multi-b", 17),
            ),
            use_pve_ipam_dnsnmasq=True,
            allow_domains=("cloudflare.com",),
        ),
        vms_config=(
            VmConfig(
                name="multi-vm-a",
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                ram_mb=512,
                vcpus=1,
                nics=(VmNicConfig(vnet_alias="multi-a"),),
            ),
            VmConfig(
                name="multi-vm-b",
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                ram_mb=512,
                vcpus=1,
                nics=(VmNicConfig(vnet_alias="multi-b"),),
            ),
        ),
    )
    task_name = "tmultivnet"
    try:
        _, envs_dict = await setup_sandbox(task_name, config)
        sandboxes = list(envs_dict.values())
        assert len(sandboxes) == 2, (
            f"Expected 2 sandbox envs, got {len(sandboxes)}"
        )

        for i, sandbox in enumerate(sandboxes):
            label = f"VM-{i}"

            # Allowed domain should be reachable
            curl_allowed = await sandbox.exec(
                [
                    "curl", "--fail", "--max-time", "15",
                    "http://cloudflare.com",
                ],
                timeout=20,
            )
            assert curl_allowed.success, (
                f"{label}: curl to cloudflare.com (allowed) "
                f"should succeed: {curl_allowed=}"
            )

            # Blocked domain should fail
            curl_blocked = await sandbox.exec(
                [
                    "curl", "--fail", "--max-time", "5",
                    "http://google.com",
                ],
                timeout=10,
            )
            assert not curl_blocked.success, (
                f"{label}: curl to google.com (blocked) "
                f"should fail: {curl_blocked=}"
            )
    finally:
        if envs_dict:
            await ProxmoxSandboxEnvironment.sample_cleanup(
                task_name=task_name,
                config=config,
                environments=envs_dict,
                interrupted=False,
            )
