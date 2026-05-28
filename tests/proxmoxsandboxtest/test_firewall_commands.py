"""Integration tests for FirewallCommands against a live Proxmox.

Uses the same conftest fixtures as the other integration test modules
(env vars PROXMOX_HOST/PORT/USER/PASSWORD/REALM/NODE).
"""

from ipaddress import IPv4Network
from typing import AsyncGenerator

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.firewall_commands import (
    OURS_COMMENT,
    FirewallCommands,
    HostIsolationConflictError,
)
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import (
    ProxmoxSandboxEnvironment,
    ProxmoxSandboxEnvironmentConfig,
)
from proxmoxsandbox.schema import HostIsolation

from .proxmox_sandbox_utils import setup_sandbox


async def _clear_ours_rules(api: AsyncProxmoxAPI, node: str) -> None:
    """Remove any ours-tagged rules from the node firewall (bottom-up)."""
    rules = await api.request("GET", f"/nodes/{node}/firewall/rules")
    to_delete = [r for r in rules if r.get("comment") == OURS_COMMENT]
    to_delete.sort(key=lambda r: int(r["pos"]), reverse=True)
    for r in to_delete:
        await api.request("DELETE", f"/nodes/{node}/firewall/rules/{r['pos']}")


async def _clear_ours_ipset_entries(api: AsyncProxmoxAPI) -> None:
    """Remove any ours-tagged entries from the management ipset."""
    ipsets = await api.request("GET", "/cluster/firewall/ipset")
    if not any(s.get("name") == "management" for s in ipsets):
        return
    entries = await api.request("GET", "/cluster/firewall/ipset/management")
    for e in entries:
        if e.get("comment") == OURS_COMMENT:
            await api.request(
                "DELETE",
                f"/cluster/firewall/ipset/management/{e['cidr']}",
            )


async def _disable_cluster_fw(api: AsyncProxmoxAPI) -> None:
    """Disable the cluster firewall so the host is reachable from any IP.

    Required after a test that locks down the ipset, otherwise the next
    test (or any other API user) can't connect.
    """
    await api.request("PUT", "/cluster/firewall/options", json={"enable": 0})


@pytest.fixture
async def firewall_commands(
    infra_commands: InfraCommands,
) -> AsyncGenerator[FirewallCommands, None]:
    """Yield FirewallCommands with ours-tagged state cleaned before and after.

    Teardown disables the cluster firewall after clearing our state so the
    host stays reachable for subsequent tests / other API users — our
    teardown can leave the management ipset empty, which would otherwise
    block all non-host-subnet traffic.
    """
    fw = infra_commands.firewall_commands
    api = fw.async_proxmox
    node = fw.node

    await _clear_ours_rules(api, node)
    await _clear_ours_ipset_entries(api)
    # Re-use the same FirewallCommands across tests, but reset its memo.
    fw._applied = False

    yield fw

    fw._applied = False
    await _clear_ours_rules(api, node)
    await _clear_ours_ipset_entries(api)
    await _disable_cluster_fw(api)


