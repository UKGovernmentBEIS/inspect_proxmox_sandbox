"""Integration tests for FirewallCommands against a live Proxmox.

Uses the same conftest fixtures as the other integration test modules
(env vars PROXMOX_HOST/PORT/USER/PASSWORD/REALM/NODE).

Isolation is interface-scoped, so the test runner keeps API access via the
management-interface ACCEPT regardless of which subnet it is on — no
runner-specific allowlist is needed.
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
    """Disable the cluster + node firewall so the host is reachable again."""
    await api.request("PUT", "/cluster/firewall/options", json={"enable": 0})


@pytest.fixture
async def firewall_commands(
    infra_commands: InfraCommands,
) -> AsyncGenerator[FirewallCommands, None]:
    """Yield FirewallCommands with ours-tagged state cleaned before and after.

    Teardown disables the cluster firewall after clearing our state so the
    host stays reachable for subsequent tests / other API users.
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


def _ours(rules: list) -> list:
    return [r for r in rules if r.get("comment") == OURS_COMMENT]


async def test_ensure_host_isolation_from_clean_state(
    firewall_commands: FirewallCommands,
) -> None:
    """Apply from a clean slate; both firewalls enabled with managed rules."""
    fw = firewall_commands
    api = fw.async_proxmox

    await fw.ensure_host_isolation(HostIsolation())

    cluster_opts = await api.request("GET", "/cluster/firewall/options")
    assert int(cluster_opts.get("enable", 0)) == 1

    node_opts = await api.request("GET", f"/nodes/{fw.node}/firewall/options")
    assert int(node_opts.get("enable", 0)) == 1

    rules = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ours = _ours(rules)
    # 3 unscoped DNS/DHCP rules + 2 interface-scoped management rules.
    assert len(ours) == 5, f"expected 5 managed rules, found {len(ours)}: {ours}"
    by_dport = {r["dport"]: r for r in ours}
    assert by_dport["53"]["proto"] in ("udp", "tcp")
    assert by_dport["67"]["proto"] == "udp"
    # Management ports are pinned to an interface; DNS/DHCP are not.
    assert by_dport["8006"].get("iface")
    assert by_dport["22"].get("iface")
    assert by_dport["8006"]["iface"] == by_dport["22"]["iface"]
    assert not by_dport["53"].get("iface")


async def test_detect_management_interface_returns_active_physical(
    firewall_commands: FirewallCommands,
) -> None:
    """Auto-detection returns an active physical interface on the node."""
    fw = firewall_commands
    iface = await fw._detect_management_interface()

    net = await fw.async_proxmox.request("GET", f"/nodes/{fw.node}/network")
    match = [i for i in net if i.get("iface") == iface]
    assert match, f"{iface} not in node network config"
    assert match[0].get("type") in ("eth", "bond")
    assert int(match[0].get("active", 0)) == 1


async def test_ensure_host_isolation_is_idempotent(
    firewall_commands: FirewallCommands,
) -> None:
    fw = firewall_commands
    api = fw.async_proxmox

    await fw.ensure_host_isolation(HostIsolation())
    rules_after_first = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")

    # Force a re-run (the _applied memo would otherwise short-circuit).
    fw._applied = False
    await fw.ensure_host_isolation(HostIsolation())
    rules_after_second = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")

    assert len(_ours(rules_after_first)) == len(_ours(rules_after_second)) == 5


async def test_ensure_host_isolation_disabled_makes_no_changes(
    firewall_commands: FirewallCommands,
) -> None:
    fw = firewall_commands
    api = fw.async_proxmox

    rules_before = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ipset_before = await api.request("GET", "/cluster/firewall/ipset/management")

    await fw.ensure_host_isolation(HostIsolation(enabled=False))

    rules_after = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
    ipset_after = await api.request("GET", "/cluster/firewall/ipset/management")

    # Compare ignoring digest (which can change for unrelated reasons).
    def _strip(items):
        return [{k: v for k, v in i.items() if k != "digest"} for i in items]

    assert _strip(rules_before) == _strip(rules_after)
    assert _strip(ipset_before) == _strip(ipset_after)


async def test_ensure_host_isolation_management_cidrs_escape_hatch(
    firewall_commands: FirewallCommands,
) -> None:
    """An explicit management_cidrs entry lands in the ipset with our comment."""
    fw = firewall_commands
    api = fw.async_proxmox

    extra = IPv4Network("203.0.113.0/24")
    await fw.ensure_host_isolation(HostIsolation(management_cidrs=(extra,)))

    entries = await api.request("GET", "/cluster/firewall/ipset/management")
    matching = [e for e in entries if e.get("cidr") == str(extra)]
    assert matching, f"expected {extra} in management ipset, got {entries}"
    assert matching[0].get("comment") == OURS_COMMENT


async def test_ensure_host_isolation_conflict_on_foreign_drop(
    firewall_commands: FirewallCommands,
) -> None:
    """A foreign IN DROP on a managed dport raises and writes nothing.

    The conflict is detected before any enable, so the cluster firewall is
    left disabled — the host is never half-mutated.
    """
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
            await fw.ensure_host_isolation(HostIsolation())

        # No managed rules were added, and the firewall was never enabled.
        rules = await api.request("GET", f"/nodes/{fw.node}/firewall/rules")
        assert _ours(rules) == []
        cluster_opts = await api.request("GET", "/cluster/firewall/options")
        assert int(cluster_opts.get("enable", 0)) == 0

    finally:
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

    This is the property the feature exists to deliver. The VM reaches the
    host over its SDN bridge, so its packets never ingress on the management
    interface and hit the default-deny policy — even when aimed at the SDN
    gateway IP where pveproxy also listens.
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