async def test_ensure_host_isolation_from_clean_state(
    firewall_commands: FirewallCommands,
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> None:
    """Apply from a clean slate; both firewalls enabled with managed rules."""
    fw = firewall_commands
    api = fw.async_proxmox

    # Use an explicit extra_management_cidrs so the ipset assertion is
    # deterministic regardless of how the test connects to the Proxmox
    # host (auto-detection returns None when caller_host resolves to
    # loopback, e.g. via an SSH tunnel).
    extra = IPv4Network("198.51.100.0/24")
    await fw.ensure_host_isolation(
        HostIsolation(extra_management_cidrs=(extra,)),
        caller_host=sandbox_env_config.host,
    )

    cluster_opts = await api.request("GET", "/cluster/firewall/options")
    assert int(cluster_opts.get("enable", 0)) == 1

    node_opts = await api.request("GET", f"/nodes/{fw.node}/firewall/options")
    assert int(node_opts.get("enable", 0)) == 1

    rules = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ours = [r for r in rules if r.get("comment") == OURS_COMMENT]
    assert len(ours) == 3, f"expected 3 managed rules, found {len(ours)}: {ours}"
    by_dport = {r["dport"]: r for r in ours}
    assert by_dport["53"]["proto"] in ("udp", "tcp")
    assert by_dport["67"]["proto"] == "udp"

    ipset_entries = await api.request("GET", "/cluster/firewall/ipset/management")
    our_entries = [e for e in ipset_entries if e.get("comment") == OURS_COMMENT]
    assert any(e["cidr"] == str(extra) for e in our_entries), (
        f"expected ours-tagged entry for {extra}, found {ipset_entries}"
    )


async def test_ensure_host_isolation_is_idempotent(
    firewall_commands: FirewallCommands,
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> None:
    fw = firewall_commands
    api = fw.async_proxmox

    await fw.ensure_host_isolation(HostIsolation(), caller_host=sandbox_env_config.host)
    rules_after_first = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ipset_after_first = await api.request("GET", "/cluster/firewall/ipset/management")

    # Force a re-run (the _applied memo would otherwise short-circuit).
    fw._applied = False
    await fw.ensure_host_isolation(HostIsolation(), caller_host=sandbox_env_config.host)

    rules_after_second = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ipset_after_second = await api.request("GET", "/cluster/firewall/ipset/management")

    # Same number of ours-tagged rules in both reads; no duplicates.
    def ours_count(rules: list) -> int:
        return sum(1 for r in rules if r.get("comment") == OURS_COMMENT)

    assert ours_count(rules_after_first) == ours_count(rules_after_second) == 3
    assert sum(1 for e in ipset_after_first if e.get("comment") == OURS_COMMENT) == sum(
        1 for e in ipset_after_second if e.get("comment") == OURS_COMMENT
    )


async def test_ensure_host_isolation_disabled_makes_no_changes(
    firewall_commands: FirewallCommands,
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> None:
    fw = firewall_commands
    api = fw.async_proxmox

    rules_before = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ipset_before = await api.request("GET", "/cluster/firewall/ipset/management")

    await fw.ensure_host_isolation(
        HostIsolation(enabled=False), caller_host=sandbox_env_config.host
    )

    rules_after = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ipset_after = await api.request("GET", "/cluster/firewall/ipset/management")

    # Compare ignoring digest (which can change for unrelated reasons).
    def _strip(items):
        return [{k: v for k, v in i.items() if k != "digest"} for i in items]

    assert _strip(rules_before) == _strip(rules_after)
    assert _strip(ipset_before) == _strip(ipset_after)


async def test_ensure_host_isolation_extra_cidrs(
    firewall_commands: FirewallCommands,
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> None:
    """An explicit extra_management_cidrs entry lands in the ipset with our comment."""
    fw = firewall_commands
    api = fw.async_proxmox

    extra = IPv4Network("203.0.113.0/24")
    await fw.ensure_host_isolation(
        HostIsolation(extra_management_cidrs=(extra,)),
        caller_host=sandbox_env_config.host,
    )

    entries = await api.request("GET", "/cluster/firewall/ipset/management")
    matching = [e for e in entries if e.get("cidr") == str(extra)]
    assert matching, f"expected {extra} in management ipset, got {entries}"
    assert matching[0].get("comment") == OURS_COMMENT


async def test_ensure_host_isolation_conflict_on_foreign_drop(
    firewall_commands: FirewallCommands,
    sandbox_env_config: ProxmoxSandboxEnvironmentConfig,
) -> None:
    """A foreign IN DROP on a managed dport raises and writes nothing."""
    fw = firewall_commands
    api = fw.async_proxmox

    await api.request(
        "POST",
        f"/nodes/{fw.node}/firewall/rules",
        json={
            "type": "in",
            "action": "DROP",
            "proto": "udp",
            "dport": "53",
            "enable": 1,
        },
    )

    try:
        with pytest.raises(HostIsolationConflictError):
            await fw.ensure_host_isolation(
                HostIsolation(), caller_host=sandbox_env_config.host
            )

        # No managed rules were added — atomicity check.
        rules = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
        ours = [r for r in rules if r.get("comment") == OURS_COMMENT]
        assert ours == []

    finally:
        # Remove the foreign rule we added.
        rules = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
        for r in rules:
            if (
                r.get("type") == "in"
                and r.get("action") == "DROP"
                and r.get("proto") == "udp"
                and r.get("dport") == "53"
                and r.get("comment") in (None, "")
            ):
                await api.request(
                    "DELETE",
                    f"/nodes/{fw.node}/firewall/rules/{r['pos']}",
                )
                break


async def test_sandbox_vm_cannot_reach_pveproxy_or_ssh() -> None:
    """End-to-end: a sandbox VM brought up via sample_init can't curl pveproxy.

    This is the property the feature exists to deliver — the API-level
    assertions only prove the API plumbing fires correctly. We hit the
    SDN gateway IP from inside the VM because that's the IP the VM sees
    as its default route, and it's also where pveproxy listens (since
    pveproxy binds 0.0.0.0:8006).
    """
    task_name = "test_firewall_e2e"
    config = ProxmoxSandboxEnvironmentConfig()  # default: host_isolation enabled

    _, envs_dict = await setup_sandbox(task_name, config)
    try:
        env = envs_dict["default"]
        assert isinstance(env, ProxmoxSandboxEnvironment)

        # Discover the SDN gateway IP from inside the VM. That gateway is
        # the Proxmox host's IP on the SDN bridge — pveproxy will be
        # listening there.
        gw_res = await env.exec(
            ["sh", "-c", "ip route show default | awk '{print $3}'"],
            timeout=10,
        )
        assert gw_res.returncode == 0
        gw = gw_res.stdout.strip()
        assert gw, "no default gateway found inside the sandbox VM"

        # Try to reach pveproxy. Without the firewall this returns HTTP
        # 401 in ~30ms; with the firewall, the TCP connection itself
        # never completes and curl exits 28 (operation timed out).
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
            f"(curl returned http_code={api_res.stdout.strip()!r}, "
            f"returncode={api_res.returncode}). "
            "The host firewall should be blocking this."
        )

        # Same check for SSH — confirm the TCP connection doesn't
        # complete in 3s.
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

    finally:
        await ProxmoxSandboxEnvironment.sample_cleanup(
            task_name=task_name,
            config=config,
            environments=envs_dict,
            interrupted=False,
        )
